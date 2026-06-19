"""Section difficulty-quota + type-mix enforcement tests (balancing #1 & #3).

#1 (difficulty): the legacy assembler left Section 1 a flat medium pull
(no enforced spread) and treated a routed Section-2 tier as only a SOFT
ranking weight, so on a realistic pool a "hard" S2 was barely
distinguishable from medium and S1 had no easy/hard tails. Real GRE
sections are a band-centered MIX: S1 ~25/50/25 (easy/med/hard), a hard S2
slides the whole spread up (majority hard, a few off-band), an easy S2
slides it down. These tests pin a per-section coarse-band QUOTA derived
from the tier, swapping only stimulus-less singletons so RC/DI clusters
stay atomic.

#3 (type-mix): subtype proportions were best-effort; a thin pool or the
DI/figure passes could silently drift the mix. These tests pin that the
big buckets land within ±1 of their rounded targets on a deep pool.

All quotas/spreads are APPROXIMATE (reverse-engineered prep estimates),
kept as tunable module constants.
"""
from __future__ import annotations

import random
from collections import Counter

import pytest

# temp_db fixture from tests/conftest.py is applied automatically.


def _coarse(d):
    d = d or 3
    return "lo" if d <= 2 else ("mid" if d == 3 else "hi")


def _make_deep_quant_pool(_db, per_cell=10):
    """Plenty of stimulus-less qc / mcq_single / mcq_multi / numeric_entry
    across all 5 difficulty bands — everything is swappable."""
    from models.database import Question
    for subtype in ("qc", "mcq_single", "mcq_multi", "numeric_entry"):
        for band in (1, 2, 3, 4, 5):
            for i in range(per_cell):
                Question.create(measure="quant", subtype=subtype,
                                prompt=f"{subtype}-{band}-{i}",
                                time_target_seconds=90, concept_tags="[]",
                                explanation="", difficulty_target=band,
                                status="live")


def _make_deep_verbal_pool(_db, per_cell=10):
    from models.database import Question
    for subtype in ("tc", "se"):
        for band in (1, 2, 3, 4, 5):
            for i in range(per_cell):
                Question.create(measure="verbal", subtype=subtype,
                                prompt=f"{subtype}-{band}-{i}",
                                time_target_seconds=60, concept_tags="[]",
                                explanation="", difficulty_target=band,
                                status="live")


def _bands(qids):
    from models.database import Question
    rows = list(Question.select(Question.id, Question.difficulty_target)
                .where(Question.id.in_(list(qids))))
    c = Counter(_coarse(r.difficulty_target) for r in rows)
    return c


def _subtypes(qids):
    from models.database import Question
    rows = list(Question.select(Question.id, Question.subtype)
                .where(Question.id.in_(list(qids))))
    return Counter(r.subtype for r in rows)


# ── #1 difficulty: S1 medium-centered spread ──────────────────────────

def test_s1_medium_has_balanced_spread(temp_db):
    """S1 (difficulty_band='medium', no routing_tier, no theta) must be a
    medium-CENTERED spread — mid is the plurality but easy and hard tails
    are present. Pre-fix this was a flat random pull (mid ~20%)."""
    from services.question_bank import QuestionBankService
    _make_deep_quant_pool(temp_db)
    qb = QuestionBankService()
    mids, los, his = [], [], []
    for seed in range(20):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="quant", count=12, difficulty_band="medium")
        c = _bands(qids)
        mids.append(c["mid"]); los.append(c["lo"]); his.append(c["hi"])
    mean_mid = sum(mids) / len(mids)
    # medium spread is ~50% mid on a 12-item section → ≥5; pre-fix ~2-3.
    assert mean_mid >= 4.5, f"mid not the plurality: mean mid={mean_mid} (mids={mids})"
    # tails present (not a monolith): at least some easy and some hard.
    assert min(los) >= 1 and min(his) >= 1, (
        f"S1 lacks tails: los={los}, his={his}")


# ── #1 difficulty: routed S2 forms differentiate ──────────────────────

def test_hard_s2_is_hi_band_heavy(temp_db):
    from services.question_bank import QuestionBankService
    _make_deep_quant_pool(temp_db)
    qb = QuestionBankService()
    his = []
    for seed in range(20):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="quant", count=15, difficulty_band="hard",
            routing_tier="hard")
        his.append(_bands(qids)["hi"])
    mean_hi = sum(his) / len(his)
    # hard spread ~53% hi on 15 → target 8; allow the swap pool to land ≥7.
    assert mean_hi >= 7.0, f"hard S2 not hi-heavy: mean hi={mean_hi} (his={his})"


