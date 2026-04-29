"""
End-to-end smoke test: builds 5 full mock exams (different seeds), verifies
each matches the ETS blueprint from data/audits/ets_blueprint_2026.md, and
checks cross-exam dedup.

Usage:
    venv/bin/python scripts/smoke_test_exam_assembly.py [--no-persist]

Exit 0 if all 5 pass blueprint + cross-exam dedup; exit 1 otherwise.

Persists per-exam dumps to data/audits/smoke_exams/exam_<seed>.json.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    AWA_TIME, VERBAL_S1_TIME, VERBAL_S2_TIME,
    QUANT_S1_TIME, QUANT_S2_TIME,
    VERBAL_S1_COUNT, VERBAL_S2_COUNT,
    QUANT_S1_COUNT, QUANT_S2_COUNT,
)
from models.database import init_db, Question, Response           # noqa: E402
from models.exam_session import (                                  # noqa: E402
    ExamSession, SectionType, SECTION_META,
)
from services.question_bank import (                               # noqa: E402
    QuestionBankService, CLUSTERED_VERBAL_SUBTYPES,
    DI_CLUSTER_MIN_SIZE, DI_CLUSTER_TARGET_SIZE,
)


BLUEPRINT_STRUCTURE = {
    SectionType.AWA.value:       {"count": 1,               "time": AWA_TIME},
    SectionType.VERBAL_S1.value: {"count": VERBAL_S1_COUNT, "time": VERBAL_S1_TIME},
    SectionType.VERBAL_S2.value: {"count": VERBAL_S2_COUNT, "time": VERBAL_S2_TIME},
    SectionType.QUANT_S1.value:  {"count": QUANT_S1_COUNT,  "time": QUANT_S1_TIME},
    SectionType.QUANT_S2.value:  {"count": QUANT_S2_COUNT,  "time": QUANT_S2_TIME},
}


def _subtype_histogram(qids):
    rows = Question.select(Question.id, Question.subtype, Question.stimulus_id)\
                   .where(Question.id.in_(list(qids)))
    subtype = Counter()
    clusters = defaultdict(list)
    for r in rows:
        subtype[r.subtype] += 1
        if r.stimulus_id is not None:
            clusters[r.stimulus_id].append((r.id, r.subtype))
    return subtype, clusters


def _check_cluster_atomicity(qids, clusters):
    """For any stimulus present, every live sibling must be present."""
    qid_set = set(qids)
    violations = []
    for stim_id in clusters:
        siblings = {q.id for q in Question.select(Question.id)
                    .where((Question.stimulus_id == stim_id) &
                           (Question.status == "live"))}
        # Only flag RC/DI clusters (passages and charts). Solo qc/mcq_single
        # that happen to share a stimulus_id for rendering-only reasons are
        # not treated as mandatory clusters.
        if len(siblings) < 2:
            continue
        # Only clustered-verbal subtypes AND chart/graph stimuli are
        # considered mandatory clusters (matches the atomicity rule in
        # question_bank.py).
        first_subtype = clusters[stim_id][0][1]
        if first_subtype in CLUSTERED_VERBAL_SUBTYPES:
            missing = siblings - qid_set
            if missing:
                violations.append({"stimulus_id": stim_id,
                                   "missing": sorted(missing)})
    return violations


def _check_di_cluster(qant_section_qids):
    """Check that the quant section contains at least one DI cluster of
    size ≥DI_CLUSTER_MIN_SIZE from a graph/table/chart stimulus, OR that
    at least DI_CLUSTER_MIN_SIZE solo data_interp items are present as
    the documented fallback."""
    rows = list(
        Question.select(Question.id, Question.subtype, Question.stimulus_id)
        .where(Question.id.in_(list(qant_section_qids)))
    )
    by_stim = defaultdict(list)
    solo_di = 0
    for r in rows:
        if r.subtype == "data_interp" and r.stimulus_id is None:
            solo_di += 1
        elif r.stimulus_id is not None:
            by_stim[r.stimulus_id].append(r)

    # Pull the stimulus types for clustered entries
    stim_types = {}
    if by_stim:
        for s in Stimulus.select(Stimulus.id, Stimulus.stimulus_type)\
                         .where(Stimulus.id.in_(list(by_stim.keys()))):
            stim_types[s.id] = s.stimulus_type

    for stim_id, items in by_stim.items():
        if stim_types.get(stim_id) in ("graph", "table", "chart"):
            if len(items) >= DI_CLUSTER_MIN_SIZE:
                return True, f"cluster on stimulus {stim_id} size={len(items)}"
    if solo_di >= DI_CLUSTER_MIN_SIZE:
        return True, f"solo data_interp fallback count={solo_di}"
    # Count how many solo+also data_interp items to flag a partial miss
    return False, "no DI cluster and fewer than 2 solo data_interp items"


# We need Stimulus here — import late to avoid circular import at module-level
from models.database import Stimulus                               # noqa: E402


def build_and_audit(seed):
    random.seed(seed)
    qb = QuestionBankService()
    exam = ExamSession(test_type="full_mock", mode="simulation")
    exam.build_full_mock(qb)

    # Simulate S1 completion at 50% correct so S2 materializes.
    for s1 in (SectionType.VERBAL_S1, SectionType.QUANT_S1):
        if s1 in exam.sections:
            sec = exam.sections[s1]
            sec._correctness = {qid: (i % 2 == 0)
                                for i, qid in enumerate(sec.question_ids)}
            exam._adapt_next_section(s1)

    audit = {"seed": seed, "order": [s.value for s in exam.section_order],
             "sections": [], "blueprint_ok": True, "failures": []}

    all_qids_in_exam = []
    for sec_type in exam.section_order:
        sec = exam.sections[sec_type]
        expected = BLUEPRINT_STRUCTURE[sec_type.value]
        subtype_hist, clusters = _subtype_histogram(sec.question_ids)

        failures = []
        if len(sec.question_ids) != expected["count"]:
            failures.append(
                f"count mismatch: expected {expected['count']}, got "
                f"{len(sec.question_ids)}")
        if sec.time_limit != expected["time"]:
            failures.append(
                f"time mismatch: expected {expected['time']}, got "
                f"{sec.time_limit}")

        if sec_type != SectionType.AWA:
            cluster_viols = _check_cluster_atomicity(sec.question_ids,
                                                      clusters)
            if cluster_viols:
                failures.append(
                    f"cluster atomicity violations: {cluster_viols}")

        if sec_type in (SectionType.QUANT_S1, SectionType.QUANT_S2):
            di_ok, di_note = _check_di_cluster(sec.question_ids)
            if not di_ok:
                failures.append(f"DI cluster missing: {di_note}")

        if sec_type != SectionType.AWA:
            all_qids_in_exam.extend(sec.question_ids)

        audit["sections"].append({
            "section": sec_type.value,
            "count": len(sec.question_ids),
            "time_limit": sec.time_limit,
            "subtypes": dict(subtype_hist),
            "clusters": {str(k): len(v) for k, v in clusters.items()},
            "failures": failures,
        })
        if failures:
            audit["blueprint_ok"] = False
            audit["failures"].extend([f"{sec_type.value}: {f}"
                                      for f in failures])

    # In-exam S1→S2 dedup (no question in S1 and S2 of same measure)
    for measure_pair in [(SectionType.VERBAL_S1, SectionType.VERBAL_S2),
                         (SectionType.QUANT_S1, SectionType.QUANT_S2)]:
        s1, s2 = measure_pair
        if s1 in exam.sections and s2 in exam.sections:
            dup = set(exam.sections[s1].question_ids) & \
                  set(exam.sections[s2].question_ids)
            if dup:
                audit["blueprint_ok"] = False
                audit["failures"].append(
                    f"{s1.value}/{s2.value} S1-S2 duplicate qids: {sorted(dup)}")

    audit["all_non_awa_qids"] = sorted(all_qids_in_exam)
    return audit


def run(persist=True):
    init_db()
    seeds = [17, 42, 101, 2026, 31337]
    audits = []
    for seed in seeds:
        audits.append(build_and_audit(seed))

    # Cross-exam dedup validation.
    #
    # The engine's cross-session dedup runs off `Response` rows from the
    # last N days — it only kicks in once a question has actually been
    # answered. The 5 smoke builds here don't simulate answers, so some
    # repetition across independent random seeds is expected.
    #
    # What we *can* validate is the in-exam dedup wiring (S1 and S2 of
    # the same measure share zero qids) — that's already asserted
    # per-exam in `build_and_audit`.
    #
    # We also run a stricter sixth exam, feeding the union of all qids
    # picked across the first 5 as `exclude_ids`, and assert zero overlap.
    # That exercises the cross-exam exclusion plumbing end-to-end.
    overall_count = Counter()
    for a in audits:
        for qid in a["all_non_awa_qids"]:
            overall_count[qid] += 1
    total_qs = sum(overall_count.values())
    unique_qs = len(overall_count)
    reuse_ratio = 1.0 - (unique_qs / total_qs) if total_qs else 0.0
    max_per_qid = max(overall_count.values(), default=0)

    # Sixth exam with explicit exclude of every qid served so far.
    qb = QuestionBankService()
    random.seed(sum(seeds))
    served = set(overall_count)
    exclude_check = {}
    for measure, count in [("verbal", VERBAL_S1_COUNT),
                           ("quant", QUANT_S1_COUNT)]:
        picked = qb.select_questions_composed(
            measure=measure, count=count, difficulty_band="medium",
            exclude_ids=served,
        )
        overlap = set(picked) & served
        exclude_check[f"{measure}_overlap_after_exclude"] = sorted(overlap)

    exclude_plumbing_ok = all(len(v) == 0 for v in exclude_check.values())

    summary = {
        "seeds": seeds,
        "per_exam_pass": [a["blueprint_ok"] for a in audits],
        "total_questions_served": total_qs,
        "unique_questions_served": unique_qs,
        "reuse_ratio": round(reuse_ratio, 4),
        "max_times_any_qid_reused": max_per_qid,
        "exclude_plumbing_check": exclude_check,
        "exclude_plumbing_ok": exclude_plumbing_ok,
    }

    # Hard-fail conditions.
    all_pass = all(summary["per_exam_pass"])
    summary["dedup_ok"] = exclude_plumbing_ok

    if persist:
        out_dir = ROOT / "data" / "audits" / "smoke_exams"
        out_dir.mkdir(parents=True, exist_ok=True)
        for a in audits:
            with open(out_dir / f"exam_{a['seed']}.json", "w") as f:
                json.dump(a, f, indent=2)
        with open(out_dir / "summary.json", "w") as f:
            json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    if not all_pass:
        print("\nFAIL: one or more exams did not match the blueprint.")
        for a in audits:
            if not a["blueprint_ok"]:
                print(f"  seed={a['seed']}: {a['failures']}")
    if not exclude_plumbing_ok:
        print("\nFAIL: exclude_ids plumbing leaked previously-served qids.")
        print(f"  {exclude_check}")

    return 0 if (all_pass and exclude_plumbing_ok) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--no-persist", action="store_true")
    args = p.parse_args()
    sys.exit(run(persist=not args.no_persist))
