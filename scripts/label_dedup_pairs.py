"""Auto-label the held-out dedup-evaluation pair set with cross-family LLMs
(Phase 1.1, docs/implementation_plan_2026_05_18.md §164-174).

For each pair in data/dedup_eval/candidate_pairs_2026_05_18.csv we ask two
LLMs from different families whether the pair is effectively the same
question. The first-pass labeller is anthropic/claude-opus-4 and the
adjudicator is google/gemini-pro-1.5. Both are routed through
services.llm_service.LLMService (OpenRouter under the hood).

  * agreement (Yes/Yes or No/No or Maybe/Maybe) — accept the consensus.
  * disagreement (e.g. Yes/No, Yes/Maybe, No/Maybe) — final_label = "Maybe"
    with a comment that records the split.
  * any LLM error — final_label = "ERROR"; pair is skipped from F1 stats.

The script is RESUMABLE. If the labeled CSV already exists, we read its
already-labelled pair_ids and skip them. Throttle: 0.5 s between calls.
Cap: 200 pairs per run.

This is a v1 surrogate for human labels, NOT a long-term replacement. The
user can override any row by hand-editing the labeled CSV; downstream
threshold sweeps should treat the resulting labels as noisy ground truth.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import Counter
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models.database import db, Question, QuestionOption, Stimulus  # noqa: E402

CANDIDATE_PATH = PROJECT_ROOT / "data" / "dedup_eval" / "candidate_pairs_2026_05_18.csv"
LABELED_PATH = PROJECT_ROOT / "data" / "dedup_eval" / "labeled_pairs_2026_05_18.csv"
FINDINGS_PATH = PROJECT_ROOT / "research" / "cleanup-2026-05-18" / "workers" / "P1.1" / "findings.jsonl"

CLAUDE_MODEL = "anthropic/claude-opus-4"
# Note: google/gemini-pro-1.5 was retired on OpenRouter (404 No endpoints
# found, verified 2026-05-18). Switched to google/gemini-2.5-pro — still a
# Google-family reasoning model, satisfying the cross-family requirement
# in the implementation plan §164-174.
GEMINI_MODEL = "google/gemini-2.5-pro"

THROTTLE_SECONDS = 0.5
RETRY_COUNT = 3
RETRY_BACKOFF_BASE = 2.0  # seconds

MAX_LLM_PAIRS_PER_RUN = 200
MAX_STIM_CHARS = 1500       # truncate stimulus when prompting
MAX_PROMPT_CHARS = 1500
MAX_OPT_CHARS = 200

LABEL_SCHEMA_KEYS = {"verdict", "confidence", "rationale"}
VALID_VERDICTS = {"Yes", "No", "Maybe"}


SYSTEM_PROMPT = (
    "You are a GRE content-curation expert evaluating whether two questions "
    "are effective duplicates from a learner's perspective. A pair is "
    "considered a duplicate (verdict 'Yes') when showing both items to a "
    "learner would add ZERO additional pedagogical value beyond showing one "
    "of them — same underlying skill, same trap, same answer logic, with at "
    "most surface paraphrasing or trivial number swaps. Pure same-topic "
    "overlap is NOT a duplicate (verdict 'No'). Use 'Maybe' only when you "
    "genuinely cannot decide.\n\n"
    "Respond with strict JSON ONLY (no markdown, no preamble) matching: "
    '{"verdict": "Yes"|"No"|"Maybe", "confidence": <integer 1-5>, '
    '"rationale": "<<=200 chars>"}.'
)


# ── Pair-context fetching ───────────────────────────────────────────


def _strip_html_for_prompt(text):
    """Light HTML cleanup so the prompt stays readable; we keep LaTeX as-is
    since the LLM can parse \\frac{}{}, \\sqrt{}, etc."""
    if not text:
        return ""
    s = unescape(text)
    # Drop obvious tags but keep newlines that <p>...</p> imply
    import re
    s = re.sub(r"<\s*br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    return s.strip()


def _truncate(s, n):
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[:n].rstrip() + " …[truncated]"


def fetch_full_context(qids):
    """Bulk-fetch prompts, stimulus content, and options for a set of qids.

    Returns: dict[qid] -> {prompt, stimulus, options (list of (label, text, is_correct))}
    """
    qids = list(set(qids))
    if not qids:
        return {}
    db.connect(reuse_if_open=True)
    try:
        questions = list(
            Question
            .select(Question.id, Question.measure, Question.subtype, Question.source,
                    Question.stimulus, Question.prompt)
            .where(Question.id.in_(qids))
        )
        stim_ids = [q.stimulus_id for q in questions if q.stimulus_id]
        stim_map = {}
        if stim_ids:
            stim_map = {s.id: s.content for s in
                        Stimulus.select(Stimulus.id, Stimulus.content)
                        .where(Stimulus.id.in_(stim_ids))}
        opts_by_qid = {}
        for opt in (QuestionOption.select()
                    .where(QuestionOption.question.in_(qids))):
            opts_by_qid.setdefault(opt.question_id, []).append(
                (opt.option_label, opt.option_text, bool(opt.is_correct))
            )
    finally:
        db.close()

    out = {}
    for q in questions:
        out[q.id] = {
            "measure": q.measure,
            "subtype": q.subtype,
            "source": q.source,
            "prompt": q.prompt or "",
            "stimulus": stim_map.get(q.stimulus_id, "") if q.stimulus_id else "",
            "options": sorted(opts_by_qid.get(q.id, []), key=lambda x: x[0]),
        }
    return out


def render_question_block(label, ctx):
    """Format a question's full context for the LLM prompt."""
    lines = ["[" + label + "]"]
    lines.append("measure: %s | subtype: %s | source: %s" %
                 (ctx["measure"], ctx["subtype"], ctx["source"]))
    if ctx["stimulus"]:
        lines.append("stimulus:\n" + _truncate(_strip_html_for_prompt(ctx["stimulus"]), MAX_STIM_CHARS))
    lines.append("prompt:\n" + _truncate(_strip_html_for_prompt(ctx["prompt"]), MAX_PROMPT_CHARS))
    if ctx["options"]:
        lines.append("answer choices:")
        for lbl, text, is_correct in ctx["options"]:
            marker = "*" if is_correct else "-"
            lines.append("  %s %s. %s" % (marker, lbl, _truncate(_strip_html_for_prompt(text), MAX_OPT_CHARS)))
    return "\n".join(lines)


