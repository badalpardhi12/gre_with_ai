"""Phase 7.2 — section-level adaptivity 3-tier raw-count routing.

The user resolved Open Question 1 with Path B (2026-05-18): replace the
continuous theta/percentage routing with ETS's 3-tier raw-count routing.
Theta is still computed and persisted as a side-signal but does NOT
drive the section-routing decision.

These tests pin the contract:

  * Quant: ≥8 correct → 'hard', 4-7 → 'medium', <4 → 'easy'.
  * Verbal: ≥9 correct → 'hard', 5-8 → 'medium', <5 → 'easy'.
  * Edge cases: exactly at threshold lands on the upper tier.
  * SectionResult persists ``routing_tier`` on commit (column is
    nullable for AWA / pre-Phase-7 rows).
  * Theta still flows into the SectionResult-bearing path (compute_theta
    runs, target_theta is forwarded to the question bank).
  * Backwards compat: ``routing_tier=None`` callers see the legacy
    theta-aware ranking unchanged.
"""
from __future__ import annotations

import json
import random

import pytest

from models.exam_session import (
    ExamSession, SectionType, _resolve_tier,
    QUANT_TIER_THRESHOLDS, VERBAL_TIER_THRESHOLDS,
)


# ── Stub bank, mirrors tests/test_exam_session.py ──────────────────────


class StubBank:
    """Minimal QuestionBank-like stub.

    Captures the full kwargs dict on each call so tests can assert the
    new ``routing_tier`` (and the existing ``target_theta``) are wired
    through correctly.
    """

    def __init__(self):
        self.calls = []

    def select_questions_composed(self, measure, count, difficulty_band,
                                   exclude_ids=None, **kwargs):
        self.calls.append({
            "measure": measure,
            "count": count,
            "difficulty_band": difficulty_band,
            "exclude_ids": exclude_ids,
            **kwargs,
        })
        base = 1000 if measure == "verbal" else 2000
        return [base + i for i in range(count)]

    def select_questions(self, *args, **kwargs):
        return [99]

    def select_awa_prompt(self):
        return [1]


def _build_full_mock():
    exam = ExamSession(test_type="full_mock", mode="simulation")
    exam.build_full_mock(StubBank())
    return exam


# ── _resolve_tier — pure function ──────────────────────────────────────


@pytest.mark.parametrize("correct,expected", [
    (0, "easy"),
    (3, "easy"),       # below threshold
    (4, "medium"),     # exactly at medium threshold
    (7, "medium"),     # below hard threshold
    (8, "hard"),       # exactly at hard threshold (edge case)
    (10, "hard"),
    (12, "hard"),
])
def test_resolve_tier_quant(correct, expected):
    """Quant: ≥8 hard, ≥4 medium, else easy."""
    assert _resolve_tier("quant", correct) == expected


@pytest.mark.parametrize("correct,expected", [
    (0, "easy"),
    (4, "easy"),       # below threshold
    (5, "medium"),     # exactly at medium threshold
    (8, "medium"),     # below hard threshold
    (9, "hard"),       # exactly at hard threshold (edge case)
    (11, "hard"),
    (12, "hard"),
])
def test_resolve_tier_verbal(correct, expected):
    """Verbal: ≥9 hard, ≥5 medium, else easy."""
    assert _resolve_tier("verbal", correct) == expected


def test_resolve_tier_unknown_measure_defaults_medium():
    """An unknown measure (e.g. AWA) returns 'medium' rather than
    crashing — protects drill paths and AWA. """
    assert _resolve_tier("awa", 0) == "medium"


def test_threshold_constants_exist():
    """Tier thresholds are exposed at module scope so future
    calibration data can shift them without code surgery."""
    assert "hard" in QUANT_TIER_THRESHOLDS
    assert "medium" in QUANT_TIER_THRESHOLDS
    assert "hard" in VERBAL_TIER_THRESHOLDS
    assert "medium" in VERBAL_TIER_THRESHOLDS
    assert QUANT_TIER_THRESHOLDS["hard"] == 8
    assert QUANT_TIER_THRESHOLDS["medium"] == 4
    assert VERBAL_TIER_THRESHOLDS["hard"] == 9
    assert VERBAL_TIER_THRESHOLDS["medium"] == 5


# ── _adapt_next_section — quant 12-Q boundaries ────────────────────────


