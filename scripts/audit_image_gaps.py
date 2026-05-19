#!/usr/bin/env python3
"""Image-gap audit (Phase 5.2).

Read-only scan of the live question pool to catalogue items that *should*
have a figure attached but don't (or have a broken pointer). The output
CSV feeds Phase 6.4's figure-synthesis batch.

Scope
-----
Only items with subtype in {data_interp, mcq_single, mcq_multi, qc,
numeric_entry} are scanned, and only when at least one of these heuristics
fires:

  * ``topic == 'geometry'``
  * ``subtopic LIKE '%geometry%'``
  * stem matches the spatial-language regex
    ``\\bthe figure (above|below)\\b | \\bthe diagram\\b | \\bin the diagram\\b
       | \\bin the figure\\b | \\bgraph above\\b | \\bgraph below\\b
       | \\btable above\\b  | \\btable below\\b``

Output
------
``data/audits/image_gaps_<DATE>.csv`` with columns:

    qid, source, subtype, topic, subtopic, stem_first120,
    expected_figure_type, current_state, recommended_action

``current_state`` is one of:
    has_figure          - figure_refs is non-null AND points to an existing file
    missing_figure_refs - figure_refs is null/empty
    broken_pointer      - figure_refs is non-empty but no file exists

``expected_figure_type``:
    chart_or_table      - subtype = data_interp
    geometric_diagram   - geometry topic/subtopic
    table               - stem mentions "table above|below"
    chart               - stem mentions "graph above|below"
    unknown             - none of the above

``recommended_action``:
    ok                                - has_figure
    retire_no_figure_dependency       - short stem, no spatial language (rare)
    synth_geom_figure                 - everything else (default for caution)

Usage
-----
    venv/bin/python scripts/audit_image_gaps.py
    venv/bin/python scripts/audit_image_gaps.py --output /path/to/out.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Importable regardless of cwd.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import DATA_DIR  # noqa: E402
from models.database import Question, init_db  # noqa: E402

# Reuse the resolver from the migration script so they agree on what
# "exists" means.
from scripts.migrate_figure_refs_to_images_dir import (  # noqa: E402
    _resolve_existing,
)


# ── Constants ───────────────────────────────────────────────────────

ELIGIBLE_SUBTYPES = {
    "data_interp", "mcq_single", "mcq_multi", "qc", "numeric_entry",
}

# Compiled once; word-boundary anchors matter — bare "diagram" without
# context shouldn't trigger.
SPATIAL_RE = re.compile(
    r"\bthe figure (above|below)\b"
    r"|\bthe diagram\b"
    r"|\bin the diagram\b"
    r"|\bin the figure\b"
    r"|\bgraph above\b"
    r"|\bgraph below\b"
    r"|\btable above\b"
    r"|\btable below\b",
    re.IGNORECASE,
)

TABLE_RE = re.compile(r"\btable\s+(?:above|below)\b", re.IGNORECASE)
GRAPH_RE = re.compile(r"\bgraph\s+(?:above|below)\b", re.IGNORECASE)


# ── Heuristics ──────────────────────────────────────────────────────

def _is_geometry_item(topic: str, subtopic: str) -> bool:
    if (topic or "").strip().lower() == "geometry":
        return True
    return "geometry" in (subtopic or "").lower()


def _expected_figure_type(question) -> str:
    if question.subtype == "data_interp":
        return "chart_or_table"
    if _is_geometry_item(question.topic, question.subtopic):
        return "geometric_diagram"
    prompt = question.prompt or ""
    if TABLE_RE.search(prompt):
        return "table"
    if GRAPH_RE.search(prompt):
        return "chart"
    return "unknown"


def _has_existing_figure(question) -> Tuple[bool, bool]:
    """Return ``(has_any_ref, all_resolve)``.

    has_any_ref: figure_refs has at least one non-empty string.
    all_resolve: every entry resolves to a file on disk (only meaningful
                 if ``has_any_ref`` is True).
    """
    try:
        refs = json.loads(question.figure_refs or "[]")
    except (ValueError, TypeError):
        refs = []
    if not isinstance(refs, list):
        refs = []
    refs = [r for r in refs if isinstance(r, str) and r.strip()]
    if not refs:
        return False, False
    for entry in refs:
        if _resolve_existing(entry, question.source or "") is None:
            return True, False
    return True, True


def _current_state(question) -> str:
    has_ref, all_resolve = _has_existing_figure(question)
    if not has_ref:
        return "missing_figure_refs"
    if not all_resolve:
        return "broken_pointer"
    return "has_figure"


def _looks_short_and_self_contained(prompt: str) -> bool:
    """Heuristic for 'stem doesn't actually need a figure'.

    Conservative: only fires for SHORT prompts (< 220 chars) that
    contain NO spatial language at all and don't mention figures, charts,
    tables, diagrams, or graphs. Default to ``False`` (= keep figure)
    when uncertain — Phase 6.4 prefers a wasted synth attempt over a
    silently-retired item.
    """
    if not prompt or len(prompt) > 220:
        return False
    low = prompt.lower()
    for kw in ("figure", "diagram", "graph", "chart", "table",
               "above", "below", "shown", "shaded"):
        if kw in low:
            return False
    return True


def _recommended_action(question, current_state: str) -> str:
    if current_state == "has_figure":
        return "ok"
    if _looks_short_and_self_contained(question.prompt or ""):
        return "retire_no_figure_dependency"
    return "synth_geom_figure"


def _eligible(question) -> bool:
    if question.subtype not in ELIGIBLE_SUBTYPES:
        return False
    if _is_geometry_item(question.topic, question.subtopic):
        return True
    if SPATIAL_RE.search(question.prompt or ""):
        return True
    return False


# ── Main ────────────────────────────────────────────────────────────

CSV_HEADER = [
    "qid", "source", "subtype", "topic", "subtopic", "stem_first120",
    "expected_figure_type", "current_state", "recommended_action",
]


def audit(output_path: Path) -> Tuple[int, Dict[Tuple[str, str, str], int]]:
    """Walk the live pool and emit the CSV.

    Returns ``(rows_written, summary)`` where ``summary`` keys are
    ``(source, subtype, current_state)`` triples.
    """
    init_db()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows_written = 0
    summary: Dict[Tuple[str, str, str], int] = defaultdict(int)

    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)

        for q in Question.select().where(Question.status == "live"):
            if not _eligible(q):
                continue
            state = _current_state(q)
            row = [
                q.id,
                q.source or "",
                q.subtype or "",
                q.topic or "",
                q.subtopic or "",
                (q.prompt or "")[:120].replace("\n", " "),
                _expected_figure_type(q),
                state,
                _recommended_action(q, state),
            ]
            writer.writerow(row)
            rows_written += 1
            summary[(q.source or "", q.subtype or "", state)] += 1

    return rows_written, summary


def _format_summary(summary: Dict[Tuple[str, str, str], int]) -> str:
    if not summary:
        return "(no eligible rows)"
    width_src = max(len(k[0]) for k in summary)
    width_st = max(len(k[1]) for k in summary)
    width_state = max(len(k[2]) for k in summary)
    lines = ["",
             f"{'source':<{width_src}}  {'subtype':<{width_st}}  "
             f"{'current_state':<{width_state}}  count"]
    lines.append("-" * (width_src + width_st + width_state + 12))
    for (src, st, state), count in sorted(summary.items()):
        lines.append(
            f"{src:<{width_src}}  {st:<{width_st}}  "
            f"{state:<{width_state}}  {count:>5}"
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    # Plan §368 fixes the output filename so it's referenced in the
    # cleanup-baseline benchmark consistently across re-runs.
    default_out = DATA_DIR / "audits" / "image_gaps_2026_05_18.csv"

    parser = argparse.ArgumentParser(
        description="Catalogue figure-bearing items missing or broken figures.",
    )
    parser.add_argument(
        "--output", "-o", type=Path, default=default_out,
        help=f"Output CSV path (default: {default_out}).",
    )
    args = parser.parse_args(argv)

    rows, summary = audit(args.output)

    print(f"Wrote {rows} eligible rows -> {args.output}")
    print(_format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
