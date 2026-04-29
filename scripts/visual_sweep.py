"""Visual coherence sweep over live question pool.

Content-only audit: loads each live question + its linked stimulus + options
+ attached figure image (if any), and asks Sonnet 4.6 (vision) whether the
item renders as a coherent, answerable GRE question. Low-confidence /
incoherent verdicts are escalated to Opus 4.7 as a second judge.

Safe to interrupt — the per-qid cache at
``data/extracted/visual_sweep_cache.json`` means a rerun skips items already
audited (unless ``--force`` is passed). DB demotion only happens when BOTH
judges agree on a high-confidence structural issue.

Usage:
    venv/bin/python scripts/visual_sweep.py                 # full run
    venv/bin/python scripts/visual_sweep.py --batch-size 20 # default
    venv/bin/python scripts/visual_sweep.py --start-at 0    # resume point
    venv/bin/python scripts/visual_sweep.py --limit 50      # smoke
    venv/bin/python scripts/visual_sweep.py --dry-run       # no DB writes
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

WT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WT_ROOT))

from services._llm_gateway import FloodgateClient, MODEL_SONNET, MODEL_OPUS  # noqa: E402
from models.database import (  # noqa: E402
    db, Question, QuestionOption, Stimulus,
)

CACHE_PATH = WT_ROOT / "data" / "extracted" / "visual_sweep_cache.json"
PROGRESS_PATH = WT_ROOT / "data" / "extracted" / "visual_sweep_progress.json"

# Allowed issue tags the model may use. Anything else gets coerced to "other".
ALLOWED_ISSUES = {
    "blank_stimulus",
    "missing_options",
    "wrong_option_count",
    "caption_inlined",
    "wrong_figure",
    "broken_stem_latex",
    "duplicate_options",
    "ambiguous_stem",
    "stem_truncated",
    "empty_explanation",
    "unrelated_distractors",
    "other",
}

# Issues we will auto-demote live → draft for (when both judges agree and
# high confidence). Caption inlining is being fixed by a parallel agent, so
# log-only. Broken LaTeX is a render-layer bug, not item-level. Empty
# explanation / duplicates / unrelated_distractors are quality concerns but
# the spec asks to demote ambiguous_stem + unrelated_distractors.
DEMOTE_ISSUES = {
    "blank_stimulus",
    "missing_options",
    "wrong_option_count",
    "stem_truncated",
    "wrong_figure",
    "ambiguous_stem",
    "unrelated_distractors",
}

# Subtypes that legitimately have no stimulus (standalone item stems).
NO_STIMULUS_SUBTYPES = {
    "tc", "se", "qc", "mcq_single", "mcq_multi", "numeric_entry",
}

# Subtypes where a non-empty options list is required.
NEEDS_OPTIONS_SUBTYPES = {
    "tc", "se", "qc", "mcq_single", "mcq_multi",
    "rc_single", "rc_multi", "rc_select_passage", "data_interp",
}


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    tmp.replace(CACHE_PATH)


def resolve_image_path(ref: str) -> Path | None:
    """Return a filesystem path for a figure_ref, or None if not found."""
    candidates: list[Path] = []
    # Normalize: strip leading "images/"
    stripped = re.sub(r"^images/", "", ref)

    # 1) Princeton extracted images (via worktree symlink)
    candidates.append(WT_ROOT / "data" / "princeton_images" / stripped)
    # 2) Kaplan extracted images
    candidates.append(WT_ROOT / "data" / "extracted" / "kaplan" / "images" / stripped)
    # 3) Generic data/images/
    candidates.append(WT_ROOT / "data" / "images" / stripped)
    # 4) Original ref, interpreted relative to data/
    candidates.append(WT_ROOT / "data" / ref)
    # 5) Printable glob fallback through extracted sub-dirs
    for sub in ("princeton", "kaplan", "manhattan"):
        candidates.append(WT_ROOT / "data" / "extracted" / sub / "images" / stripped)

    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return c
        except OSError:
            continue
    return None


def clean_html(text: str) -> str:
    """Very light HTML→plain conversion; we want the judge to see raw content."""
    if not text:
        return ""
    # Strip script/style
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text,
                  flags=re.DOTALL | re.IGNORECASE)
    # Convert <br> and block ends to newlines
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*(p|div|li|tr)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 18] + "\n[... truncated ...]"


def build_prompt(qrow: dict, options: list[dict], stim: dict | None) -> str:
    """Build the judge prompt body."""
    parts = []
    parts.append("You're auditing a GRE practice question. Rate whether this "
                 "renders as a coherent, answerable question.\n")
    parts.append(f"Subtype: {qrow['subtype']}")
    parts.append(f"Measure: {qrow['measure']}")
    parts.append(f"Source: {qrow['source']}")
    parts.append("")
    parts.append("Question stem:")
    parts.append(truncate(clean_html(qrow.get("prompt", "")), 4000))
    parts.append("")
    if stim is not None:
        parts.append(f"Stimulus type: {stim.get('stimulus_type', '?')}")
        parts.append(f"Stimulus title: {stim.get('title', '')}")
        parts.append("Stimulus content:")
        body = clean_html(stim.get("content", ""))
        parts.append(truncate(body or "(empty)", 6000))
        parts.append("")
    elif qrow.get("stimulus_id"):
        parts.append("Stimulus: (MISSING — stimulus_id set but row not found)")
        parts.append("")
    else:
        parts.append("Stimulus: (none — standalone item)")
        parts.append("")
    if qrow.get("figure_refs_list"):
        parts.append(f"Attached figure files: {qrow['figure_refs_list']}")
        parts.append("(See attached image for the figure contents.)")
        parts.append("")
    parts.append("Options:")
    if not options:
        parts.append("  (no options rows)")
    else:
        for opt in options:
            label = opt.get("option_label", "?")
            text = truncate(clean_html(opt.get("option_text", "")), 300)
            mark = " [CORRECT]" if opt.get("is_correct") else ""
            parts.append(f"  {label}. {text}{mark}")
    parts.append("")
    parts.append("Correct labels: " + ", ".join(
        o["option_label"] for o in options if o.get("is_correct")
    ) or "(none)")
    parts.append("")
    parts.append("Explanation (truncated):")
    parts.append(truncate(clean_html(qrow.get("explanation", "")), 1500)
                 or "(empty)")
    parts.append("")
    parts.append(
        "Return a single JSON object, no prose, no markdown fence. Schema:\n"
        "{\n"
        '  "coherent": true|false,\n'
        '  "issues": [choose any that apply from: blank_stimulus, '
        "missing_options, wrong_option_count, caption_inlined, wrong_figure, "
        "broken_stem_latex, duplicate_options, ambiguous_stem, "
        "stem_truncated, empty_explanation, unrelated_distractors, other],\n"
        '  "confidence": "high" | "medium" | "low",\n'
        '  "reasoning": "one or two concise sentences"\n'
        "}\n"
        "If the item is coherent, return coherent=true and issues=[]."
    )
    return "\n".join(parts)


def build_messages(prompt_text: str, image_bytes_list: list[tuple[bytes, str]]):
    """Build anthropic messages with optional images."""
    content = []
    for img_bytes, media_type in image_bytes_list:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(img_bytes).decode("utf-8"),
            },
        })
    content.append({"type": "text", "text": prompt_text})
    return [{"role": "user", "content": content}]


def parse_verdict(raw: str) -> dict:
    """Parse JSON; on failure, mark low-confidence other."""
    text = (raw or "").strip()
    # Strip fences if present
    if text.startswith("```"):
        lines = [ln for ln in text.split("\n") if not ln.strip().startswith("```")]
        text = "\n".join(lines)
    # Find first { ... last }
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        text = text[first : last + 1]
    try:
        obj = json.loads(text)
    except Exception:
        return {
            "coherent": True,  # can't judge; assume ok (no demote)
            "issues": ["other"],
            "confidence": "low",
            "reasoning": f"parse_failure: {raw[:200]}",
            "_parse_failed": True,
        }
    # Normalize
    issues_in = obj.get("issues") or []
    issues = []
    for iss in issues_in:
        s = str(iss).strip().lower().replace("-", "_").replace(" ", "_")
        if s in ALLOWED_ISSUES:
            issues.append(s)
        else:
            issues.append("other")
    obj["issues"] = sorted(set(issues))
    obj["coherent"] = bool(obj.get("coherent", True))
    conf = str(obj.get("confidence", "medium")).lower()
    obj["confidence"] = conf if conf in {"high", "medium", "low"} else "medium"
    obj["reasoning"] = str(obj.get("reasoning", ""))[:800]
    return obj


def call_judge_with_retry(client: FloodgateClient, model: str, messages,
                          max_tokens: int = 512, attempts: int = 3) -> str:
    """Call judge with backoff. On 2nd timeout, raise."""
    backoff = [2, 5, 10, 20, 40]
    for i in range(attempts):
        try:
            return client.call_anthropic(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                max_retries=1,  # gateway's internal retries
            )
        except Exception as e:
            if i == attempts - 1:
                raise
            wait = backoff[min(i, len(backoff) - 1)]
            sys.stderr.write(f"[judge-retry] {type(e).__name__} {e} → sleep {wait}s\n")
            sys.stderr.flush()
            time.sleep(wait)
    raise RuntimeError("unreachable")


def estimate_cost(n_sonnet: int, n_opus: int) -> float:
    # Rough: avg 1.2k input + 200 output per call.
    # Sonnet 4.6: $3/$15 per Mt in/out.
    # Opus 4.7: $15/$75 per Mt in/out.
    sonnet_cost = n_sonnet * (1200 * 3.0 / 1_000_000 + 200 * 15.0 / 1_000_000)
    opus_cost = n_opus * (1200 * 15.0 / 1_000_000 + 200 * 75.0 / 1_000_000)
    return sonnet_cost + opus_cost


def _db_retry(fn, attempts: int = 4):
    """Retry a DB callable through transient 'malformed' / 'locked' errors."""
    last_err = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # peewee.DatabaseError, OperationalError, etc.
            msg = str(e).lower()
            last_err = e
            if "malformed" in msg or "locked" in msg or "busy" in msg:
                try:
                    db.close()
                except Exception:
                    pass
                try:
                    db.connect(reuse_if_open=True)
                    db.execute_sql("REINDEX;")
                except Exception:
                    pass
                time.sleep(1 + i * 2)
                continue
            raise
    raise last_err  # type: ignore[misc]


def load_question_row(qid: int) -> dict | None:
    q = _db_retry(lambda: Question.get_or_none(Question.id == qid))
    if q is None:
        return None
    opts = _db_retry(lambda: list(
        QuestionOption.select()
        .where(QuestionOption.question == q)
        .order_by(QuestionOption.option_label)
    ))
    opts_dict = [
        {
            "option_label": o.option_label,
            "option_text": o.option_text,
            "is_correct": bool(o.is_correct),
        }
        for o in opts
    ]
    stim = None
    if q.stimulus_id:
        s = _db_retry(lambda: Stimulus.get_or_none(Stimulus.id == q.stimulus_id))
        if s is not None:
            stim = {
                "stimulus_type": s.stimulus_type,
                "title": s.title,
                "content": s.content,
            }
    figure_refs = q.get_figure_refs()
    refs_str = ", ".join(fr.get("filename") if isinstance(fr, dict) else str(fr)
                         for fr in figure_refs)
    return {
        "id": q.id,
        "subtype": q.subtype,
        "measure": q.measure,
        "source": q.source,
        "prompt": q.prompt,
        "explanation": q.explanation,
        "status": q.status,
        "stimulus_id": q.stimulus_id,
        "figure_refs_list": refs_str,
        "figure_refs_raw": figure_refs,
        "_stim": stim,
        "_options": opts_dict,
    }


def load_images_for_question(qrow: dict) -> list[tuple[bytes, str]]:
    out: list[tuple[bytes, str]] = []
    for fr in qrow.get("figure_refs_raw", []) or []:
        ref = fr.get("filename") if isinstance(fr, dict) else str(fr)
        if not ref:
            continue
        p = resolve_image_path(ref)
        if p is None:
            continue
        try:
            b = p.read_bytes()
        except Exception:
            continue
        mt, _ = mimetypes.guess_type(str(p))
        if not mt or not mt.startswith("image/"):
            # Anthropic only supports jpeg/png/gif/webp; GIF → image/gif
            ext = p.suffix.lower().lstrip(".")
            mt = f"image/{ext}" if ext in {"jpeg", "jpg", "png", "gif", "webp"} else "image/png"
        if mt == "image/jpg":
            mt = "image/jpeg"
        # Cap: 1 image max per question for bandwidth.
        out.append((b, mt))
        if len(out) >= 1:
            break
    return out


def judge_question(client: FloodgateClient, qrow: dict) -> dict:
    """Run Sonnet primary + optional Opus escalation for one question."""
    stem_prompt = build_prompt(
        qrow,
        qrow["_options"],
        qrow["_stim"],
    )
    images = load_images_for_question(qrow)
    messages = build_messages(stem_prompt, images)

    t0 = time.time()
    raw_sonnet = call_judge_with_retry(client, MODEL_SONNET, messages,
                                       max_tokens=512, attempts=3)
    sonnet_latency = time.time() - t0
    sonnet_verdict = parse_verdict(raw_sonnet)

    # Escalate if Sonnet said not coherent OR was low-confidence.
    opus_verdict = None
    opus_latency = None
    needs_opus = (not sonnet_verdict["coherent"]) or (
        sonnet_verdict["confidence"] == "low"
    )
    if needs_opus:
        t0 = time.time()
        try:
            raw_opus = call_judge_with_retry(client, MODEL_OPUS, messages,
                                             max_tokens=512, attempts=3)
            opus_latency = time.time() - t0
            opus_verdict = parse_verdict(raw_opus)
        except Exception as e:
            opus_latency = time.time() - t0
            opus_verdict = {
                "coherent": True,
                "issues": ["other"],
                "confidence": "low",
                "reasoning": f"opus_call_failed: {type(e).__name__}: {e}",
                "_error": True,
            }

    return {
        "qid": qrow["id"],
        "source": qrow["source"],
        "subtype": qrow["subtype"],
        "has_image": bool(images),
        "sonnet": sonnet_verdict,
        "sonnet_latency_s": round(sonnet_latency, 2),
        "opus": opus_verdict,
        "opus_latency_s": round(opus_latency, 2) if opus_latency else None,
        "judged_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


def decide_action(verdict: dict) -> tuple[str, list[str]]:
    """Return (action, demoteable_issues)."""
    sonnet = verdict["sonnet"]
    opus = verdict.get("opus")

    if sonnet["coherent"] and opus is None:
        return "keep_live", []
    if sonnet["coherent"] and opus is not None and opus["coherent"]:
        return "keep_live", []

    # At least one judge flagged. Compute consensus demoteable issues.
    sonnet_issues = set(sonnet.get("issues", []))
    opus_issues = set(opus.get("issues", [])) if opus else set()

    # Require agreement for demotion: both judges must flag not coherent AND
    # share at least one DEMOTE_ISSUES tag at high confidence (at least one
    # of the judges high, not both low).
    if opus is None:
        # Should not happen given escalation rule, but be safe.
        return "log_only", []

    both_not_coherent = (not sonnet["coherent"]) and (not opus["coherent"])
    shared_demote = (sonnet_issues | opus_issues) & DEMOTE_ISSUES
    confident = (
        sonnet["confidence"] == "high" or opus["confidence"] == "high"
    )

    if both_not_coherent and shared_demote and confident:
        return "demote", sorted(shared_demote)
    return "log_only", []


def flush_progress(msg: str) -> None:
    print(msg, flush=True)
    sys.stdout.flush()


def ensure_review_notes_append(q: Question, note: str) -> None:
    existing = q.review_notes or ""
    if note in existing:
        return
    sep = "\n---\n" if existing else ""
    q.review_notes = (existing + sep + note)[:16000]


def demote_question(qid: int, verdict: dict, issues: list[str],
                    dry_run: bool) -> bool:
    def _do():
        q = Question.get_or_none(Question.id == qid)
        if q is None or q.status != "live":
            return False
        note_payload = {
            "source": "visual_sweep_2026_04_28",
            "action": "live→draft",
            "issues": issues,
            "sonnet": verdict["sonnet"],
            "opus": verdict.get("opus"),
            "timestamp": verdict["judged_at"],
        }
        note = "[visual_sweep 2026-04-28] " + json.dumps(note_payload,
                                                        ensure_ascii=False)
        if dry_run:
            return True
        with db.atomic():
            q.status = "draft"
            ensure_review_notes_append(q, note)
            q.updated_at = datetime.utcnow()
            q.save()
        return True
    return _db_retry(_do)


def pick_live_qids() -> list[int]:
    qids = _db_retry(lambda: [
        q.id for q in Question.select(Question.id)
        .where(Question.status == "live")
        .order_by(Question.id)
    ])
    return qids


def run_sweep(args) -> int:
    db.connect(reuse_if_open=True)
    # Proactively rebuild indices — a sibling worktree may have produced
    # transient corruption that surfaces mid-sweep otherwise.
    try:
        db.execute_sql("REINDEX;")
    except Exception as e:
        flush_progress(f"[warn] REINDEX failed: {e}")

    all_qids = pick_live_qids()
    total = len(all_qids)
    flush_progress(f"[sweep] live pool total: {total}")

    cache = load_cache()
    flush_progress(f"[sweep] cache hits: {len(cache)}")

    if args.limit:
        all_qids = all_qids[: args.limit]
    if args.start_at:
        all_qids = [q for q in all_qids if q >= args.start_at]

    # Skip already-cached unless --force.
    queue = []
    for qid in all_qids:
        if not args.force and str(qid) in cache:
            continue
        queue.append(qid)
    flush_progress(f"[sweep] queue after cache-skip: {len(queue)}")

    client = FloodgateClient()
    batch_size = args.batch_size
    batches = [queue[i : i + batch_size] for i in range(0, len(queue), batch_size)]
    n_batches = len(batches)

    demote_count = 0
    log_count = 0
    keep_count = 0
    issue_counter: dict[str, int] = {}
    sonnet_calls = 0
    opus_calls = 0

    t_start = time.time()

    for bi, batch in enumerate(batches, 1):
        batch_t0 = time.time()
        batch_demotes = 0
        batch_flags = 0
        batch_issues = []

        for qid in batch:
            qrow = load_question_row(qid)
            if qrow is None:
                continue

            last_progress = time.time()
            try:
                verdict = judge_question(client, qrow)
            except Exception as e:
                flush_progress(f"[err] qid={qid} {type(e).__name__}: {e}")
                verdict = {
                    "qid": qid,
                    "source": qrow["source"],
                    "subtype": qrow["subtype"],
                    "has_image": bool(qrow.get("figure_refs_list")),
                    "sonnet": {
                        "coherent": True,
                        "issues": ["other"],
                        "confidence": "low",
                        "reasoning": f"judge_failed: {type(e).__name__}",
                    },
                    "sonnet_latency_s": 0.0,
                    "opus": None,
                    "opus_latency_s": None,
                    "judged_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "_error": True,
                }

            sonnet_calls += 1
            if verdict.get("opus") is not None:
                opus_calls += 1

            action, issues = decide_action(verdict)
            verdict["action"] = action
            verdict["demote_issues"] = issues

            if action == "demote":
                if demote_question(qid, verdict, issues, args.dry_run):
                    demote_count += 1
                    batch_demotes += 1
                    for iss in issues:
                        issue_counter[iss] = issue_counter.get(iss, 0) + 1
                        batch_issues.append(iss)
            elif action == "log_only":
                log_count += 1
                batch_flags += 1
                sonnet_iss = verdict["sonnet"].get("issues") or []
                for iss in sonnet_iss:
                    issue_counter[iss] = issue_counter.get(iss, 0) + 1
                    batch_issues.append(iss)
            else:
                keep_count += 1

            cache[str(qid)] = verdict

            # Force a progress print on >5 min silence mid-call — we do so by
            # flushing between items; a batch of 20 should take well under 5m
            # at ~3s per Sonnet + occasional Opus.

        save_cache(cache)

        cost = estimate_cost(sonnet_calls, opus_calls)
        batch_elapsed = time.time() - batch_t0
        total_elapsed = time.time() - t_start

        flush_progress(
            f"[batch {bi}/{n_batches}] audited={len(batch)} "
            f"flagged={batch_flags} demoted={batch_demotes} "
            f"top_issues={','.join(batch_issues[:3]) or '-'} "
            f"batch_s={batch_elapsed:.1f} total_min={total_elapsed/60:.1f} "
            f"est_cost=${cost:.3f} "
            f"cum_keep={keep_count} cum_log={log_count} cum_demote={demote_count}"
        )

    flush_progress("")
    flush_progress(f"[sweep-done] audited={sonnet_calls} keep={keep_count} "
                   f"log_only={log_count} demote={demote_count}")
    flush_progress(f"[sweep-done] sonnet_calls={sonnet_calls} opus_calls={opus_calls} "
                   f"est_cost=${estimate_cost(sonnet_calls, opus_calls):.2f}")
    flush_progress("[sweep-done] issue histogram:")
    for iss, n in sorted(issue_counter.items(), key=lambda kv: -kv[1]):
        flush_progress(f"  {iss}: {n}")

    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--start-at", type=int, default=0,
                    help="resume from this qid")
    ap.add_argument("--limit", type=int, default=0,
                    help="debug cap on queue size")
    ap.add_argument("--force", action="store_true",
                    help="ignore cache and re-judge everything")
    ap.add_argument("--dry-run", action="store_true",
                    help="skip DB writes")
    args = ap.parse_args()
    return run_sweep(args)


if __name__ == "__main__":
    sys.exit(main())