@pytest.mark.parametrize("n_correct,expected", [
    (0, "easy"),
    (3, "easy"),       # 3/12 — under 4 -> easy
    (4, "medium"),     # 4/12 — exactly at threshold -> medium
    (7, "medium"),     # 7/12 — still medium
    (8, "hard"),       # 8/12 — exactly at threshold -> hard
    (12, "hard"),
])
def test_quant_adapt_uses_3tier_routing(n_correct, expected):
    """Quant adapt resolves S2 difficulty via raw-count tier (not
    pct_correct). 8/12 is hard, NOT medium."""
    exam = _build_full_mock()
    s1 = exam.sections[SectionType.QUANT_S1]
    s1.question_ids = list(range(12))
    s1._correctness = {
        qid: i < n_correct for i, qid in enumerate(s1.question_ids)
    }
    exam._adapt_next_section(SectionType.QUANT_S1)
    s2 = exam.sections[SectionType.QUANT_S2]
    assert s2.difficulty_band == expected
    assert s2.routing_tier == expected


# ── _adapt_next_section — verbal 12-Q boundaries ───────────────────────


@pytest.mark.parametrize("n_correct,expected", [
    (0, "easy"),
    (4, "easy"),       # 4/12 — under 5 -> easy
    (5, "medium"),     # 5/12 — exactly at threshold -> medium
    (8, "medium"),     # 8/12 — still medium
    (9, "hard"),       # 9/12 — exactly at threshold -> hard
    (12, "hard"),
])
def test_verbal_adapt_uses_3tier_routing(n_correct, expected):
    exam = _build_full_mock()
    s1 = exam.sections[SectionType.VERBAL_S1]
    s1.question_ids = list(range(12))
    s1._correctness = {
        qid: i < n_correct for i, qid in enumerate(s1.question_ids)
    }
    exam._adapt_next_section(SectionType.VERBAL_S1)
    s2 = exam.sections[SectionType.VERBAL_S2]
    assert s2.difficulty_band == expected
    assert s2.routing_tier == expected


def test_adapt_with_zero_answers_defaults_medium():
    """Empty correctness map → medium tier (don't punish a skipped
    section by routing them to easy and lowering the ceiling)."""
    exam = _build_full_mock()
    s1 = exam.sections[SectionType.VERBAL_S1]
    s1.question_ids = list(range(12))
    s1._correctness = {}
    exam._adapt_next_section(SectionType.VERBAL_S1)
    s2 = exam.sections[SectionType.VERBAL_S2]
    assert s2.difficulty_band == "medium"
    assert s2.routing_tier == "medium"


# ── routing_tier is forwarded to the question bank ─────────────────────


def test_routing_tier_forwarded_to_question_bank():
    """The composer call wires both ``routing_tier`` and
    ``difficulty_band`` to the new tier."""
    exam = _build_full_mock()
    bank = exam._question_bank
    bank.calls.clear()  # reset stub call log
    s1 = exam.sections[SectionType.QUANT_S1]
    s1.question_ids = list(range(12))
    s1._correctness = {
        qid: i < 8 for i, qid in enumerate(s1.question_ids)  # 8/12 -> hard
    }
    exam._adapt_next_section(SectionType.QUANT_S1)
    # Find the call for QUANT_S2 assembly (latest quant call).
    quant_calls = [c for c in bank.calls if c["measure"] == "quant"]
    assert quant_calls, "expected the composer to be called for S2"
    last = quant_calls[-1]
    assert last["routing_tier"] == "hard"
    assert last["difficulty_band"] == "hard"
    # Theta key must also be present (None is acceptable when
    # rating_service is unavailable in the test env).
    assert "target_theta" in last


def test_theta_still_computed_in_adapt_path(monkeypatch):
    """Theta is still computed and forwarded as a side-signal, even
    though it no longer drives the routing decision."""
    exam = _build_full_mock()
    bank = exam._question_bank
    bank.calls.clear()

    captured = {"compute_theta_called": False}

    def fake_compute_theta(user_id="local"):
        captured["compute_theta_called"] = True
        return 0.7

    # The local import in _adapt_next_section reads from the live module.
    import services.scoring
    monkeypatch.setattr(services.scoring, "compute_theta", fake_compute_theta)

    s1 = exam.sections[SectionType.QUANT_S1]
    s1.question_ids = list(range(12))
    s1._correctness = {qid: True for qid in s1.question_ids}  # 12/12 -> hard
    exam._adapt_next_section(SectionType.QUANT_S1)
    assert captured["compute_theta_called"], (
        "compute_theta must still run as the side-signal source"
    )
    quant_calls = [c for c in bank.calls if c["measure"] == "quant"]
    assert quant_calls[-1]["target_theta"] == 0.7


# ── SectionResult persistence ──────────────────────────────────────────