def test_easy_s2_is_lo_band_heavy(temp_db):
    from services.question_bank import QuestionBankService
    _make_deep_quant_pool(temp_db)
    qb = QuestionBankService()
    los = []
    for seed in range(20):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="quant", count=15, difficulty_band="easy",
            routing_tier="easy")
        los.append(_bands(qids)["lo"])
    mean_lo = sum(los) / len(los)
    assert mean_lo >= 7.0, f"easy S2 not lo-heavy: mean lo={mean_lo} (los={los})"


def test_routed_tiers_are_clearly_differentiated(temp_db):
    """The whole point: a hard S2 must contain visibly MORE hard items than
    a medium S2, which in turn has more than an easy S2 (and vice-versa for
    easy items). This is the 'hard section feels harder' fix."""
    from services.question_bank import QuestionBankService
    _make_deep_quant_pool(temp_db)
    qb = QuestionBankService()

    def mean_band(tier, band):
        vals = []
        for seed in range(15):
            random.seed(seed)
            qids = qb.select_questions_composed(
                measure="quant", count=15, difficulty_band=tier,
                routing_tier=tier)
            vals.append(_bands(qids)[band])
        return sum(vals) / len(vals)

    hi_hard = mean_band("hard", "hi")
    hi_med = mean_band("medium", "hi")
    hi_easy = mean_band("easy", "hi")
    assert hi_hard > hi_med > hi_easy, (
        f"hi-band not monotonic across tiers: hard={hi_hard}, "
        f"med={hi_med}, easy={hi_easy}")

    lo_easy = mean_band("easy", "lo")
    lo_med = mean_band("medium", "lo")
    lo_hard = mean_band("hard", "lo")
    assert lo_easy > lo_med > lo_hard, (
        f"lo-band not monotonic across tiers: easy={lo_easy}, "
        f"med={lo_med}, hard={lo_hard}")


def test_verbal_hard_s2_shifts_swappable_bands_up(temp_db):
    """Verbal swappable items are TC/SE singletons (RC sits in clusters).
    A hard verbal S2 must push those singletons toward the hi band."""
    from services.question_bank import QuestionBankService
    _make_deep_verbal_pool(temp_db)
    qb = QuestionBankService()
    hi_hard, hi_easy = [], []
    for seed in range(15):
        random.seed(seed)
        qh = qb.select_questions_composed(
            measure="verbal", count=15, difficulty_band="hard",
            routing_tier="hard")
        hi_hard.append(_bands(qh)["hi"])
        random.seed(seed)
        qe = qb.select_questions_composed(
            measure="verbal", count=15, difficulty_band="easy",
            routing_tier="easy")
        hi_easy.append(_bands(qe)["hi"])
    assert sum(hi_hard) / len(hi_hard) > sum(hi_easy) / len(hi_easy), (
        f"verbal hard not harder than easy: hard_hi={hi_hard}, easy_hi={hi_easy}")


# ── #1: theta-CAT path is NOT overridden by the band quota ─────────────

def test_theta_path_not_overridden_when_no_routing_tier(temp_db):
    """When routing_tier is None but target_theta is active (legacy CAT),
    the difficulty quota must NOT clamp the band spread — theta still
    drives selection. (Mirrors test_section_3tier_routing's theta test.)"""
    from models.database import Question
    from services.question_bank import QuestionBankService
    for band in (1, 2, 3, 4, 5):
        for i in range(6):
            Question.create(measure="quant", subtype="mcq_single",
                            prompt=f"b{band}-{i}", difficulty_target=band,
                            time_target_seconds=90, concept_tags="[]",
                            explanation="", status="live")
    from services.rating_service import seed_initial_ratings
    seed_initial_ratings()
    from models.database import ItemRating
    qb = QuestionBankService()
    random.seed(20260618)
    picks = qb.select_questions_composed(
        measure="quant", count=10, difficulty_band="medium",
        target_theta=1.0, routing_tier=None)
    ratings = [ItemRating.get(ItemRating.question_id == q).rating for q in picks]
    mean_rating = sum(ratings) / len(ratings)
    # theta=+1.0 should still pull picks above neutral; a medium band quota
    # would have dragged this toward 0.
    assert mean_rating >= 0.25, (
        f"theta path was overridden by band quota: mean rating={mean_rating}")


# ── #3 type-mix: big buckets within tolerance on a deep pool ───────────

def test_quant_type_mix_within_tolerance(temp_db):
    from services.question_bank import QuestionBankService
    _make_deep_quant_pool(temp_db)
    qb = QuestionBankService()
    for seed in range(20):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="quant", count=15, difficulty_band="medium")
        c = _subtypes(qids)
        # qc ~33% of 15 ≈ 5; allow ±1.
        assert 4 <= c["qc"] <= 6, f"seed {seed}: qc off target: {c['qc']} ({dict(c)})"
        # mcq_single is the largest bucket.
        assert c["mcq_single"] >= c["qc"], (
            f"seed {seed}: mcq_single should be the largest bucket: {dict(c)}")
        # minority buckets present.
        assert c["mcq_multi"] >= 1, f"seed {seed}: no mcq_multi: {dict(c)}"
        assert c["numeric_entry"] >= 1, f"seed {seed}: no numeric_entry: {dict(c)}"


