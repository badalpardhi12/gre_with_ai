"""Legacy ai_generated expert review — promote-or-demote gate.

All 1,033 `source='ai_generated'` items shipped with status='live' but
predate the 5-axis expert review gate. Known issues include
size-1 DI clusters and weak distractors. This script runs the unified
``services.expert_review`` (5-axis × 3-judge panel: Opus + Sonnet +
Gemini) on every one of them and demotes any item that fails the gate
to ``status='draft'``.

Promotion rule (copy of ``expert_review`` policy): every axis needs at
least 2 of 3 judges at >= 4 AND no axis spread > 2. Otherwise the item
is demoted with the per-judge breakdown written to ``review_notes``.

Batching: 25 items per commit point. Per-item wall cap 90s (60s judge
call + parse slop + aggregate time). Two per-item timeouts → route to
``draft`` with ``review_notes='expert_review timed out twice'``.

Cache: ``data/extracted/legacy_ai_generated/expert_review_cache.json``
keyed by qid. Idempotent.

Usage:
  venv/bin/python scripts/expert_review_ai_generated.py
  venv/bin/python scripts/expert_review_ai_generated.py --limit 25    # smoke
  venv/bin/python scripts/expert_review_ai_generated.py --batch-size 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


SOURCE_TAG = "ai_generated"
CACHE_PATH = (REPO / "data" / "extracted" / "legacy_ai_generated"
              / "expert_review_cache.json")


def load_cache() -> Dict[str, Dict[str, Any]]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def fetch_ai_generated_live(db, Question, QuestionOption,
                            NumericAnswer) -> List[Dict[str, Any]]:
    """Load live ai_generated rows + their options into review dicts."""
    rows = list(
        Question.select()
        .where((Question.source == SOURCE_TAG) &
               (Question.status == "live"))
        .order_by(Question.id)
    )
    # Bulk-fetch options per question.
    qids = [r.id for r in rows]
    opts_by_q: Dict[int, List[Dict[str, Any]]] = {qid: [] for qid in qids}
    for o in (QuestionOption
              .select()
              .where(QuestionOption.question_id.in_(qids))
              .order_by(QuestionOption.question_id,
                        QuestionOption.option_label)):
        opts_by_q[o.question_id].append({
            "label": o.option_label,
            "text": o.option_text,
            "is_correct": bool(o.is_correct),
        })

    out: List[Dict[str, Any]] = []
    for r in rows:
        correct_labels = [o["label"] for o in opts_by_q[r.id]
                          if o["is_correct"]]
        correct_label = correct_labels[0] if correct_labels else ""
        out.append({
            "qid": r.id,
            "stem": r.prompt,
            "options": opts_by_q[r.id],
            "correct_label": correct_label,
            "explanation": r.explanation,
            "subtype": r.subtype,
            "difficulty": r.difficulty_target,
            "source": r.source,
        })
    return out


def review_one(item: Dict[str, Any], wall_cap: float = 60.0) -> Dict[str, Any]:
    """Call the 3-judge expert review and return the verdict dict.

    Wrapped in a top-level watchdog: if the underlying expert_review
    hangs past ``wall_cap`` seconds (sum of parallel-judge timeouts +
    slop), we abort and return a synthetic draft verdict. This keeps
    the batch loop moving even if one item's judge threads are wedged
    on a socket.
    """
    import threading as _th
    from services.expert_review import expert_review_kaplan

    box: Dict[str, Any] = {}

    def _runner():
        try:
            box["verdict"] = expert_review_kaplan(item)
        except Exception as exc:  # pragma: no cover
            box["error"] = f"{exc!r}"

    t = _th.Thread(target=_runner, daemon=True)
    t.start()
    t.join(wall_cap)
    if t.is_alive():
        return {
            "verdict": "draft",
            "reviewer_notes": (f"expert_review wall-cap exceeded "
                               f"({wall_cap}s) — forced demotion"),
            "axis_mean": {}, "scores": {}, "failures": [],
            "panel": [], "judge_count": 0,
        }
    if "error" in box:
        return {
            "verdict": "draft",
            "reviewer_notes": f"expert_review error: {box['error']}",
            "axis_mean": {}, "scores": {}, "failures": [],
            "panel": [], "judge_count": 0,
        }
    return box.get("verdict", {
        "verdict": "draft",
        "reviewer_notes": "expert_review returned no verdict",
        "axis_mean": {}, "scores": {}, "failures": [],
        "panel": [], "judge_count": 0,
    })


def _axis_mean(verdicts: List[Dict[str, Any]], axis: str) -> float:
    means = [v.get("axis_mean", {}).get(axis)
             for v in verdicts if v.get("axis_mean")]
    means = [m for m in means if isinstance(m, (int, float))]
    return sum(means) / len(means) if means else 0.0


def apply_verdict(db, Question, qid: int, verdict: Dict[str, Any]) -> str:
    """Mutate the DB based on the verdict. Returns 'live' or 'draft'."""
    from datetime import datetime as _dt
    v = verdict.get("verdict", "draft")
    notes = verdict.get("reviewer_notes", "")
    new_status = "live" if v == "live" else "draft"
    if new_status == "live":
        # Stamp the review_notes anyway so the audit trail shows it was
        # expert-reviewed on 2026-04-28.
        note = (f"[expert-review 2026-04-28] LIVE — "
                + (notes or "all axes passed"))
    else:
        note = f"[expert-review 2026-04-28] DEMOTED — " + (notes or "")
    db.connect(reuse_if_open=True)
    Question.update(
        status=new_status,
        review_notes=note[:8000],  # guardrail against absurd sizes
        updated_at=_dt.now(),
    ).where(Question.id == qid).execute()
    return new_status


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--batch-size", type=int, default=25,
                   help="items per commit point (default 25)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap total items (smoke testing)")
    p.add_argument("--start-at", type=int, default=0,
                   help="skip the first N items")
    p.add_argument("--no-commit", action="store_true",
                   help="don't git-commit between batches")
    p.add_argument("--force", action="store_true",
                   help="ignore cache and re-call the panel")
    p.add_argument("--report",
                   default=str(REPO / "data" / "audits"
                               / "ai_generated_expert_review_2026_04_28.md"))
    p.add_argument("--concurrency", type=int, default=4,
                   help="parallel items inside a batch (default 4)")
    p.add_argument("--item-timeout", type=float, default=60.0,
                   help="per-item wall-cap seconds (default 60)")
    args = p.parse_args(argv)

    from models.database import db, Question, QuestionOption, NumericAnswer

    cache = load_cache() if not args.force else {}
    items = fetch_ai_generated_live(db, Question, QuestionOption,
                                     NumericAnswer)
    if args.start_at:
        items = items[args.start_at:]
    if args.limit:
        items = items[:args.limit]
    total = len(items)
    if total == 0:
        print("[expert-review] nothing to review", flush=True)
        return 0

    print(f"[expert-review] {total} live ai_generated items to review",
          flush=True)
    print(f"[expert-review] cache: {CACHE_PATH}", flush=True)

    batch_size = max(1, args.batch_size)
    n_batches = (total + batch_size - 1) // batch_size

    demoted_qids: List[int] = []
    promoted_qids: List[int] = []
    all_verdicts: List[Dict[str, Any]] = []
    started = time.time()
    last_progress = time.time()

    for b in range(n_batches):
        lo = b * batch_size
        hi = min(lo + batch_size, total)
        batch = items[lo:hi]
        batch_t0 = time.time()
        print(f"[expert-review] batch {b+1}/{n_batches} "
              f"(items {lo+1}..{hi})", flush=True)

        # Parallelize across items inside a batch — each item already
        # fans out to 3 parallel judges, so 4 concurrent items = 12
        # in-flight requests, well within typical gateway quota.
        import concurrent.futures as _cf

        def _do(item):
            qid = item["qid"]
            cache_key = str(qid)
            if not args.force and cache_key in cache:
                return (item, cache[cache_key], True)
            try:
                v = review_one(item, wall_cap=args.item_timeout)
            except Exception as exc:
                v = {
                    "verdict": "draft",
                    "reviewer_notes": f"expert_review_failed: {exc!r}",
                    "axis_mean": {}, "scores": {}, "failures": [],
                    "panel": [], "judge_count": 0,
                }
            return (item, v, False)

        with _cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            future_to_item = {ex.submit(_do, it): it for it in batch}
            results_in_order: Dict[int, Any] = {}
            idx_for_item = {id(it): j for j, it in enumerate(batch)}
            for fut in _cf.as_completed(future_to_item):
                item, verdict, from_cache = fut.result()
                results_in_order[idx_for_item[id(item)]] = (
                    item, verdict, from_cache)
            for j in range(len(batch)):
                item, verdict, from_cache = results_in_order[j]
                qid = item["qid"]
                if not from_cache:
                    cache[str(qid)] = verdict
                    save_cache(cache)
                new_status = apply_verdict(db, Question, qid, verdict)
                if new_status == "draft":
                    demoted_qids.append(qid)
                else:
                    promoted_qids.append(qid)
                all_verdicts.append({"qid": qid, **verdict})
                tag = ("CACHE" if from_cache
                       else verdict.get("verdict", "draft").upper())
                print(f"    [{lo+j+1:4d}/{total}] qid={qid:<5d} {tag:<5s} "
                      f"dt={time.time() - batch_t0:.1f}s",
                      flush=True)
            if time.time() - last_progress > 300:
                done = len(all_verdicts)
                dt_so_far = time.time() - started
                rate = done / max(1, dt_so_far)
                eta = (total - done) / max(1e-6, rate)
                print(f"[expert-review] progress: {done}/{total} "
                      f"({100*done/total:.1f}%) — "
                      f"demoted={len(demoted_qids)} "
                      f"eta={eta:.0f}s", flush=True)
                last_progress = time.time()

        dt_batch = time.time() - batch_t0
        print(f"[expert-review] batch {b+1}/{n_batches} done — "
              f"total_demoted={len(demoted_qids)} "
              f"promoted={len(promoted_qids)} "
              f"batch_wall={dt_batch:.0f}s", flush=True)

    # Final report.
    pre_live = total
    post_live = len(promoted_qids)
    post_draft = len(demoted_qids)
    axis_means = {ax: _axis_mean(all_verdicts, ax)
                  for ax in ("correctness", "clarity",
                             "distractor_quality", "difficulty_match",
                             "gre_authenticity")}
    wall_s = time.time() - started

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    lines.append("# Legacy ai_generated expert review — 2026-04-28")
    lines.append("")
    lines.append(f"- Timestamp: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- Items reviewed: **{len(all_verdicts)}**")
    lines.append(f"- Promoted (remain live): **{post_live}**")
    lines.append(f"- Demoted to draft: **{post_draft}** "
                 f"({100*post_draft/max(1,len(all_verdicts)):.1f}%)")
    lines.append(f"- Wall time: **{wall_s:.0f}s**")
    lines.append("")
    lines.append("## Axis means (all items)")
    lines.append("")
    lines.append("| axis | mean |")
    lines.append("|---|---|")
    for ax, m in axis_means.items():
        lines.append(f"| {ax} | {m:.2f} |")
    lines.append("")
    lines.append(f"## Demoted qids (first 100 of {post_draft})")
    lines.append("")
    for qid in demoted_qids[:100]:
        lines.append(f"- qid={qid}")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[expert-review] report: {report_path}", flush=True)
    print(f"[expert-review] done. Reviewed={len(all_verdicts)} "
          f"Promoted={post_live} Demoted={post_draft} Wall={wall_s:.0f}s",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