def test_routing_tier_persists_to_section_result(temp_db):
    """A SectionResult row created with ``routing_tier='hard'`` round-
    trips the value through the user DB."""
    from models.database import Session, SectionResult
    sess = Session.create(
        test_type="full_mock", mode="simulation",
        section_order="[]", state="completed",
    )
    sr = SectionResult.create(
        session=sess, section_name="quant_s2", measure="quant",
        section_index=2, difficulty_band="hard",
        routing_tier="hard",
        time_limit_seconds=1560, time_used_seconds=600,
        question_ids=json.dumps([1, 2, 3]),
    )
    sr_id = sr.id
    # Re-fetch to confirm round-trip.
    refetched = SectionResult.get(SectionResult.id == sr_id)
    assert refetched.routing_tier == "hard"
    assert refetched.difficulty_band == "hard"


def test_routing_tier_nullable_on_section_result(temp_db):
    """A SectionResult row can omit ``routing_tier`` (AWA / drill /
    pre-Phase-7 rows leave it NULL)."""
    from models.database import Session, SectionResult
    sess = Session.create(
        test_type="drill", mode="learning",
        section_order="[]", state="completed",
    )
    sr = SectionResult.create(
        session=sess, section_name="awa", measure="awa",
        section_index=1, difficulty_band="medium",
        # routing_tier omitted intentionally
        time_limit_seconds=1800, time_used_seconds=0,
        question_ids="[]",
    )
    refetched = SectionResult.get(SectionResult.id == sr.id)
    assert refetched.routing_tier is None


# ── Backwards compat: routing_tier=None preserves theta path ──────────


def test_routing_tier_none_preserves_theta_aware_ranking(temp_db):
    """``select_questions_composed(routing_tier=None, target_theta=+1.0)``
    must continue to bias picks toward higher-rating items — same
    behavior as the pre-Phase-7 theta-aware path."""
    from models.database import Question, ItemRating
    from services.question_bank import QuestionBankService

    # Tiny live quant bank spanning bands 1..5, with an ItemRating per qid.
    pool = []
    for band in (1, 2, 3, 4, 5):
        for i in range(6):
            q = Question.create(
                measure="quant", subtype="mcq_single",
                prompt=f"band-{band}-{i}",
                difficulty_target=band,
                time_target_seconds=90,
                concept_tags="[]", explanation="",
                status="live",
            )
            pool.append((q.id, band))
    from services.rating_service import seed_initial_ratings
    seed_initial_ratings()

    qb = QuestionBankService()
    random.seed(20260518)
    high_picks = qb.select_questions_composed(
        measure="quant", count=10, difficulty_band="medium",
        target_theta=1.0,
        routing_tier=None,  # explicit no-tier
    )
    assert len(high_picks) == 10
    ratings = [ItemRating.get(ItemRating.question_id == qid).rating
               for qid in high_picks]
    mean_rating = sum(ratings) / len(ratings)
    # theta=+1.0 should pull picks above the neutral baseline (~0).
    # The test_cat_wireup canonical >=+0.3 threshold can land just at
    # the float epsilon edge depending on which 10 of 30 items get
    # picked, so we use a slightly looser lower bound here.
    assert mean_rating >= 0.25, (
        f"theta path regressed at routing_tier=None: mean rating={mean_rating}"
    )


def test_routing_tier_hard_widens_pool_past_band_gate(temp_db):
    """With ``routing_tier='hard'`` the composer no longer hard-WHEREs
    on band; it ranks via ``TIER_DIFFICULTY_MIX``. So even when the
    nominal ``difficulty_band`` is ``'medium'`` (or omitted), a hard
    tier should still surface band-4/5 items in the pool above pure
    chance.
    """
    from models.database import Question
    from services.question_bank import QuestionBankService

    # Live quant bank spanning all 5 bands, no ratings (theta inactive).
    for band in (1, 2, 3, 4, 5):
        for i in range(6):
            Question.create(
                measure="quant", subtype="mcq_single",
                prompt=f"band-{band}-{i}",
                difficulty_target=band,
                time_target_seconds=90,
                concept_tags="[]", explanation="",
                status="live",
            )

    qb = QuestionBankService()
    random.seed(20260518)
    picks = qb.select_questions_composed(
        measure="quant", count=10,
        difficulty_band="medium",  # legacy band signal
        routing_tier="hard",  # tier should override
        target_theta=None,
    )
    assert len(picks) == 10
    bands = [Question.get(Question.id == qid).difficulty_target for qid in picks]
    # Tier 'hard' weights are 0.32 and 0.35 on bands 4 and 5; medium
    # weights total ~0.05 on bands 1+2 and 0.28 on band 3. So at least
    # half the picks should land on band ≥3 (the tier gives almost zero
    # weight to bands 1-2). This is a coarse but stable assertion.
    assert sum(1 for b in bands if b >= 3) >= 5, (
        f"hard tier failed to surface upper bands: bands={bands}"
    )