def test_verbal_type_mix_tc_se_balanced(temp_db):
    from services.question_bank import QuestionBankService
    _make_deep_verbal_pool(temp_db)
    qb = QuestionBankService()
    for seed in range(20):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="verbal", count=12, difficulty_band="medium")
        c = _subtypes(qids)
        # No RC pool here (all singletons), so the section is TC+SE; each
        # ~25% of the blueprint but with RC absent they split the section.
        # Pin that neither collapses to zero and they're roughly balanced.
        assert c["tc"] >= 3, f"seed {seed}: tc too sparse: {dict(c)}"
        assert c["se"] >= 3, f"seed {seed}: se too sparse: {dict(c)}"


# ── seed-backed end-to-end: routed S2 visibly differs from S1 ──────────

from config import SEED_DB_PATH  # noqa: E402

_SEED_OK = SEED_DB_PATH.exists() and SEED_DB_PATH.stat().st_size >= 1024


def _coarse_counts_for(qids):
    from models.database import Question
    rows = list(Question.select(Question.id, Question.difficulty_target)
                .where(Question.id.in_(list(qids))))
    return Counter(_coarse(r.difficulty_target) for r in rows)


@pytest.mark.skipif(not _SEED_OK, reason="seed DB missing or LFS pointer")
def test_seed_hard_s2_has_more_hi_than_s1():
    """End-to-end on the shipped seed: a routed-HARD Quant S2 must contain
    more hi-band items than the (medium) Quant S1 it routed from."""
    import random as _r
    from models.database import init_db, ServedLog
    from models.exam_session import ExamSession, SectionType
    from services.question_bank import QuestionBankService
    init_db()
    ServedLog.delete().execute()

    wins = 0
    trials = [11, 23, 57, 99, 256]
    for seed in trials:
        _r.seed(seed)
        exam = ExamSession(test_type="full_mock", mode="simulation")
        exam.build_full_mock(QuestionBankService())
        s1 = exam.sections[SectionType.QUANT_S1]
        s1._correctness = {qid: True for qid in s1.question_ids}  # top → hard
        exam._adapt_next_section(SectionType.QUANT_S1)
        s2 = exam.sections[SectionType.QUANT_S2]
        assert s2.routing_tier == "hard"
        hi_s1 = _coarse_counts_for(s1.question_ids)["hi"]
        hi_s2 = _coarse_counts_for(s2.question_ids)["hi"]
        if hi_s2 > hi_s1:
            wins += 1
    ServedLog.delete().execute()
    assert wins >= len(trials) - 1, (
        f"hard S2 not reliably harder than S1: {wins}/{len(trials)} seeds")


@pytest.mark.skipif(not _SEED_OK, reason="seed DB missing or LFS pointer")
def test_seed_easy_s2_has_more_lo_than_hard_s2():
    """A routed-EASY Quant S2 has more lo-band items than a routed-HARD
    Quant S2 built from the same mock shell."""
    import random as _r
    from models.database import init_db, ServedLog
    from models.exam_session import ExamSession, SectionType
    from services.question_bank import QuestionBankService
    init_db()
    ServedLog.delete().execute()

    lo_easy_total = 0
    lo_hard_total = 0
    for seed in [5, 19, 64, 128, 300]:
        _r.seed(seed)
        exam = ExamSession(test_type="full_mock", mode="simulation")
        exam.build_full_mock(QuestionBankService())
        s1 = exam.sections[SectionType.QUANT_S1]

        s1._correctness = {qid: False for qid in s1.question_ids}  # → easy
        exam._adapt_next_section(SectionType.QUANT_S1)
        easy_s2 = exam.sections[SectionType.QUANT_S2]
        lo_easy_total += _coarse_counts_for(easy_s2.question_ids)["lo"]

        exam.sections[SectionType.QUANT_S2].question_ids = []
        s1._correctness = {qid: True for qid in s1.question_ids}  # → hard
        exam._adapt_next_section(SectionType.QUANT_S1)
        hard_s2 = exam.sections[SectionType.QUANT_S2]
        lo_hard_total += _coarse_counts_for(hard_s2.question_ids)["lo"]
    ServedLog.delete().execute()
    assert lo_easy_total > lo_hard_total, (
        f"easy S2 not easier than hard S2: lo(easy)={lo_easy_total}, "
        f"lo(hard)={lo_hard_total}")