def build_user_prompt(ctx_a, ctx_b):
    return (
        "Compare the two GRE questions below. Decide whether they are "
        "effective duplicates (per the system instructions).\n\n"
        + render_question_block("QUESTION A", ctx_a)
        + "\n\n"
        + render_question_block("QUESTION B", ctx_b)
        + "\n\nReturn the JSON now."
    )


# ── LLM calls ───────────────────────────────────────────────────────


def _coerce_label(parsed):
    """Accept a few schema variants. Returns dict or None on bad shape."""
    if not isinstance(parsed, dict):
        return None
    verdict = parsed.get("verdict") or parsed.get("label")
    if verdict in (True, False):
        verdict = "Yes" if verdict else "No"
    if not isinstance(verdict, str):
        return None
    verdict = verdict.strip().capitalize()
    # tolerate "yes" / "no" / "maybe"
    if verdict not in VALID_VERDICTS:
        # tolerate boolean-ish words
        v = verdict.lower()
        mapping = {"true": "Yes", "duplicate": "Yes", "false": "No",
                   "not_duplicate": "No", "unsure": "Maybe", "unknown": "Maybe"}
        if v in mapping:
            verdict = mapping[v]
        else:
            return None
    confidence = parsed.get("confidence")
    try:
        confidence = int(confidence) if confidence is not None else 3
    except (ValueError, TypeError):
        confidence = 3
    rationale = parsed.get("rationale") or ""
    if not isinstance(rationale, str):
        rationale = str(rationale)
    rationale = rationale.replace("\n", " ").replace("\r", " ").strip()[:200]
    return {"verdict": verdict, "confidence": confidence, "rationale": rationale}


