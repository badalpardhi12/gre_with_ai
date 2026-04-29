"""Persist extracted Princeton questions to the worktree DB.

Pipeline (per item):

  1. Skip if the item failed any deterministic gate (status='draft' with
     ``review_notes={"reason": "deterministic_gate_fail", ...}``).
  2. If a per-item LLM extraction-faithfulness verdict exists in
     ``data/extracted/princeton/verification/`` or the post-fix dir, use
     it. ``verified=False`` -> draft with verifier defects.
  3. Run :func:`services.expert_review.expert_review` on the dict. The
     drafter is the deterministic extractor (no model), so no exclusion
     needed. ``verdict=draft`` -> persist as draft with the per-judge
     breakdown stored in ``review_notes``.
  4. Otherwise upsert as ``status='live'``.

The expert review is text-only and 3 judges deep, so items whose stem
references a chart, geometry diagram, or other figure are routed past
the jury (``expert_skipped_figure``) and rely on the deterministic
gates + the per-item vision verifier instead.

Idempotency
-----------

The unique key is ``(source='princeton_2012', source_anchor=<QSTxxx>)``.
On re-run, an existing row is updated in place; no duplicates are
created. Old QuestionOption / NumericAnswer rows are deleted and rewritten.

Stimulus
--------

RC passages and DI charts are persisted as :class:`Stimulus` rows keyed
by their ``stimulus_anchor``. Sibling questions in the same cluster
share the foreign-key reference.

Output
------

``data/extracted/princeton/persistence_summary.json`` records the live /
draft split, per-stage drop counts, total spend (LLM verifier + expert
review), and the per-defect histogram so the reviewer can see at a
glance where items are landing.

Concurrency
-----------

Expert-review calls go through a thread pool (``--workers``) because
each call serializes 3 judge requests internally; one item takes
~50-90 seconds end-to-end. Default 6 workers keeps total wall time
under an hour for ~800 items while staying well below the gateway's
rate limit.

Usage
-----

::

    venv/bin/python scripts/persist_princeton.py
    venv/bin/python scripts/persist_princeton.py --limit 25      # smoke test
    venv/bin/python scripts/persist_princeton.py --skip-expert   # gates+verify only
    venv/bin/python scripts/persist_princeton.py --dry-run       # plan, no DB / no LLM
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

# Path acrobatics so the script runs from the worktree root.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
MAIN_REPO = "/Users/chiku/Documents/side_projects/gre_with_ai"
if MAIN_REPO not in sys.path:
    sys.path.append(MAIN_REPO)

from models.database import (  # noqa: E402
    db, init_db, Question, QuestionOption, NumericAnswer, Stimulus,
)
from services.expert_review import expert_review  # noqa: E402


PRINCETON_DIR = os.path.join(ROOT, "data", "extracted", "princeton")
ALL_QUESTIONS = os.path.join(ROOT, "sample_review_tmp", "all_questions.json")
TC_VISION = os.path.join(ROOT, "sample_review_tmp", "tc_vision_results.json")
SOURCE_TAG = "princeton_2012"
SUMMARY_OUT = os.path.join(PRINCETON_DIR, "persistence_summary.json")

VERIFICATION_DIRS = [
    os.path.join(PRINCETON_DIR, "verification_post_fix"),
    os.path.join(PRINCETON_DIR, "verification"),
]

# Subtype -> (Question.measure, Question.subtype) mapping. We accept the
# extractor's subtype verbatim — the schema's CharField is open.
def _measure_for(subtype: str, extractor_measure: str) -> str:
    if extractor_measure in {"verbal", "quant", "awa"}:
        return extractor_measure
    if subtype.startswith(("rc", "tc", "se")):
        return "verbal"
    return "quant"


# ── Verification cache --------------------------------------------------


def load_verification_cache() -> dict:
    """Build {qst_id: verdict_dict} prefering the post-fix verification."""
    cache = {}
    for vdir in VERIFICATION_DIRS:
        if not os.path.isdir(vdir):
            continue
        for fn in os.listdir(vdir):
            if not fn.startswith("qst") or not fn.endswith(".json"):
                continue
            try:
                qid = int(fn[3:-5])
            except ValueError:
                continue
            if qid in cache:
                continue  # earlier dir (post_fix) already wins
            try:
                with open(os.path.join(vdir, fn)) as f:
                    blob = json.load(f)
            except Exception:
                continue
            verdict = blob.get("verdict") or blob
            cache[qid] = verdict
    return cache


# ── TC table flattening -------------------------------------------------


def _flat_options_from_vision(vr, raw_correct_label):
    """Flatten the vision-extracted multi-blank table into a flat options list.

    Necessary because :class:`QuestionOption` is a flat (label, text)
    table — the runtime question screen reconstructs the per-blank grid
    from the option_label suffixes (``blank1_A``, ``blank2_F``, ...).
    """
    out = []
    correct_set = set()
    for blank_label, ch_label in (vr.get("matches") or []):
        correct_set.add((blank_label, ch_label))
    multi = len(vr["table"]["blanks"]) > 1
    for blank in vr["table"]["blanks"]:
        for ch in blank["choices"]:
            label = (blank["label"] + "_" + ch["label"]) if multi else ch["label"]
            out.append({
                "label": label,
                "text": ch["text"],
                "is_correct": (blank["label"], ch["label"]) in correct_set,
            })
    return out


def _estimate_difficulty(subtype: str, stem: str, stimulus_text: str) -> int:
    """Deterministic per-item difficulty estimate in 1..5.

    Princeton's source material doesn't label difficulty, but uniform
    ``difficulty_target=3`` breaks ``services.question_bank``'s easy/hard
    filters. We bucket by combined stem + stimulus length using subtype-
    specific thresholds calibrated against the ``ai_synthetic``
    distribution. Intended as a coarse signal; run
    ``scripts/backfill_difficulty_target.py`` afterwards for a
    population-relative spread.
    """
    n = len(stem or "") + len(stimulus_text or "")
    # (threshold, difficulty) pairs; first threshold wins.
    curve = {
        "tc": [(90, 2), (160, 3), (280, 4), (10**9, 5)],
        "se": [(120, 2), (180, 3), (260, 4), (10**9, 5)],
        "qc": [(50, 2), (90, 3), (160, 4), (10**9, 5)],
        "numeric_entry": [(70, 2), (130, 3), (220, 4), (10**9, 5)],
        "mcq_single": [(70, 2), (130, 3), (220, 4), (10**9, 5)],
        "mcq_multi": [(100, 2), (160, 3), (250, 4), (10**9, 5)],
        "data_interp": [(120, 3), (200, 4), (10**9, 5)],
        "rc_single": [(40, 2), (90, 3), (160, 4), (10**9, 5)],
        "rc_multi": [(120, 2), (200, 3), (320, 4), (10**9, 5)],
        "rc_select_passage": [(80, 2), (140, 3), (220, 4), (10**9, 5)],
    }.get(subtype, [(100, 2), (180, 3), (300, 4), (10**9, 5)])
    for threshold, diff in curve:
        if n <= threshold:
            return diff
    return 3


def build_question_for_review(q: dict, tc_vision: dict) -> dict:
    """Project an extracted item into the dict shape expert_review expects."""
    options = q.get("options") or []
    if q["subtype"] == "tc":
        vr = tc_vision.get(str(q["qst_id"]))
        if vr and "table" in vr:
            options = _flat_options_from_vision(vr, q.get("correct_label"))
    stem = q.get("prompt") or ""
    stimulus_text = q.get("stimulus_text") or ""
    return {
        "subtype": q["subtype"],
        "stem": stem,
        "options": options,
        "correct_label": q.get("correct_label"),
        "explanation": "",
        "difficulty": _estimate_difficulty(q["subtype"], stem, stimulus_text),
        "source": SOURCE_TAG,
        "stimulus_text": stimulus_text,
    }


# ── Stimulus cache ------------------------------------------------------


def upsert_stimulus(anchor: str, stim_type: str, content: str,
                    cache: dict) -> Stimulus:
    """Idempotent stimulus upsert keyed by ``anchor`` (used as the title)."""
    if anchor in cache:
        return cache[anchor]
    obj, _ = Stimulus.get_or_create(
        title=anchor,
        defaults={
            "stimulus_type": stim_type,
            "content": content or "",
        },
    )
    # Backfill content if a re-run got a fuller passage than the first.
    if content and obj.content != content:
        obj.content = content
        obj.save()
    cache[anchor] = obj
    return obj


# ── Persistence ---------------------------------------------------------


def _expert_review_eligible(q: dict) -> bool:
    """Return True iff a text-only judge can fairly grade this question.

    Items whose stem references a chart, diagram, or geometry figure
    can't be scored without the image. The text-only jury would
    universally tag them ``missing_figure`` / ``unsolvable`` and route
    them to draft, which would be both expensive and a false negative.
    For those items we trust the deterministic gates + the per-item
    vision verifier and skip the expert review with reason
    ``expert_skipped_figure``. Returning False here means
    ``persist_one`` will land the item as ``status='live'`` (assuming
    earlier gates passed) without calling the jury.
    """
    if q.get("subtype") == "data_interp":
        return False
    # Quant items with attached figures (geometry diagrams, number
    # lines) — judges can't see them.
    if q.get("figure_refs"):
        return False
    # DI cluster siblings that don't carry their own figure_refs but
    # share the cluster's chart via stimulus_anchor.
    if q.get("stimulus_anchor") and q.get("measure") == "quant":
        return False
    return True


def plan_one(q: dict, verify_cache: dict) -> dict:
    """Decide a routing reason that doesn't need an LLM call.

    Returns a dict with::
      decision : 'live' | 'draft' | None    (None = needs expert review)
      reason   : str
      defects  : list[str]
      review_notes : str (JSON-encoded blob to persist)
    Items that pass the deterministic gate AND the verifier AND are
    expert-eligible return ``decision=None`` so the driver knows to
    enqueue them for the jury.
    """
    if not q.get("_gate_passed"):
        return {
            "decision": "draft",
            "reason": "deterministic_gate_fail",
            "defects": list(q.get("_failed_gates") or []),
            "review_notes": json.dumps({
                "stage": "deterministic_gates",
                "failed_gates": q.get("_failed_gates") or [],
                "details": {k: q.get("_gate_details", {}).get(k)
                            for k in (q.get("_failed_gates") or [])},
            }, ensure_ascii=False),
        }
    ver = verify_cache.get(q["qst_id"])
    if ver and ver.get("verified") is False and not ver.get("skipped"):
        return {
            "decision": "draft",
            "reason": "verifier_defect",
            "defects": list(ver.get("defects") or []),
            "review_notes": json.dumps({
                "stage": "extraction_verifier",
                "verdict": ver,
            }, ensure_ascii=False),
        }
    if not _expert_review_eligible(q):
        return {
            "decision": "live",
            "reason": "expert_skipped_figure",
            "defects": [],
            "review_notes": "",
        }
    return {"decision": None, "reason": None, "defects": [], "review_notes": ""}


def expert_review_one(q: dict, tc_vision: dict) -> dict:
    """Synchronous wrapper around :func:`expert_review` for the pool."""
    review_dict = build_question_for_review(q, tc_vision)
    t0 = time.time()
    try:
        ev = expert_review(review_dict)
    except Exception as exc:
        ev = {"verdict": "draft",
              "promotion_reason": "judge_error",
              "reviewer_notes": "exception: " + str(exc),
              "defects": ["judge_error"],
              "per_judge": [], "axis_summary": {}}
    ev["elapsed_s"] = round(time.time() - t0, 1)
    return {"qst_id": q["qst_id"], "ev": ev}


def persist_one(q: dict, tc_vision: dict, plan: dict,
                expert_ev: "Optional[dict]",
                stim_cache: dict, *, dry_run: bool) -> dict:
    """Apply all gates and persist one question. Returns a routing record."""
    rec = {
        "qst_id": q["qst_id"],
        "subtype": q["subtype"],
        "source_anchor": q["source_anchor"],
        "decision": plan["decision"],
        "reason": plan["reason"],
        "defects": list(plan["defects"]),
        "expert_verdict": None,
    }
    review_notes = plan["review_notes"]

    # Resolve the expert-review verdict if the planner deferred.
    if plan["decision"] is None:
        if expert_ev is None:
            # Planner asked for review but the driver didn't supply one
            # (--skip-expert path). Land as live.
            rec["decision"] = "live"
            rec["reason"] = "expert_skipped"
        else:
            ev = expert_ev
            rec["expert_verdict"] = {
                "verdict": ev.get("verdict"),
                "reason": ev.get("promotion_reason"),
                "defects": ev.get("defects"),
                "passing_judges": ev.get("passing_judges"),
                "reviewer_notes": ev.get("reviewer_notes"),
                "elapsed_s": ev.get("elapsed_s"),
            }
            if ev.get("verdict") == "live":
                rec["decision"] = "live"
                rec["reason"] = "expert_pass"
            else:
                rec["decision"] = "draft"
                rec["reason"] = "expert_" + str(ev.get("promotion_reason"))
                rec["defects"] = list(ev.get("defects") or [])
                review_notes = json.dumps({
                    "stage": "expert_review",
                    "verdict": ev,
                }, ensure_ascii=False)

    if dry_run:
        return rec

    # ── DB writes ------------------------------------------------------
    measure = _measure_for(q["subtype"], q.get("measure", ""))

    # Stimulus
    stimulus = None
    anchor = q.get("stimulus_anchor")
    if anchor:
        stim_type = "passage" if measure == "verbal" else "graph"
        stimulus = upsert_stimulus(
            anchor, stim_type, q.get("stimulus_text") or "", stim_cache)

    # Idempotent question upsert by (source, source_anchor).
    existing = (Question
                .select()
                .where((Question.source == SOURCE_TAG)
                       & (Question.source_anchor == q["source_anchor"]))
                .first())
    # Idempotency guard: when the planner returned ``decision=live`` with
    # reason ``expert_skipped`` (i.e. ``--skip-expert`` was passed for
    # this run), DON'T overwrite a row that already carries an expert
    # verdict in ``review_notes``. Otherwise a quick re-run with
    # --skip-expert would revive items the jury had previously
    # demoted to draft.
    if (existing and rec.get("reason") == "expert_skipped"
            and (existing.review_notes or "")
            and existing.status == "draft"):
        rec["decision"] = "draft"
        rec["reason"] = "preserved_existing_draft"
        rec["question_id"] = existing.id
        return rec
    payload = {
        "measure": measure,
        "subtype": q["subtype"],
        "stimulus": stimulus,
        "prompt": q.get("prompt") or "",
        "difficulty_target": _estimate_difficulty(
            q["subtype"], q.get("prompt") or "", q.get("stimulus_text") or ""
        ),
        "concept_tags": json.dumps([]),
        "topic": "",
        "subtopic": "",
        "question_type": "",
        "source": SOURCE_TAG,
        "provenance": "imported",
        "status": rec["decision"],
        "explanation": "",
        "source_anchor": q["source_anchor"],
        "review_notes": review_notes,
        "updated_at": datetime.now(),
    }
    if existing:
        for k, v in payload.items():
            setattr(existing, k, v)
        existing.save()
        question_obj = existing
        # Wipe & rewrite child rows.
        QuestionOption.delete().where(QuestionOption.question == existing).execute()
        NumericAnswer.delete().where(NumericAnswer.question == existing).execute()
    else:
        question_obj = Question.create(**payload)

    # Options (TC items get the flattened blank1_A / blank2_F labels).
    options_to_write = []
    if q["subtype"] == "tc":
        vr = tc_vision.get(str(q["qst_id"]))
        if vr and "table" in vr:
            options_to_write = _flat_options_from_vision(
                vr, q.get("correct_label"))
    if not options_to_write:
        options_to_write = q.get("options") or []
    for o in options_to_write:
        QuestionOption.create(
            question=question_obj,
            option_label=(o.get("label") or "")[:32],
            option_text=o.get("text") or "",
            is_correct=bool(o.get("is_correct")),
        )

    # Numeric entry: store the structured answer if the extractor produced one.
    if q["subtype"] == "numeric_entry" and q.get("numeric_answer"):
        na = q["numeric_answer"]
        if isinstance(na, dict):
            NumericAnswer.create(
                question=question_obj,
                exact_value=na.get("exact_value"),
                numerator=na.get("numerator"),
                denominator=na.get("denominator"),
                tolerance=na.get("tolerance", 0.001),
                mode=na.get("mode", "auto"),
            )

    rec["question_id"] = question_obj.id
    return rec


# ── Driver -------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="cap items processed (smoke test)")
    parser.add_argument("--skip-expert", action="store_true",
                        help="bypass the expert-review jury. Items that "
                             "already carry a stored expert verdict in "
                             "Question.review_notes are left untouched so "
                             "repeated runs don't blow away the gate.")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan only — no DB writes, no LLM calls")
    parser.add_argument("--workers", type=int, default=6,
                        help="thread-pool size for the expert-review jury "
                             "(default 6 — each call serialises 3 judges)")
    parser.add_argument("--questions",
                        default=ALL_QUESTIONS,
                        help="path to extractor output JSON")
    args = parser.parse_args()

    print(f"Loading questions from {args.questions}", flush=True)
    questions = json.load(open(args.questions))
    if args.limit:
        questions = questions[:args.limit]
    print(f"  {len(questions)} items", flush=True)

    tc_vision = json.load(open(TC_VISION)) if os.path.exists(TC_VISION) else {}
    verify_cache = load_verification_cache()
    print(f"  {len(verify_cache)} cached verifier verdicts", flush=True)

    if not args.dry_run:
        init_db()

    # ── Phase 1: deterministic planning (no LLM) ----------------------
    plans = [plan_one(q, verify_cache) for q in questions]
    needs_expert_idx = [i for i, p in enumerate(plans) if p["decision"] is None]
    print(f"  planner: {len(needs_expert_idx)} item(s) need expert review; "
          f"{len(plans) - len(needs_expert_idx)} already routed", flush=True)

    # ── Phase 2: parallel expert review --------------------------------
    expert_results = {}      # qst_id -> ev dict
    expert_calls = 0
    t_review_start = time.time()
    if not args.skip_expert and not args.dry_run and needs_expert_idx:
        print(f"  expert review: {len(needs_expert_idx)} call(s) across "
              f"{args.workers} workers", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(expert_review_one, questions[i], tc_vision): i
                for i in needs_expert_idx
            }
            done = 0
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    out = fut.result()
                except Exception as exc:
                    out = {"qst_id": questions[i]["qst_id"],
                           "ev": {"verdict": "draft",
                                  "promotion_reason": "judge_error",
                                  "reviewer_notes": "future failed: " + str(exc),
                                  "defects": ["judge_error"],
                                  "per_judge": [], "axis_summary": {}}}
                expert_results[out["qst_id"]] = out["ev"]
                expert_calls += 1
                done += 1
                if done % 25 == 0 or done == len(needs_expert_idx):
                    elapsed = time.time() - t_review_start
                    rate = done / max(elapsed, 0.1)
                    eta = (len(needs_expert_idx) - done) / max(rate, 0.001)
                    n_live = sum(1 for ev in expert_results.values()
                                 if ev.get("verdict") == "live")
                    print(f"    [{done}/{len(needs_expert_idx)}] live={n_live} "
                          f"draft={done - n_live} elapsed={elapsed:.0f}s "
                          f"eta={eta:.0f}s", flush=True)

    # ── Phase 3: serial DB persistence --------------------------------
    stim_cache = {}
    routing = []
    counter = Counter()
    t_start = time.time()
    for i, q in enumerate(questions, 1):
        plan = plans[i - 1]
        ev = expert_results.get(q["qst_id"])
        rec = persist_one(q, tc_vision, plan, ev, stim_cache,
                          dry_run=args.dry_run)
        routing.append(rec)
        counter[rec["decision"] + ":" + rec["reason"]] += 1

    # Summary
    summary = {
        "source": SOURCE_TAG,
        "total": len(routing),
        "live": sum(1 for r in routing if r["decision"] == "live"),
        "draft": sum(1 for r in routing if r["decision"] == "draft"),
        "by_reason": dict(counter),
        "expert_calls": expert_calls,
        "elapsed_s": round(time.time() - t_start, 1),
        "expert_review_elapsed_s": round(time.time() - t_review_start, 1)
            if expert_calls else 0,
        "estimated_cost_usd": round(expert_calls * 0.045, 2),  # 3 judges * ~$0.015
        "defect_distribution": dict(Counter(
            d for r in routing for d in (r.get("defects") or [])
        )),
        "dry_run": args.dry_run,
    }
    if not args.dry_run:
        os.makedirs(os.path.dirname(SUMMARY_OUT), exist_ok=True)
        with open(SUMMARY_OUT, "w") as f:
            json.dump({"summary": summary, "routing": routing},
                      f, indent=2, ensure_ascii=False)
        print(f"\nwrote {SUMMARY_OUT}")
    print("\nSummary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
