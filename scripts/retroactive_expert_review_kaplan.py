"""Retroactive expert-jury review for every live Kaplan item in the
worktree seed DB, run in small batches with per-batch git commits.

The batched design exists because the long-running bulk reviewer (~200
items × 3 judges × tens of seconds each) trips the agent harness
watchdog after 600 s of silent output. After every batch we:

  1. Flush a progress line (stdout).
  2. Let the per-call cache (``data/extracted/kaplan/expert_review_cache.json``)
     capture each verdict as we go — so a kill mid-batch loses at most
     one item's quota spend.
  3. ``git add -A && git commit`` (when anything changed) so progress
     survives process restarts.

Delegates the heavy lifting to :mod:`scripts.review_kaplan_questions`,
which contains the proven single-item driver ``review_one_question``.
This wrapper just chunks the work list, prints between batches, and
commits.

Usage::

    venv/bin/python scripts/retroactive_expert_review_kaplan.py
    venv/bin/python scripts/retroactive_expert_review_kaplan.py --batch-size 10 --start-at 40
    venv/bin/python scripts/retroactive_expert_review_kaplan.py --limit 20    # smoke
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


SOURCE_TAG = "kaplan_2024"


def _git(args: List[str]) -> subprocess.CompletedProcess:
    """Run a git command rooted at the worktree repo."""
    return subprocess.run(
        ["git", "-C", _REPO] + args,
        capture_output=True, text=True, check=False,
    )


def _commit_batch(batch_no: int, total_batches: int, reviewed: int,
                  demoted: int, axis_means: Dict[str, float]) -> None:
    """``git add`` + commit with a short message describing batch stats."""
    status = _git(["status", "--porcelain"]).stdout.strip()
    if not status:
        return
    _git(["add", "-A"])
    means_str = ",".join(f"{axis_means.get(ax, 0):.2f}" for ax in (
        "correctness", "clarity", "distractor_quality",
        "difficulty_match", "gre_authenticity",
    ))
    msg = (
        f"Expert review batch {batch_no}/{total_batches} "
        f"(reviewed={reviewed} demoted={demoted} means=[{means_str}])"
    )
    result = _git(["commit", "-m", msg])
    if result.returncode != 0:
        # Non-fatal: print but keep going.
        print(f"  [warn] git commit failed: {result.stderr.strip()}",
              flush=True)


def _axis_means(verdicts: List[Dict[str, Any]]) -> Dict[str, float]:
    from services import expert_review as er
    sums = {ax: 0.0 for ax in er.RUBRIC_AXES}
    counts = {ax: 0 for ax in er.RUBRIC_AXES}
    for v in verdicts:
        means = v.get("axis_mean") or {}
        for ax in er.RUBRIC_AXES:
            if ax in means:
                sums[ax] += float(means[ax])
                counts[ax] += 1
    return {ax: (sums[ax] / counts[ax]) if counts[ax] else 0.0
            for ax in er.RUBRIC_AXES}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--batch-size", type=int, default=20,
                   help="items per batch + commit point (default 20)")
    p.add_argument("--start-at", type=int, default=0,
                   help="skip the first N items of the live-kaplan list")
    p.add_argument("--limit", type=int, default=None,
                   help="cap total items reviewed (smoke testing)")
    p.add_argument("--no-commit", action="store_true",
                   help="skip git commits between batches")
    p.add_argument("--force", action="store_true",
                   help="ignore cache and re-call the panel")
    args = p.parse_args(argv)

    # Bind the Peewee DB to the seed before importing models.
    from scripts.review_kaplan_questions import (
        _bind_seed_db, review_one_question, DEFAULT_CACHE_PATH,
    )
    _bind_seed_db()

    from models.database import Question
    from services import expert_review as er

    cache_path = Path(DEFAULT_CACHE_PATH)

    pre_live_total = (Question.select()
                      .where((Question.source == SOURCE_TAG)
                             & (Question.status == "live"))
                      .count())
    pre_draft = (Question.select()
                 .where((Question.source == SOURCE_TAG)
                        & (Question.status == "draft"))
                 .count())

    qrows = list(
        Question.select(Question.id)
        .where((Question.source == SOURCE_TAG)
               & (Question.status == "live"))
        .order_by(Question.id)
    )
    if args.start_at:
        qrows = qrows[args.start_at:]
    if args.limit is not None:
        qrows = qrows[:args.limit]

    total = len(qrows)
    batch_size = max(1, args.batch_size)
    total_batches = (total + batch_size - 1) // batch_size

    print(f"[retroactive] Pre-run: live={pre_live_total} draft={pre_draft}",
          flush=True)
    print(f"[retroactive] Will review {total} items in {total_batches} "
          f"batch(es) of up to {batch_size} "
          f"(start_at={args.start_at} limit={args.limit})",
          flush=True)
    print(f"[retroactive] Cache file: {cache_path}", flush=True)

    all_verdicts: List[Dict[str, Any]] = []
    demoted_qids: List[int] = []
    spend = 0.0
    started = time.time()

    for b in range(total_batches):
        batch_t0 = time.time()
        lo = b * batch_size
        hi = min(lo + batch_size, total)
        batch_rows = qrows[lo:hi]
        batch_verdicts: List[Dict[str, Any]] = []
        batch_demoted = 0
        batch_cache_hits = 0

        print(f"[retroactive] Batch {b + 1}/{total_batches} "
              f"(items {lo + 1}..{hi}) starting", flush=True)

        for j, row in enumerate(batch_rows, 1):
            item_t0 = time.time()
            try:
                v = review_one_question(row.id, force=args.force,
                                        cache_path=cache_path)
            except Exception as e:
                print(f"    [{lo + j:3d}/{total}] qid={row.id} ERROR: {e!r}",
                      flush=True)
                continue
            batch_verdicts.append({"qid": row.id, **v})
            all_verdicts.append({"qid": row.id, **v})
            if v["verdict"] == "draft":
                demoted_qids.append(row.id)
                batch_demoted += 1
            if v.get("from_cache"):
                batch_cache_hits += 1
            spend += float(v.get("cost_estimate_usd", 0.0))
            tag = "CACHE" if v.get("from_cache") else v["verdict"].upper()
            dt = time.time() - item_t0
            print(f"    [{lo + j:3d}/{total}] qid={row.id:<5d} "
                  f"{tag:<5s} dt={dt:5.1f}s", flush=True)

            # Mid-batch elapsed guardrail — if we're already >8 min into
            # a batch, surface it so the operator knows to shrink.
            if time.time() - batch_t0 > 8 * 60:
                print(f"  [warn] batch {b + 1} exceeded 8 min wall; "
                      f"consider --batch-size 10 on retry", flush=True)

        axis_means = _axis_means(all_verdicts)
        reviewed = len(all_verdicts)
        demoted_total = len(demoted_qids)
        means_str = ",".join(f"{axis_means.get(ax, 0):.2f}" for ax in (
            "correctness", "clarity", "distractor_quality",
            "difficulty_match", "gre_authenticity",
        ))
        dt_batch = time.time() - batch_t0
        print(f"[retroactive] batch {b + 1}/{total_batches} done — "
              f"reviewed={reviewed} demoted={demoted_total} "
              f"means=[{means_str}] "
              f"cache_hits={batch_cache_hits} "
              f"batch_wall={dt_batch:.0f}s "
              f"spend≈${spend:.2f}",
              flush=True)

        if not args.no_commit:
            _commit_batch(b + 1, total_batches, reviewed,
                          demoted_total, axis_means)

    # Post counts.
    post_live = (Question.select()
                 .where((Question.source == SOURCE_TAG)
                        & (Question.status == "live"))
                 .count())
    post_draft = (Question.select()
                  .where((Question.source == SOURCE_TAG)
                         & (Question.status == "draft"))
                  .count())
    axis_means = _axis_means(all_verdicts)
    total_wall = time.time() - started

    # Write a summary JSON alongside the cache.
    summary = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "pre_live": pre_live_total,
        "pre_draft": pre_draft,
        "post_live": post_live,
        "post_draft": post_draft,
        "demoted_count": post_draft - pre_draft,
        "demoted_qids_this_run": demoted_qids,
        "reviewed_count": len(all_verdicts),
        "axis_means": axis_means,
        "spend_estimate_usd": spend,
        "wall_seconds": total_wall,
        "batch_size": batch_size,
        "start_at": args.start_at,
    }
    summary_path = cache_path.with_name("expert_review_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print()
    print(f"[retroactive] DONE  pre live/draft={pre_live_total}/{pre_draft}"
          f"  post live/draft={post_live}/{post_draft}  "
          f"(demoted-this-run={len(demoted_qids)})", flush=True)
    print(f"[retroactive] Wall={total_wall:.0f}s  Spend≈${spend:.2f}",
          flush=True)
    print("[retroactive] Per-axis means:", flush=True)
    for ax, mean in axis_means.items():
        print(f"    {ax:24s} {mean:.2f}", flush=True)
    print(f"[retroactive] Summary written to {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
