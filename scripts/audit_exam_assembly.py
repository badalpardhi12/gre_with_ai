"""Probe the current exam-assembly output against the ETS blueprint.

Run from the worktree root:
    venv/bin/python scripts/audit_exam_assembly.py

Produces stdout report AND data/audits/current_assembly_audit.json.
Read-only against the DB (uses QuestionBankService through the normal
public API).
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Ensure the worktree root is importable before models/config load.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from models.database import init_db, Question   # noqa: E402
from models.exam_session import (               # noqa: E402
    ExamSession,
    SectionType,
    SECTION_META,
)
from services.question_bank import QuestionBankService  # noqa: E402


def _subtype_histogram(qids):
    rows = Question.select(Question.id, Question.subtype,
                           Question.difficulty_target, Question.stimulus_id)\
                   .where(Question.id.in_(list(qids)))
    subtype = Counter()
    diff = Counter()
    cluster_by_stim = defaultdict(list)
    for r in rows:
        subtype[r.subtype] += 1
        diff[r.difficulty_target] += 1
        if r.stimulus_id is not None:
            cluster_by_stim[r.stimulus_id].append((r.id, r.subtype))
    return subtype, diff, cluster_by_stim


def _total_per_subtype(measure):
    """Count how many questions the DB knows about per subtype, for context."""
    rows = (Question
            .select(Question.subtype, Question.status)
            .where(Question.measure == measure))
    live = Counter()
    for r in rows:
        if r.status == "live":
            live[r.subtype] += 1
    return dict(live)


def _cluster_atomicity(cluster_by_stim, all_qids_in_section):
    """For every stimulus present in this section, verify ALL its live
    sibling questions are either all present or all absent. Returns a list
    of violations."""
    violations = []
    all_qids_in_section = set(all_qids_in_section)
    for stim_id, rows in cluster_by_stim.items():
        if len(rows) == 1:
            # Could still be an incomplete RC/DI cluster — check the DB for
            # siblings.
            sibling_ids = {
                q.id for q in Question.select(Question.id)
                    .where((Question.stimulus_id == stim_id) &
                           (Question.status == "live"))
            }
            if len(sibling_ids) > 1:
                included = sibling_ids & all_qids_in_section
                missing = sibling_ids - included
                if missing:
                    violations.append({
                        "stimulus_id": stim_id,
                        "included": sorted(included),
                        "missing": sorted(missing),
                        "cluster_size": len(sibling_ids),
                    })
    return violations


def audit(seed=17):
    random.seed(seed)
    init_db()
    qb = QuestionBankService()

    exam = ExamSession(test_type="full_mock", mode="simulation")
    exam.build_full_mock(qb)

    # Simulate S1 completion at 50% correct so S2 receives "medium" band
    # and actually materializes (instead of staying deferred).
    for s1_type in (SectionType.VERBAL_S1, SectionType.QUANT_S1):
        if s1_type in exam.sections:
            s1 = exam.sections[s1_type]
            s1._correctness = {qid: (i % 2 == 0)
                               for i, qid in enumerate(s1.question_ids)}
            exam._adapt_next_section(s1_type)

    report = {"seed": seed, "section_order": [s.value for s in exam.section_order],
              "sections": []}

    for sec_type in exam.section_order:
        sec = exam.sections[sec_type]
        meta = SECTION_META[sec_type]
        measure, sec_idx, time_limit, target_count = meta
        subtype_hist, diff_hist, clusters = _subtype_histogram(sec.question_ids)
        violations = _cluster_atomicity(clusters, sec.question_ids)

        report["sections"].append({
            "section": sec_type.value,
            "measure": measure,
            "expected_count": target_count,
            "actual_count": len(sec.question_ids),
            "time_limit_s": time_limit,
            "subtype_histogram": dict(subtype_hist),
            "difficulty_histogram": dict(diff_hist),
            "cluster_sizes": {str(k): len(v) for k, v in clusters.items()},
            "cluster_atomicity_violations": violations,
        })

    # Library context: how many items per subtype does the DB expose?
    report["library_live_counts"] = {
        "verbal": _total_per_subtype("verbal"),
        "quant":  _total_per_subtype("quant"),
    }

    out_path = ROOT / "data" / "audits" / "current_assembly_audit.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    print(f"\nWritten: {out_path}")


if __name__ == "__main__":
    audit()