def _call_with_retry(llm, system_prompt, user_prompt, model):
    last_err = None
    for attempt in range(RETRY_COUNT):
        try:
            raw = llm.generate(system_prompt, user_prompt, model=model)
            text = (raw or "").strip()
            # Strip optional markdown fences
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                text = "\n".join(lines).strip()
            # Find the JSON object (some models wrap with prose)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                # last-ditch: extract the first {...} block
                start = text.find("{")
                end = text.rfind("}")
                if start != -1 and end != -1 and end > start:
                    parsed = json.loads(text[start:end + 1])
                else:
                    raise
            label = _coerce_label(parsed)
            if label is None:
                raise ValueError("malformed verdict; raw=%s" % text[:200])
            return label, None
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = RETRY_BACKOFF_BASE * (2 ** attempt)
            print("[label_dedup_pairs]   retry %d/%d after error: %s "
                  "(sleeping %.1fs)" % (attempt + 1, RETRY_COUNT, e, wait))
            time.sleep(wait)
    return None, last_err


# ── Findings JSONL ─────────────────────────────────────────────────


def write_finding(record):
    FINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FINDINGS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ── Resumable IO ───────────────────────────────────────────────────


CSV_FIELDNAMES = [
    "pair_id", "qid_a", "qid_b",
    "source_a", "source_b",
    "measure_a", "measure_b",
    "subtype_a", "subtype_b",
    "stem_a_first120", "stem_b_first120",
    "jaccard_5_shingle", "tfidf_cosine",
    "sampling_strata", "bucket",
    "claude_verdict", "claude_rationale", "claude_confidence",
    "gemini_verdict", "gemini_rationale", "gemini_confidence",
    "final_label", "agreement_status", "comment",
]


def load_existing(path):
    if not path.exists():
        return {}, []
    rows = []
    seen_ids = set()
    try:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Don't blow up on missing columns from a partial prior run
                rows.append(row)
                seen_ids.add(int(row["pair_id"]))
    except (ValueError, OSError) as e:
        print("[label_dedup_pairs] WARNING: could not read existing CSV "
              "(%s) — starting fresh" % e)
        return {}, []
    return {r["pair_id"]: r for r in rows}, rows


