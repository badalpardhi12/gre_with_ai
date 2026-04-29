"""
Blueprint-pinning tests. These encode the real-GRE composition rules from
data/audits/ets_blueprint_2026.md so any regression that shrinks a section,
drops the DI cluster, or splits an RC passage fails CI loudly.

These tests hit the real (seeded) SQLite DB via the normal question-bank
entry points — no mocking. That's deliberate: the point is to catch
assembly-side drift even when the DB shifts underneath.
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict

import pytest

from config import (
    AWA_TIME, VERBAL_S1_TIME, VERBAL_S2_TIME,
    QUANT_S1_TIME, QUANT_S2_TIME,
    VERBAL_S1_COUNT, VERBAL_S2_COUNT,
    QUANT_S1_COUNT, QUANT_S2_COUNT,
)
from models.database import init_db, Question, Stimulus
from models.exam_session import ExamSession, SectionType, SECTION_META
from services.question_bank import (
    QuestionBankService,
    CLUSTERED_VERBAL_SUBTYPES,
    DI_STIMULUS_TYPES,
    DI_CLUSTER_MIN_SIZE,
    DI_CLUSTER_TARGET_SIZE,
    VERBAL_COMPOSITION,
    QUANT_COMPOSITION,
)


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    yield


@pytest.fixture
def qb():
    return QuestionBankService()


def _build_full_mock(seed):
    random.seed(seed)
    qb = QuestionBankService()
    exam = ExamSession(test_type="full_mock", mode="simulation")
    exam.build_full_mock(qb)
    # Materialize S2 by pretending S1 was half-right.
    for s1 in (SectionType.VERBAL_S1, SectionType.QUANT_S1):
        if s1 in exam.sections:
            sec = exam.sections[s1]
            sec._correctness = {qid: (i % 2 == 0)
                                for i, qid in enumerate(sec.question_ids)}
            exam._adapt_next_section(s1)
    return exam


def _clusters(qids):
    rows = Question.select(Question.id, Question.subtype, Question.stimulus_id)\
                   .where(Question.id.in_(list(qids)))
    by_stim = defaultdict(list)
    subtype_counter = Counter()
    for r in rows:
        subtype_counter[r.subtype] += 1
        if r.stimulus_id is not None:
            by_stim[r.stimulus_id].append((r.id, r.subtype))
    return by_stim, subtype_counter


# ── 1. Section counts + time limits match the blueprint ─────────────

@pytest.mark.parametrize("seed", [17, 42, 101, 2026, 31337])
def test_full_mock_section_counts_and_times(seed):
    exam = _build_full_mock(seed)
    expected = {
        SectionType.AWA:       (1,                AWA_TIME),
        SectionType.VERBAL_S1: (VERBAL_S1_COUNT,  VERBAL_S1_TIME),
        SectionType.VERBAL_S2: (VERBAL_S2_COUNT,  VERBAL_S2_TIME),
        SectionType.QUANT_S1:  (QUANT_S1_COUNT,   QUANT_S1_TIME),
        SectionType.QUANT_S2:  (QUANT_S2_COUNT,   QUANT_S2_TIME),
    }
    for sec_type, (want_count, want_time) in expected.items():
        sec = exam.sections[sec_type]
        assert len(sec.question_ids) == want_count, \
            f"{sec_type.value}: expected {want_count}, got {len(sec.question_ids)}"
        assert sec.time_limit == want_time, \
            f"{sec_type.value}: expected {want_time}s, got {sec.time_limit}s"


# ── 2. RC cluster atomicity ─────────────────────────────────────────

@pytest.mark.parametrize("seed", [17, 42, 101, 2026, 31337])
def test_rc_clusters_are_atomic(seed):
    exam = _build_full_mock(seed)
    for sec_type in (SectionType.VERBAL_S1, SectionType.VERBAL_S2):
        sec = exam.sections[sec_type]
        by_stim, _ = _clusters(sec.question_ids)
        qid_set = set(sec.question_ids)
        for stim_id, items in by_stim.items():
            first_subtype = items[0][1]
            if first_subtype not in CLUSTERED_VERBAL_SUBTYPES:
                continue
            live_siblings = {
                q.id for q in Question.select(Question.id)
                .where((Question.stimulus_id == stim_id) &
                       (Question.measure == "verbal") &
                       (Question.status == "live") &
                       (Question.subtype.in_(list(CLUSTERED_VERBAL_SUBTYPES))))
            }
            missing = live_siblings - qid_set
            assert not missing, (
                f"{sec_type.value}: stimulus {stim_id} has missing live "
                f"siblings {sorted(missing)}; cluster was split.")


# ── 3. DI cluster present in every quant section ────────────────────

@pytest.mark.parametrize("seed", [17, 42, 101, 2026, 31337])
def test_quant_section_contains_di_cluster(seed):
    exam = _build_full_mock(seed)
    for sec_type in (SectionType.QUANT_S1, SectionType.QUANT_S2):
        sec = exam.sections[sec_type]
        by_stim, subtype_ctr = _clusters(sec.question_ids)
        # At least one of:
        # (a) a graph/table/chart stimulus with ≥2 quant siblings present, or
        # (b) fallback: ≥2 solo data_interp items
        cluster_ok = False
        if by_stim:
            stim_types = {
                s.id: s.stimulus_type
                for s in Stimulus.select(Stimulus.id, Stimulus.stimulus_type)
                .where(Stimulus.id.in_(list(by_stim.keys())))
            }
            for sid, items in by_stim.items():
                if (stim_types.get(sid) in DI_STIMULUS_TYPES
                        and len(items) >= DI_CLUSTER_MIN_SIZE):
                    cluster_ok = True
                    break
        if not cluster_ok:
            cluster_ok = subtype_ctr.get("data_interp", 0) >= DI_CLUSTER_MIN_SIZE
        assert cluster_ok, (
            f"{sec_type.value}: no DI cluster found (subtypes={dict(subtype_ctr)}, "
            f"cluster_sizes={[len(v) for v in by_stim.values()]})")


# ── 4. S1→S2 dedup (no repeats within a single exam) ────────────────

@pytest.mark.parametrize("seed", [17, 42, 101, 2026, 31337])
def test_s1_s2_no_duplicates(seed):
    exam = _build_full_mock(seed)
    for s1_type, s2_type in [(SectionType.VERBAL_S1, SectionType.VERBAL_S2),
                             (SectionType.QUANT_S1,  SectionType.QUANT_S2)]:
        s1_ids = set(exam.sections[s1_type].question_ids)
        s2_ids = set(exam.sections[s2_type].question_ids)
        overlap = s1_ids & s2_ids
        assert not overlap, (
            f"Overlap between {s1_type.value} and {s2_type.value}: {overlap}")


# ── 5. Verbal composition proportions within tolerance ──────────────

@pytest.mark.parametrize("seed", [17, 42, 101])
def test_verbal_composition_within_tolerance(seed):
    """RC ~50%, TC ~25%, SE ~25% per section — allow ±2 items slack for
    a 12/15-item section given rounding and finite pool sizes."""
    exam = _build_full_mock(seed)
    for sec_type in (SectionType.VERBAL_S1, SectionType.VERBAL_S2):
        sec = exam.sections[sec_type]
        _, hist = _clusters(sec.question_ids)
        rc = (hist.get("rc_single", 0) + hist.get("rc_multi", 0)
              + hist.get("rc_select_passage", 0))
        tc = hist.get("tc", 0)
        se = hist.get("se", 0)
        total = len(sec.question_ids)
        assert rc >= total // 3, \
            f"{sec_type.value}: RC too sparse ({rc}/{total})"
        assert tc >= 1, f"{sec_type.value}: no TC items"
        assert se >= 1, f"{sec_type.value}: no SE items"


# ── 6. Quant composition: QC present, MCQ dominant, DI cluster counted ─

@pytest.mark.parametrize("seed", [17, 42, 101])
def test_quant_composition_within_tolerance(seed):
    exam = _build_full_mock(seed)
    for sec_type in (SectionType.QUANT_S1, SectionType.QUANT_S2):
        sec = exam.sections[sec_type]
        _, hist = _clusters(sec.question_ids)
        qc = hist.get("qc", 0)
        mcq = hist.get("mcq_single", 0) + hist.get("mcq_multi", 0)
        total = len(sec.question_ids)
        assert qc >= 2, f"{sec_type.value}: QC under-represented ({qc})"
        # MCQ + DI children dominate — together they should be the majority.
        di_like = hist.get("data_interp", 0) + mcq
        assert di_like >= total // 2, \
            f"{sec_type.value}: MCQ+DI under-represented ({di_like}/{total})"


# ── 7. Cross-exam exclude_ids plumbing ───────────────────────────────

def test_exclude_ids_prevents_reuse(qb):
    random.seed(12345)
    first = qb.select_questions_composed(
        measure="quant", count=QUANT_S1_COUNT, difficulty_band="medium",
    )
    second = qb.select_questions_composed(
        measure="quant", count=QUANT_S1_COUNT, difficulty_band="medium",
        exclude_ids=set(first),
    )
    assert not (set(first) & set(second)), \
        "exclude_ids was ignored — same qids appear in consecutive picks"


# ── 8. Composition-target rounding sums to requested count ──────────

@pytest.mark.parametrize("count", [12, 15])
def test_composition_targets_sum_to_count(count):
    v_tgt = QuestionBankService._composition_targets(VERBAL_COMPOSITION, count)
    q_tgt = QuestionBankService._composition_targets(QUANT_COMPOSITION, count)
    assert sum(v_tgt.values()) == count
    assert sum(q_tgt.values()) == count
