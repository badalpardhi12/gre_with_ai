#!/usr/bin/env python3
"""
Manhattan 5lb Chapter 24 — Data Interpretation cluster re-extraction.
====================================================================

Phase 4 · Task #19.  See research/gre-repetitiveness-roadmap/report.md §1.2
and docs/implementation_plan_2026_05_12.md.

Why this exists
---------------
The original Manhattan 5lb pipeline imported each Chapter 24 DI item as a
solo singleton — every question got its own one-off Stimulus row, which
defeats the whole point of Data Interpretation sets (one chart / table,
multiple linked questions).  The Phase 4 P4.P3 stimulus-id cooldown
reduced the repetitiveness blast-radius but didn't fix the data.  This
re-extraction ships the chapter as proper clusters: ONE Stimulus per
set, with ``stimulus_id`` shared across 3-6 sibling questions.

Input
-----
``data/extracted/manhattan/ch24_raw.json`` — the structured-JSON dump
produced by the upstream LLM extraction pass.  Each question row carries
``stimulus_text`` ONLY for the first question of its set; subsequent
questions under the same chart leave it ``null``.  That null/non-null
signal IS the set-boundary marker the parser keys off of.

Output
------
For each set:
  * 1 Stimulus row — ``stimulus_type`` in {graph, table, passage} inferred
    from the stimulus_text content, title "Manhattan 5lb Ch24 Set N".
  * N Question rows — all sharing the new ``stimulus_id``, subtype
    ``data_interp``, source ``manhattan_5lb_2018_ch24_di_v2``,
    status ``candidate``.
  * QuestionOption rows, one per choice.

Idempotency
-----------
``(source, source_anchor)`` is the unique key.  ``source_anchor`` is
``"set{N}_q{M}"`` where N is the 1-indexed set number and M is the raw
``q_number`` from the JSON.  Re-running the script skips rows that are
already present.

Usage
-----
    # dry-run (no DB writes)
    venv/bin/python scripts/extract_manhattan_di_clusters.py --dry-run

    # real import
    venv/bin/python scripts/extract_manhattan_di_clusters.py
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SOURCE_TAG = "manhattan_5lb_2018_ch24_di_v2"
DEFAULT_CH24_JSON = ROOT / "data" / "extracted" / "manhattan" / "ch24_raw.json"
SET_TITLE_PREFIX = "Manhattan 5lb Ch24 Set"

# Legacy qids this re-extraction is meant to replace.  Emitted in the
# summary so the operator knows which rows to retire via migration 029.
LEGACY_QIDS_TO_RETIRE = (
    3598, 3603, 3606, 3610, 3614, 3616, 3620, 3624, 3628, 3630, 3634,
)


# ── Parser ────────────────────────────────────────────────────────────

@dataclass
class DIQuestion:
    q_number: int
    page: Optional[int]
    subtype: str
    prompt: str
    options: List[Tuple[str, str]]
    correct_label: Optional[str]
    explanation: str


@dataclass
class DICluster:
    set_index: int  # 1-based
    stimulus_text: str
    stimulus_type: str  # "graph" | "table" | "passage"
    questions: List[DIQuestion] = field(default_factory=list)

    @property
    def title(self) -> str:
        return f"{SET_TITLE_PREFIX} {self.set_index}"


def _infer_stimulus_type(stim: str) -> str:
    """Classify the Stimulus by its description keywords.

    We keep this deliberately simple: 3 buckets (graph/table/passage)
    because that's what ``Stimulus.stimulus_type`` accepts.  Any of
    "pie chart", "bar chart", "line graph", "scatter plot", "graph",
    "chart" -> graph.  Any explicit markdown-style "|" table or
    "table" keyword -> table.  Otherwise -> passage (which covers the
    grid-style listings that aren't markdown tables).
    """
    s = (stim or "").lower()
    # Markdown table rows are a strong signal: "| Foo | Bar |".
    if "\n|" in stim or stim.startswith("|"):
        return "table"
    if "following table" in s or "(table):" in s or "table:" in s:
        return "table"
    if any(kw in s for kw in (
        "pie chart", "bar chart", "line graph", "scatter plot",
        "bar graph", "line chart", "following graph", "following chart",
        "graph", "chart", "plot",
    )):
        return "graph"
    return "passage"


def parse_sets(raw_questions: List[Dict]) -> List[DICluster]:
    """Split raw ch24 question dicts into DI clusters.

    Boundary signal: a question row whose ``stimulus_text`` is non-empty
    starts a new cluster; subsequent rows with null/empty stimulus_text
    accumulate under the current cluster.  A question that arrives
    before any stimulus has been seen is silently dropped (there is no
    set for it to belong to).
    """
    clusters: List[DICluster] = []
    current: Optional[DICluster] = None
    for rq in raw_questions:
        stim = rq.get("stimulus_text")
        if stim:
            current = DICluster(
                set_index=len(clusters) + 1,
                stimulus_text=stim,
                stimulus_type=_infer_stimulus_type(stim),
            )
            clusters.append(current)
        if current is None:
            continue
        current.questions.append(DIQuestion(
            q_number=int(rq["q_number"]),
            page=rq.get("page"),
            subtype=str(rq.get("subtype") or "mcq_single"),
            prompt=str(rq.get("prompt") or ""),
            options=[(o["label"], o["text"]) for o in (rq.get("options") or [])],
            correct_label=rq.get("correct_label"),
            explanation=str(rq.get("explanation") or ""),
        ))
    return clusters


def load_ch24_questions(path: Path) -> List[Dict]:
    """Load the raw ch24 JSON dump.

    Accepts either the ``{"questions": [...]}`` wrapper shape (the
    current production file) or a bare list (handy for tests).
    """
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict) and "questions" in data:
        return list(data["questions"])
    if isinstance(data, list):
        return data
    raise ValueError(f"unexpected ch24 JSON shape: {type(data).__name__}")


# ── DB import ─────────────────────────────────────────────────────────

def import_to_db(clusters: List[DICluster],
                 min_cluster_size: int = 2) -> Tuple[int, int, int]:
    """Insert clusters into the DB.  Returns (stimuli_inserted,
    questions_inserted, questions_skipped).

    Clusters with fewer than ``min_cluster_size`` questions are skipped
    entirely — the whole point of this re-extraction is to produce
    multi-question sets, so a lone question (which would be another
    singleton) is dropped.
    """
    from models.database import (
        db, init_db, Question, QuestionOption, Stimulus,
    )
    init_db()
    db.connect(reuse_if_open=True)

    stim_n = 0
    q_inserted = 0
    q_skipped = 0

    with db.atomic():
        for cluster in clusters:
            if len(cluster.questions) < min_cluster_size:
                q_skipped += len(cluster.questions)
                continue

            # Find-or-create the cluster's Stimulus by title.  Title
            # uniqueness inside this source is enforced by construction
            # (we build it from set_index), so this stays idempotent.
            stim_row = (
                Stimulus
                .select()
                .where(Stimulus.title == cluster.title)
                .first()
            )
            if stim_row is None:
                stim_row = Stimulus.create(
                    stimulus_type=cluster.stimulus_type,
                    title=cluster.title,
                    content=cluster.stimulus_text,
                )
                stim_n += 1

            for dq in cluster.questions:
                anchor = f"set{cluster.set_index}_q{dq.q_number}"
                exists = (
                    Question
                    .select()
                    .where((Question.source == SOURCE_TAG)
                           & (Question.source_anchor == anchor))
                    .first()
                )
                if exists:
                    q_skipped += 1
                    continue

                provenance = {
                    "pipeline": "manhattan_5lb_2018_ch24_di_v2",
                    "set_index": cluster.set_index,
                    "q_number": dq.q_number,
                    "page": dq.page,
                    "raw_subtype": dq.subtype,
                }

                q = Question.create(
                    measure="quant",
                    subtype="data_interp",
                    stimulus=stim_row,
                    prompt=dq.prompt,
                    difficulty_target=3,
                    time_target_seconds=150,  # matches engine DI default
                    concept_tags=json.dumps(["manhattan_5lb", "ch24",
                                             "data_interp"]),
                    source=SOURCE_TAG,
                    source_anchor=anchor,
                    provenance="imported",
                    status="candidate",  # reviewed before going live
                    provenance_json=json.dumps(provenance),
                    explanation=dq.explanation,
                    figure_refs=json.dumps([]),
                )

                for label, otext in dq.options:
                    QuestionOption.create(
                        question=q,
                        option_label=label,
                        option_text=otext,
                        is_correct=(label == dq.correct_label),
                    )

                q_inserted += 1

    return stim_n, q_inserted, q_skipped


# ── Summarizer ────────────────────────────────────────────────────────

def summarize(clusters: List[DICluster]) -> Dict[str, object]:
    sizes = [len(c.questions) for c in clusters]
    by_type: Dict[str, int] = {}
    for c in clusters:
        by_type[c.stimulus_type] = by_type.get(c.stimulus_type, 0) + 1
    usable = [c for c in clusters if len(c.questions) >= 2]
    total_q = sum(sizes)

    print(f"\nManhattan 5lb Ch24 DI re-extraction")
    print(f"  sets parsed:       {len(clusters)}")
    print(f"  usable (>=2 Q):    {len(usable)}")
    print(f"  total questions:   {total_q}")
    print(f"  questions / set:   min={min(sizes) if sizes else 0} "
          f"max={max(sizes) if sizes else 0}")
    print(f"  by stimulus_type:  {dict(sorted(by_type.items()))}")
    for c in clusters:
        print(f"    Set {c.set_index:2d} ({c.stimulus_type:>7}, "
              f"{len(c.questions)}Q): {c.stimulus_text[:60]!r}")
    print(f"\n  legacy qids to retire: {list(LEGACY_QIDS_TO_RETIRE)}")

    return {
        "sets": len(clusters),
        "usable_sets": len(usable),
        "total_questions": total_q,
        "by_stimulus_type": by_type,
        "sizes": sizes,
    }


# ── CLI ───────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-extract Manhattan 5lb Ch24 DI items as proper "
                    "1-chart-3+Q clusters.",
    )
    parser.add_argument(
        "--ebook", type=Path, default=None,
        help="Path to the Manhattan 5lb ebook (PDF/EPUB).  Currently "
             "unused — the pipeline reads from the already-extracted "
             "ch24 JSON dump.  Accepted for forward compatibility.",
    )
    parser.add_argument(
        "--ch24-json", type=Path, default=DEFAULT_CH24_JSON,
        help=f"Path to the ch24 raw JSON "
             f"(default: {DEFAULT_CH24_JSON.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and summarize but do not write to the DB.",
    )
    parser.add_argument(
        "--min-cluster-size", type=int, default=2,
        help="Drop clusters with fewer than this many questions "
             "(default: 2).  DI sets should be >=2 by definition.",
    )
    args = parser.parse_args(argv)

    src = args.ch24_json
    if not src.exists():
        print(f"ERROR: ch24 JSON not found: {src}", file=sys.stderr)
        return 2

    print(f"Manhattan 5lb Ch24 re-extraction — source: {src}")
    raw = load_ch24_questions(src)
    clusters = parse_sets(raw)
    summary = summarize(clusters)

    if args.dry_run:
        print("\n[dry-run] no DB writes performed")
        print(f"[dry-run-summary] {json.dumps(summary, sort_keys=True)}")
        return 0

    stim_n, q_n, q_skip = import_to_db(
        clusters, min_cluster_size=args.min_cluster_size)
    print(f"\nStimuli inserted:  {stim_n}")
    print(f"Questions inserted: {q_n}")
    print(f"Questions skipped (already present / below threshold): {q_skip}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