def write_all(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for r in rows:
            # Fill missing keys with ""
            out = {k: r.get(k, "") for k in CSV_FIELDNAMES}
            writer.writerow(out)


# ── Main ────────────────────────────────────────────────────────────


def derive_final(claude, gemini):
    """Return (final_label, agreement_status, comment)."""
    cv = claude["verdict"] if claude else None
    gv = gemini["verdict"] if gemini else None
    if cv is None or gv is None:
        return "ERROR", "error", "missing one or both LLM verdicts"
    if cv == gv:
        return cv, "agree", ""
    # Disagreement
    return "Maybe", "disagree", "claude=%s; gemini=%s" % (cv, gv)


def main():
    if not CANDIDATE_PATH.exists():
        print("[label_dedup_pairs] FATAL: candidate CSV not found at %s — "
              "run scripts/sample_dedup_pairs.py first" % CANDIDATE_PATH)
        sys.exit(2)

    # Verify API key exists BEFORE doing any work
    from config import load_llm_config
    cfg = load_llm_config()
    if not cfg.get("api_key"):
        msg = ("OPENROUTER_API_KEY missing — set it in .env or "
               "data/llm_config.json before running the labeller. "
               "STOPPING without fabricating labels.")
        print("[label_dedup_pairs] FATAL: " + msg)
        write_finding({
            "sub_q_id": "P1.1.api_key",
            "claim": "OPENROUTER_API_KEY missing; labeller halted",
            "evidence_path": "data/llm_config.json",
            "load_bearing": True,
        })
        sys.exit(2)

    # Load LLM service AFTER the env check so the import doesn't crash
    from services.llm_service import llm_service

    # Load candidates
    with open(CANDIDATE_PATH, encoding="utf-8") as f:
        candidates = list(csv.DictReader(f))
    print("[label_dedup_pairs] loaded %d candidate pairs" % len(candidates))

    # Resume from any prior run
    existing_by_id, _ = load_existing(LABELED_PATH)
    print("[label_dedup_pairs] %d already-labelled pair_ids in %s — "
          "will skip those" % (len(existing_by_id), LABELED_PATH.name))

    # Pre-fetch all qid contexts in one DB hit
    all_qids = []
    for c in candidates:
        all_qids.append(int(c["qid_a"]))
        all_qids.append(int(c["qid_b"]))
    contexts = fetch_full_context(all_qids)

    # Build the working set: prior rows passthrough, new rows we'll label
    output_rows = list(existing_by_id.values())  # may have partial fields
    pairs_to_label = [c for c in candidates
                      if c["pair_id"] not in existing_by_id]
    pairs_to_label = pairs_to_label[:MAX_LLM_PAIRS_PER_RUN]
    print("[label_dedup_pairs] will label %d new pairs (cap=%d)" %
          (len(pairs_to_label), MAX_LLM_PAIRS_PER_RUN))

    # Surfaces ✓ if LLM is unreachable on first call → save candidate set
    # (already on disk) and exit non-zero per the spec.
    first_call_ok = None

    for i, cand in enumerate(pairs_to_label, 1):
        pair_id = cand["pair_id"]
        qa_id = int(cand["qid_a"])
        qb_id = int(cand["qid_b"])
        ctx_a = contexts.get(qa_id)
        ctx_b = contexts.get(qb_id)
        if ctx_a is None or ctx_b is None:
            print("[label_dedup_pairs]   pair %s: missing context "
                  "(qa=%s/%s qb=%s/%s) — marking ERROR" %
                  (pair_id, qa_id, ctx_a is not None, qb_id, ctx_b is not None))
            row = dict(cand)
            row.update({
                "claude_verdict": "", "claude_rationale": "", "claude_confidence": "",
                "gemini_verdict": "", "gemini_rationale": "", "gemini_confidence": "",
                "final_label": "ERROR", "agreement_status": "error",
                "comment": "missing question context",
            })
            output_rows.append(row)
            continue

        user_prompt = build_user_prompt(ctx_a, ctx_b)

        print("[label_dedup_pairs] [%d/%d] pair %s "
              "(qa=%s qb=%s, %s, bucket=%s) ..." %
              (i, len(pairs_to_label), pair_id, qa_id, qb_id,
               cand.get("sampling_strata", "?"), cand.get("bucket", "?")))

        # ── Claude pass
        claude_label, claude_err = _call_with_retry(
            llm_service, SYSTEM_PROMPT, user_prompt, CLAUDE_MODEL)
        if first_call_ok is None:
            first_call_ok = claude_label is not None
            if not first_call_ok:
                # Cannot reach the LLM at all; bail out per spec.
                print("[label_dedup_pairs] FATAL: first LLM call failed "
                      "(err=%s). Candidate set is on disk; aborting "
                      "without fabricating labels." % claude_err)
                write_finding({
                    "sub_q_id": "P1.1.llm_unreachable",
                    "claim": "first Claude call failed: %s" % claude_err,
                    "evidence_path": "scripts/label_dedup_pairs.py",
                    "load_bearing": True,
                })
                # Persist whatever we have (likely just existing rows) and exit
                if output_rows:
                    write_all(LABELED_PATH, output_rows)
                sys.exit(3)

        time.sleep(THROTTLE_SECONDS)

        # ── Gemini pass
        gemini_label, gemini_err = _call_with_retry(
            llm_service, SYSTEM_PROMPT, user_prompt, GEMINI_MODEL)
        time.sleep(THROTTLE_SECONDS)

        if claude_label is None:
            write_finding({
                "sub_q_id": "P1.1.claude_error",
                "claim": "claude call failed for pair %s: %s" % (pair_id, claude_err),
                "evidence_path": "data/dedup_eval/labeled_pairs_2026_05_18.csv",
                "load_bearing": False,
            })
        if gemini_label is None:
            write_finding({
                "sub_q_id": "P1.1.gemini_error",
                "claim": "gemini call failed for pair %s: %s" % (pair_id, gemini_err),
                "evidence_path": "data/dedup_eval/labeled_pairs_2026_05_18.csv",
                "load_bearing": False,
            })

        final_label, agreement, comment = derive_final(claude_label, gemini_label)

        row = dict(cand)
        row.update({
            "claude_verdict": claude_label["verdict"] if claude_label else "",
            "claude_rationale": claude_label["rationale"] if claude_label else "",
            "claude_confidence": claude_label["confidence"] if claude_label else "",
            "gemini_verdict": gemini_label["verdict"] if gemini_label else "",
            "gemini_rationale": gemini_label["rationale"] if gemini_label else "",
            "gemini_confidence": gemini_label["confidence"] if gemini_label else "",
            "final_label": final_label,
            "agreement_status": agreement,
            "comment": comment,
        })
        output_rows.append(row)

        # Periodic checkpoint so a crash doesn't lose work
        if i % 10 == 0:
            write_all(LABELED_PATH, output_rows)

    write_all(LABELED_PATH, output_rows)
    print("[label_dedup_pairs] wrote %d rows to %s" %
          (len(output_rows), LABELED_PATH))

    # ── Acceptance check ───────────────────────────────────────────
    final_counts = Counter(r.get("final_label", "") for r in output_rows)
    agreement_counts = Counter(r.get("agreement_status", "") for r in output_rows)
    error_count = final_counts.get("ERROR", 0)
    yes_agreed = sum(1 for r in output_rows if r.get("final_label") == "Yes"
                     and r.get("agreement_status") == "agree")
    no_agreed = sum(1 for r in output_rows if r.get("final_label") == "No"
                    and r.get("agreement_status") == "agree")
    paraphrase_yes = sum(
        1 for r in output_rows
        if r.get("final_label") == "Yes"
        and float(r.get("jaccard_5_shingle", 0) or 0) < 0.5
    )

    print("\n=== ACCEPTANCE CHECK ===")
    print("  total labelled: %d" % len(output_rows))
    print("  final_label distribution: %s" % dict(final_counts))
    print("  agreement_status distribution: %s" % dict(agreement_counts))
    print("  agreed Yes: %d" % yes_agreed)
    print("  agreed No: %d" % no_agreed)
    print("  paraphrase-clones (final=Yes, jaccard<0.5): %d" % paraphrase_yes)
    print("  ERROR rows: %d" % error_count)

    n = max(1, len(output_rows) - error_count)
    n_agree = sum(1 for r in output_rows if r.get("agreement_status") == "agree")
    print("  cross-family agreement rate: %.1f%% (%d / %d non-error)" %
          (100 * n_agree / n, n_agree, n))

    fail_reasons = []
    if yes_agreed < 30:
        fail_reasons.append(
            "fewer than 30 'Yes' pairs (got %d) — increase the high-Jaccard "
            "sampling fraction in sample_dedup_pairs.py and re-run, or "
            "raise NEAREST_K to widen the search radius" % yes_agreed
        )
    if no_agreed < 30:
        fail_reasons.append(
            "fewer than 30 'No' pairs (got %d) — likely a model-bias issue; "
            "consider tightening the system prompt's 'Yes' criterion" % no_agreed
        )
    if paraphrase_yes < 1:
        fail_reasons.append(
            "no paraphrase-clone Yes pairs (jaccard<0.5) — "
            "lower HIGH_COSINE_CUT in sample_dedup_pairs.py or expand pool B"
        )

    if fail_reasons:
        print("\n  ACCEPTANCE: FAIL")
        for r in fail_reasons:
            print("    - " + r)
        write_finding({
            "sub_q_id": "P1.1.acceptance_fail",
            "claim": "; ".join(fail_reasons),
            "evidence_path": str(LABELED_PATH.relative_to(PROJECT_ROOT)),
            "load_bearing": True,
        })
    else:
        print("\n  ACCEPTANCE: PASS")
        write_finding({
            "sub_q_id": "P1.1.acceptance_pass",
            "claim": "yes_agreed=%d, no_agreed=%d, paraphrase_yes=%d, "
                    "agreement_rate=%.1f%%" %
                    (yes_agreed, no_agreed, paraphrase_yes, 100 * n_agree / n),
            "evidence_path": str(LABELED_PATH.relative_to(PROJECT_ROOT)),
            "load_bearing": True,
        })


if __name__ == "__main__":
    main()
