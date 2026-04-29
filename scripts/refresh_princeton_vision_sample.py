"""Refresh princeton_sample_review.md with 10 promoted figure items.

Samples 10 Princeton questions that have a figure_ref and status='live',
preferring the ones the vision panel just promoted (i.e. qids in
the vision_review_cache with verdict='live'). Copies the referenced GIF
into princeton_sample_review_assets/ and writes a new section into the
md so the user can inspect the stem, image, options, and verdict.

The script APPENDS a new section rather than overwriting the existing
review doc — that doc predates this pass and contains the full
extraction history the user has been iterating on.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

WT = Path("/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-ac007118")
MAIN = Path("/Users/chiku/Documents/side_projects/gre_with_ai")

os.chdir(str(WT))
sys.path.insert(0, str(WT))

CACHE = WT / "data" / "extracted" / "princeton" / "vision_review_cache.json"
IMG_SRC = WT / "data" / "extracted" / "princeton" / "images"
# Sample md lives in the worktree (tracked as an untracked artifact) so
# the feature branch commit captures the refreshed version without
# touching main.
ASSETS_DST = WT / "princeton_sample_review_assets"
MD_PATH = WT / "princeton_sample_review.md"

from models.database import Question, QuestionOption  # noqa: E402


def main() -> int:
    with open(CACHE) as f:
        cache = json.load(f)
    promoted = [(int(qid), v) for qid, v in cache.items()
                if v.get("verdict") == "live"]
    promoted.sort(key=lambda x: x[0])
    print(f"Newly promoted in cache: {len(promoted)}")

    picks = list(promoted[:10])
    # Top up from earlier-live figure items if we have fewer than 10.
    if len(picks) < 10:
        promoted_ids = {qid for qid, _ in picks}
        earlier = list(
            Question.select().where(
                (Question.source == "princeton_2012")
                & (Question.status == "live")
                & (Question.figure_refs != "[]")
                & (Question.figure_refs.is_null(False))
            ).order_by(Question.id)
        )
        for q in earlier:
            if q.id in promoted_ids:
                continue
            picks.append((q.id, None))
            if len(picks) >= 10:
                break
    print(f"Final sample: {len(picks)} items ({sum(1 for p in picks if p[1])} vision-promoted)")

    ASSETS_DST.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Vision-enabled expert review (2026-04-28 sweep)")
    lines.append("")
    lines.append(
        "The 105 Princeton TC items whose option grids live in an image "
        "(rather than as structured text) were re-reviewed by a 3-judge "
        "vision panel — Opus 4.7 + Sonnet 4.6 + Gemini 3.1 Pro — each "
        "seeing the stem, the marked correct letter, the author's "
        "explanation, and the attached GIF. Promotion rule and rubric "
        "match the text panel: every axis scored >= 4 by >= 2 judges, "
        "spread <= 2, five axes (correctness, clarity, distractor "
        "quality, difficulty match, GRE authenticity)."
    )
    lines.append("")
    lines.append(
        "This sweep covered the 51 items still in `status='draft'` "
        "after the prior Princeton extraction pass. Six promoted to "
        "live; 45 stayed draft. The items below show the newly-promoted "
        f"six followed by four pre-existing figure-backed live items "
        "for contrast — exactly what the runtime question screen "
        "serves."
    )
    lines.append("")

    for i, (qid, verdict) in enumerate(picks, 1):
        q = Question.get(Question.id == qid)
        options = list(
            QuestionOption.select().where(QuestionOption.question_id == qid)
            .order_by(QuestionOption.id)
        )
        ref = q.get_figure_refs()[0] if q.get_figure_refs() else ""
        basename = os.path.basename(ref) if ref else ""
        if basename:
            src = IMG_SRC / basename
            dst = ASSETS_DST / basename
            if src.exists() and not dst.exists():
                shutil.copy(src, dst)

        header_tag = "NEW vision-promoted" if verdict else "pre-existing live"
        lines.append(f"### Sample {i} — qid {qid}  (`{header_tag}`)")
        lines.append("")
        lines.append(f"**Subtype:** `{q.subtype}` &nbsp; "
                     f"**Declared difficulty:** {q.difficulty_target}")
        lines.append("")
        lines.append(f"**Stem:** {q.prompt}")
        lines.append("")
        if basename:
            lines.append(
                f"![options grid](princeton_sample_review_assets/{basename})"
            )
            lines.append("")
        if options:
            correct = next((o.option_label for o in options if o.is_correct), "?")
            lines.append(f"**Options (extracted from image):**")
            lines.append("")
            for o in options:
                mark = "  ← correct" if o.is_correct else ""
                lines.append(f"- **{o.option_label}.** {o.option_text}{mark}")
            lines.append("")
            lines.append(f"**Marked correct:** {correct}")
            lines.append("")
        if verdict:
            mean = verdict.get("axis_mean") or {}
            mins = verdict.get("axis_min") or {}
            maxs = verdict.get("axis_max") or {}
            lines.append("**Vision panel verdict:** live")
            lines.append("")
            lines.append("| axis | mean | range |")
            lines.append("|---|---|---|")
            for ax in ("correctness", "clarity", "distractor_quality",
                       "difficulty_match", "gre_authenticity"):
                m = mean.get(ax, 0)
                rng = f"{mins.get(ax, '?')}–{maxs.get(ax, '?')}"
                lines.append(f"| {ax} | {m:.1f} | {rng} |")
            lines.append("")
            defects = verdict.get("defects") or []
            if defects:
                lines.append(f"**Defects flagged:** {', '.join(defects)}")
                lines.append("")
        if q.explanation:
            expl = q.explanation[:500]
            lines.append(f"**Explanation:** {expl}")
            lines.append("")
        lines.append("---")
        lines.append("")

    section = "\n".join(lines)
    existing = MD_PATH.read_text() if MD_PATH.exists() else ""
    # Remove any prior sweep section so re-running is idempotent.
    marker = "## Vision-enabled expert review (2026-04-28 sweep)"
    if marker in existing:
        existing = existing.split(marker)[0].rstrip()
        # Also strip the preceding "---" separator we'd have inserted.
        if existing.endswith("---"):
            existing = existing[:-3].rstrip()
    MD_PATH.write_text(existing + section)
    print(f"Wrote {MD_PATH} ({len(section)} chars appended)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
