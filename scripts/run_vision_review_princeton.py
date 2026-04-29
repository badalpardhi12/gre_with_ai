"""Run vision-enabled expert review over Princeton items with figure_refs.

For each Princeton Question row in status='draft' whose figure_refs is
non-empty, assemble the text + image payload and run the 3-judge vision
panel. Promotion rule is the same as the text panel: every axis must
score >= 4 from >= 2 judges, with no axis spread > 2.

Outputs
-------
* DB: promoted rows flipped to status='live'; all rows get the verdict
  embedded in review_notes (JSON blob).
* Cache: data/extracted/princeton/vision_review_cache.json — keyed by
  Question.id so re-running the script picks up where it left off.
* Progress file: data/extracted/princeton/vision_review_progress.log
  written line-by-line so long-running invocations can be watched
  from a second shell without relying on pipe flushing.

Usage
-----
    venv/bin/python scripts/run_vision_review_princeton.py
    venv/bin/python scripts/run_vision_review_princeton.py --limit 15
    venv/bin/python scripts/run_vision_review_princeton.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

WT = Path("/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-ac007118")
os.chdir(str(WT))
sys.path.insert(0, str(WT))

from services.vision_expert_review import (  # noqa: E402
    vision_expert_review,
    _media_type_for,
    VISION_AXES,
)

IMAGE_DIR = WT / "data" / "extracted" / "princeton" / "images"
CACHE_PATH = WT / "data" / "extracted" / "princeton" / "vision_review_cache.json"
PROGRESS_PATH = WT / "data" / "extracted" / "princeton" / "vision_review_progress.log"
BATCH_FLUSH_EVERY = 5


def _log(msg):
    """Write one line to progress log and echo to stdout."""
    stamp = datetime.utcnow().isoformat(timespec="seconds")
    line = f"[{stamp}] {msg}"
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_PATH, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def _load_cache():
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _flush_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    tmp.replace(CACHE_PATH)


def _build_question_dict(q, options_rows):
    options = [
        {"label": o.option_label, "text": o.option_text,
         "is_correct": bool(o.is_correct)}
        for o in options_rows
    ]
    correct = next((o["label"] for o in options if o["is_correct"]), "")
    return {
        "stem": q.prompt,
        "correct_label": correct,
        "explanation": q.explanation,
        "subtype": q.subtype,
        "difficulty_target": q.difficulty_target,
        "source": q.source,
        "options": options,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Max items this run (default: all)")
    parser.add_argument("--batch-size", type=int, default=BATCH_FLUSH_EVERY)
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute verdicts but don't write DB")
    parser.add_argument("--force", action="store_true",
                        help="Ignore cache; re-review everything")
    args = parser.parse_args()

    from models.database import Question, QuestionOption  # noqa: E402

    qs = list(
        Question.select().where(
            (Question.source == "princeton_2012")
            & (Question.status == "draft")
            & (Question.figure_refs != "[]")
            & (Question.figure_refs.is_null(False))
        ).order_by(Question.id)
    )
    _log(f"Candidate Princeton drafts with figure_refs: {len(qs)}")
    if args.limit:
        qs = qs[: args.limit]
        _log(f"Limited to {len(qs)} by --limit")

    cache = {} if args.force else _load_cache()
    promoted = 0
    stayed_draft = 0
    skipped_cached = 0
    skipped_missing_image = 0
    axis_totals = {ax: [] for ax in VISION_AXES}
    mispairings = []

    for idx, q in enumerate(qs, 1):
        key = str(q.id)
        if not args.force and key in cache:
            skipped_cached += 1
            cached = cache[key]
            if cached.get("verdict") == "live":
                promoted += 1
            else:
                stayed_draft += 1
            for ax in VISION_AXES:
                mean = (cached.get("axis_mean") or {}).get(ax)
                if mean is not None:
                    axis_totals[ax].append(mean)
            continue

        refs = q.get_figure_refs()
        if not refs:
            skipped_missing_image += 1
            continue
        img_path = IMAGE_DIR / os.path.basename(refs[0])
        if not img_path.exists():
            _log(f"  [{idx}] qid={q.id} MISSING image {img_path}")
            skipped_missing_image += 1
            continue
        image_bytes = img_path.read_bytes()
        media_type = _media_type_for(str(img_path))

        options_rows = list(
            QuestionOption.select().where(QuestionOption.question_id == q.id)
            .order_by(QuestionOption.id)
        )
        qdict = _build_question_dict(q, options_rows)

        _log(f"  [{idx}/{len(qs)}] qid={q.id} starting review ({img_path.name})")
        t0 = time.time()
        try:
            verdict = vision_expert_review(
                qdict, image_bytes=image_bytes, media_type=media_type,
            )
        except Exception as exc:
            _log(f"  [{idx}] qid={q.id} review raised: {exc}")
            verdict = {
                "verdict": "draft", "escalated": True, "scores": {},
                "axis_mean": {}, "axis_min": {}, "axis_max": {},
                "defects": ["other"],
                "reviewer_notes": f"review_failed: {exc!r}",
                "judge_notes": [], "judge_count": 0,
                "panel": [], "failures": [], "cost_estimate_usd": 0,
            }
        dt = time.time() - t0

        # Detect mispairing: any judge's read_options dict is all
        # empty/"unreadable" while correct label not in the option set.
        judge_reads = []
        for jn in (verdict.get("judge_notes") or []):
            ro = jn.get("read_options") or {}
            judge_reads.append(set(ro.keys()))
        if judge_reads and all(not r for r in judge_reads):
            mispairings.append(q.id)

        # Record axis means for the summary.
        for ax in VISION_AXES:
            m = (verdict.get("axis_mean") or {}).get(ax)
            if m is not None:
                axis_totals[ax].append(m)

        cache[key] = {
            "verdict": verdict.get("verdict"),
            "escalated": verdict.get("escalated"),
            "axis_mean": verdict.get("axis_mean"),
            "axis_min": verdict.get("axis_min"),
            "axis_max": verdict.get("axis_max"),
            "defects": verdict.get("defects"),
            "reviewer_notes": verdict.get("reviewer_notes"),
            "failures": verdict.get("failures"),
            "judge_count": verdict.get("judge_count"),
            "panel": verdict.get("panel"),
            "image_ref": refs[0],
            "reviewed_at": datetime.utcnow().isoformat(),
            "elapsed_s": round(dt, 2),
        }

        new_status = (
            "live" if verdict.get("verdict") == "live" else "draft"
        )
        if verdict.get("verdict") == "live":
            promoted += 1
        else:
            stayed_draft += 1

        if not args.dry_run:
            # Embed the verdict into review_notes so the rendered review
            # md can display per-judge scores later.
            review_blob = json.dumps({
                "axis_mean": verdict.get("axis_mean"),
                "axis_min": verdict.get("axis_min"),
                "axis_max": verdict.get("axis_max"),
                "defects": verdict.get("defects"),
                "panel": verdict.get("panel"),
                "escalated": verdict.get("escalated"),
                "reviewer_notes": verdict.get("reviewer_notes"),
                "reviewed_at": cache[key]["reviewed_at"],
                "stage": "vision_review_2026_04_28",
            }, ensure_ascii=False)
            Question.update(
                status=new_status,
                review_notes=review_blob,
            ).where(Question.id == q.id).execute()

        mean_summary = ", ".join(
            f"{ax}={(verdict.get('axis_mean') or {}).get(ax, 0):.1f}"
            for ax in VISION_AXES
        )
        _log(
            f"  [{idx}/{len(qs)}] qid={q.id} verdict={verdict.get('verdict')} "
            f"({dt:.1f}s) {mean_summary}"
        )

        if idx % args.batch_size == 0:
            _flush_cache(cache)
            _log(
                f"  -- flushed batch ({idx}/{len(qs)}) "
                f"promoted={promoted} draft={stayed_draft}"
            )

    _flush_cache(cache)
    _log("=" * 60)
    _log(f"Reviewed:        {len(qs)}")
    _log(f"Cached (skipped):{skipped_cached}")
    _log(f"Missing images:  {skipped_missing_image}")
    _log(f"Promoted to live:{promoted}")
    _log(f"Stayed draft:    {stayed_draft}")
    _log("Per-axis mean of means (across reviewed items):")
    for ax in VISION_AXES:
        vals = axis_totals[ax]
        if vals:
            m = sum(vals) / len(vals)
            _log(f"  {ax:20s} {m:.2f} (n={len(vals)})")
        else:
            _log(f"  {ax:20s} (no data)")
    if mispairings:
        _log(
            f"Suspected mis-pairings (all judges returned empty "
            f"read_options): {mispairings[:15]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
