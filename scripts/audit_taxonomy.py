#!/usr/bin/env python3
"""Phase 2.1 taxonomy audit (read-only).

Quantifies how many live ``Question`` rows have NULL/empty/inconsistent
``(topic, subtopic, question_type)`` so Phase 2.2 (LLM-judge taxonomy
normalization) knows how big the backfill batch is.

Read-only. Writes a JSON report to ``data/audits/`` and prints a
human-readable summary. Does NOT mutate any rows in either DB.

Usage::

    venv/bin/python scripts/audit_taxonomy.py

Plan reference: docs/implementation_plan_2026_05_18.md, Phase 2.1.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Project root on sys.path so ``models`` imports cleanly when the
# script is invoked directly from any cwd.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.database import Question, db  # noqa: E402
from models.taxonomy import (  # noqa: E402
    AWA_TAXONOMY,
    QUANT_TAXONOMY,
    VERBAL_TAXONOMY,
)

OUTPUT_PATH = ROOT / "data" / "audits" / "taxonomy_audit_2026_05_18.json"

# ── Subtype ↔ canonical question_type ───────────────────────────────
# Phase 2.1 prompt: ``subtype`` is the storage-format ('qc', 'mcq_single',
# etc.); ``question_type`` is supposed to be the canonical label
# ('quantitative_comparison', 'multiple_choice', ...). In practice, when
# the legacy importers populated ``question_type`` at all, they often just
# echoed the subtype string. We capture BOTH the canonical mapping and
# the echo as accepted, and flag everything else as a mismatch.
CANONICAL_QUESTION_TYPE_FOR_SUBTYPE: Dict[str, Set[str]] = {
    # Quant
    "qc": {"quantitative_comparison", "qc"},
    "mcq_single": {"multiple_choice", "mcq_single", "mcq_short_answer"},
    "mcq_multi": {"multiple_choice_select_all", "mcq_multi"},
    "numeric_entry": {"numeric_entry"},
    "data_interp": {"data_interpretation", "data_interp"},
    # Verbal
    "tc": {"text_completion", "tc"},
    "se": {"sentence_equivalence", "se"},
    "rc_single": {"reading_comprehension", "rc_single"},
    "rc_multi": {"reading_comprehension_multi", "rc_multi"},
    "rc_select_passage": {"select_in_passage", "rc_select_passage"},
    # AWA
    "awa_issue": {"analyze_an_issue", "awa_issue"},
}


def _build_subtopic_allowlist() -> Set[str]:
    """Union of every subtopic listed in the three taxonomy dicts."""
    seen: Set[str] = set()
    for taxonomy in (QUANT_TAXONOMY, VERBAL_TAXONOMY, AWA_TAXONOMY):
        for _topic, td in taxonomy.items():
            for sub in td.get("subtopics", {}):
                seen.add(sub)
    return seen


def _is_empty(val) -> bool:
    """A column is 'missing' if it is NULL or an empty/whitespace-only string."""
    if val is None:
        return True
    if isinstance(val, str) and val.strip() == "":
        return True
    return False


def _subtype_question_type_mismatch(
    subtype: Optional[str], question_type: Optional[str]
) -> bool:
    """``True`` iff a non-empty ``question_type`` contradicts ``subtype``.

    An empty ``question_type`` is its own anomaly (``missing_question_type``)
    and is NOT counted as a mismatch — otherwise the mismatch bucket would
    just shadow the missing bucket and obscure real contradictions.
    """
    if _is_empty(question_type):
        return False
    if subtype is None:
        # No subtype to compare against — treat as inconclusive, not a mismatch.
        return False
    accepted = CANONICAL_QUESTION_TYPE_FOR_SUBTYPE.get(subtype)
    if accepted is None:
        # Subtype isn't in our mapping table at all. Conservative: not a
        # mismatch (we have no opinion). The subtype itself is unusual and
        # will surface elsewhere in operational dashboards.
        return False
    return question_type not in accepted


def run_audit() -> Dict[str, Any]:
    """Read every live ``Question`` and compile the audit report."""
    allowlist = _build_subtopic_allowlist()

    missing_topic: List[int] = []
    missing_subtopic: List[int] = []
    missing_question_type: List[int] = []
    subtopic_not_in_taxonomy: List[Dict[str, Any]] = []
    subtype_question_type_mismatch: List[Dict[str, Any]] = []

    by_source: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "topic_distribution": defaultdict(int),
            "subtopic_distribution": defaultdict(int),
            "question_type_distribution": defaultdict(int),
            "missing_count": 0,
            "live_count": 0,
        }
    )

    needs_phase_2_2: Set[int] = set()

    db.connect(reuse_if_open=True)
    try:
        live_q = (
            Question.select(
                Question.id,
                Question.measure,
                Question.subtype,
                Question.topic,
                Question.subtopic,
                Question.question_type,
                Question.source,
            )
            .where(Question.status == "live")
        )

        live_total = 0
        for q in live_q:
            live_total += 1
            qid = q.id
            source = q.source or "unknown"
            src_bucket = by_source[source]
            src_bucket["live_count"] += 1
            src_bucket["topic_distribution"][q.topic or ""] += 1
            src_bucket["subtopic_distribution"][q.subtopic or ""] += 1
            src_bucket["question_type_distribution"][q.question_type or ""] += 1

            row_has_anomaly = False

            if _is_empty(q.topic):
                missing_topic.append(qid)
                row_has_anomaly = True

            if _is_empty(q.subtopic):
                missing_subtopic.append(qid)
                row_has_anomaly = True
            elif q.subtopic not in allowlist:
                subtopic_not_in_taxonomy.append(
                    {"qid": qid, "subtopic": q.subtopic, "topic": q.topic}
                )
                row_has_anomaly = True

            if _is_empty(q.question_type):
                missing_question_type.append(qid)
                # NOTE: ``question_type`` is empty for ~95% of rows in the
                # current bank — i.e. the column is effectively unused.
                # Phase 2.2 should DECIDE whether to backfill it from
                # ``subtype`` or to formally retire the column. Until that
                # decision is made we still count missing-question_type
                # rows toward Phase-2.2 work so the headline includes them.
                row_has_anomaly = True
            elif _subtype_question_type_mismatch(q.subtype, q.question_type):
                subtype_question_type_mismatch.append(
                    {
                        "qid": qid,
                        "subtype": q.subtype,
                        "question_type": q.question_type,
                    }
                )
                row_has_anomaly = True

            if row_has_anomaly:
                src_bucket["missing_count"] += 1
                needs_phase_2_2.add(qid)
    finally:
        if not db.is_closed():
            db.close()

    # Convert defaultdicts to plain dicts for JSON output, sort distributions
    # by frequency desc so the report is human-skimmable.
    by_source_out: Dict[str, Any] = {}
    for src, bucket in by_source.items():
        by_source_out[src] = {
            "topic_distribution": dict(
                sorted(
                    bucket["topic_distribution"].items(),
                    key=lambda kv: (-kv[1], kv[0]),
                )
            ),
            "subtopic_distribution": dict(
                sorted(
                    bucket["subtopic_distribution"].items(),
                    key=lambda kv: (-kv[1], kv[0]),
                )
            ),
            "question_type_distribution": dict(
                sorted(
                    bucket["question_type_distribution"].items(),
                    key=lambda kv: (-kv[1], kv[0]),
                )
            ),
            "missing_count": bucket["missing_count"],
            "live_count": bucket["live_count"],
        }

    report: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "live_total": live_total,
        "missing_topic": missing_topic,
        "missing_subtopic": missing_subtopic,
        "missing_question_type": missing_question_type,
        "subtopic_not_in_taxonomy": subtopic_not_in_taxonomy,
        "subtype_question_type_mismatch": subtype_question_type_mismatch,
        "by_source": by_source_out,
        "summary": {
            "total_with_some_missing": len(
                set(missing_topic) | set(missing_subtopic) | set(missing_question_type)
            ),
            "total_with_unknown_subtopic": len(subtopic_not_in_taxonomy),
            "total_with_subtype_mismatch": len(subtype_question_type_mismatch),
            "needs_phase_2_2_count": len(needs_phase_2_2),
        },
    }
    return report


def _pct(numer: int, denom: int) -> str:
    if denom <= 0:
        return "n/a"
    return "{:.1f}%".format(100.0 * numer / denom)


def _print_summary(report: Dict[str, Any]) -> None:
    live = report["live_total"]
    s = report["summary"]
    n_topic = len(report["missing_topic"])
    n_sub = len(report["missing_subtopic"])
    n_qtype = len(report["missing_question_type"])
    n_unknown_sub = s["total_with_unknown_subtopic"]
    n_mismatch = s["total_with_subtype_mismatch"]

    print("AUDIT SUMMARY (2026-05-18)")
    print("- Live items: {:,}".format(live))
    print("- Missing topic: {} ({})".format(n_topic, _pct(n_topic, live)))
    print("- Missing subtopic: {} ({})".format(n_sub, _pct(n_sub, live)))
    print(
        "- Missing question_type: {} ({})".format(n_qtype, _pct(n_qtype, live))
    )
    print(
        "- Subtopic not in taxonomy: {} ({})".format(
            n_unknown_sub, _pct(n_unknown_sub, live)
        )
    )
    print(
        "- Subtype/question_type mismatch: {} ({})".format(
            n_mismatch, _pct(n_mismatch, live)
        )
    )
    needs = s["needs_phase_2_2_count"]
    print(
        "- Phase 2.2 needs to handle: {} items ({})".format(
            needs, _pct(needs, live)
        )
    )
    print("By source:")
    by_source = report["by_source"]
    # Stable ordering by source name for diffability across runs.
    for src in sorted(by_source.keys()):
        bucket = by_source[src]
        print(
            "  {}: {}/{}".format(
                src, bucket["missing_count"], bucket["live_count"]
            )
        )


def main() -> int:
    report = run_audit()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
    _print_summary(report)
    print("\nWrote {}".format(OUTPUT_PATH.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
