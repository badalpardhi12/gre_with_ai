"""
P3.S1 — section-level CAT wire-up.

The plan lands a theta-aware section assembler that keeps the legacy
band-switch path intact as a fallback. These tests cover the four
behaviors listed in ``docs/implementation_plan_2026_05_12.md`` Phase 3
S1 acceptance:

  * ``select_questions_composed(..., target_theta=+1.0)`` biases picks
    toward higher-rating items.
  * ``target_theta=None`` is a no-op — same machinery as pre-S1.
  * When ``rating_service`` is unreachable, ``target_theta`` is ignored
    gracefully (no crash, no silent skip).
  * ``scoring.compute_theta`` returns 0.0 with no response history and
    lands near +1.0 after 20 correct responses on rating=+1 items.
"""
from __future__ import annotations

import math
import random

import pytest


# ── Helpers ──────────────────────────────────────────────────────────


def _seed_quant_pool():
    """Build a tiny live quant bank spanning difficulty bands 1..5.

    Each item gets an ItemRating at the canonical seed logit for its
    band (-1.2 .. +1.2) so the theta-aware ranker has something to
    score. Returns the list of ``(qid, band)``.
    """
    from models.database import Question, ItemRating

    out = []
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
            out.append((q.id, band))
    from services.rating_service import seed_initial_ratings
    seed_initial_ratings()
    # Sanity: ItemRating exists for each.
    assert ItemRating.select().count() == len(out)
    return out


def _make_session_and_section():
    """Minimal Session + SectionResult so Response rows can be inserted."""
    from models.database import Session, SectionResult
    sess = Session.create(
        test_type="drill", mode="learning",
        section_order="[]", state="in_progress",
    )
    sr = SectionResult.create(
        session=sess, section_name="drill", measure="quant",
        section_index=1, time_limit_seconds=0, question_ids="[]",
    )
    return sess, sr


# ── compute_theta ────────────────────────────────────────────────────


def test_compute_theta_zero_on_empty_history(temp_db):
    """Fresh user with no graded responses — theta is 0.0 exactly."""
    from services.scoring import compute_theta
    assert compute_theta("local") == 0.0


def test_compute_theta_climbs_after_correct_responses_on_hard_items(temp_db):
    """20 correct responses to rating≈+1.2 items → theta lands near +1."""
    from models.database import Response, ItemRating
    from services.rating_service import seed_initial_ratings
    from services.scoring import compute_theta

    pool = _seed_quant_pool()
    # Pin ratings on band-5 items to +1.0 so the expected theta is clean.
    band5 = [qid for qid, band in pool if band == 5]
    for qid in band5:
        row = ItemRating.get(ItemRating.question_id == qid)
        row.rating = 1.0
        row.save()

    sess, sr = _make_session_and_section()
    # Log 20 correct responses across band-5 items.
    for i in range(20):
        qid = band5[i % len(band5)]
        Response.create(
            session=sess, section_result=sr, question=qid,
            response_payload="{}", is_correct=True,
            time_spent_seconds=10,
        )

    theta = compute_theta("local")
    # The estimator averages item ratings on correct responses; our items
    # are all at +1.0, so theta should be ~+1.0 (within a small epsilon
    # accounting for the rating_service clipping at ±3.0).
    assert 0.8 < theta < 1.2, f"expected theta near +1.0, got {theta}"


def test_compute_theta_graceful_when_rating_service_missing(temp_db, monkeypatch):
    """If rating_service import fails, compute_theta falls back to 0.0."""
    import sys
    # Simulate the module being unavailable.
    monkeypatch.setitem(sys.modules, "services.rating_service", None)
    from services.scoring import compute_theta
    # With services.rating_service = None, any attribute access raises,
    # and compute_theta must swallow that to 0.0.
    assert compute_theta("local") == 0.0


# ── select_questions_composed with target_theta ──────────────────────


def test_target_theta_biases_toward_higher_rating_items(temp_db):
    """target_theta=+1.0 should pull picks toward band-4/5 ratings."""
    from models.database import ItemRating
    from services.question_bank import QuestionBankService

    _seed_quant_pool()
    qb = QuestionBankService()

    # Fix the seed so randomesque doesn't mask the effect.
    random.seed(20260512)
    high_picks = qb.select_questions_composed(
        measure="quant", count=10, difficulty_band="medium",
        target_theta=1.0,
    )
    assert len(high_picks) == 10

    ratings = [ItemRating.get(ItemRating.question_id == qid).rating
               for qid in high_picks]
    mean_rating = sum(ratings) / len(ratings)
    # Without theta bias the mean would sit near 0; at theta=+1 the soft
    # weight and info score should pull it above +0.3.
    assert mean_rating >= 0.3, (
        f"expected mean rating >= +0.3 with theta=+1, got {mean_rating:.3f}"
    )


def test_target_theta_none_preserves_pre_s1_path(temp_db):
    """target_theta=None must NOT alter the legacy band-switch behavior.

    We confirm this indirectly: with theta disabled, the hard WHERE
    ``difficulty_target >= 4`` (for ``difficulty_band='hard'``) is still
    applied — so every pick lands at band-4 or band-5.
    """
    from models.database import Question
    from services.question_bank import QuestionBankService

    _seed_quant_pool()
    qb = QuestionBankService()

    random.seed(20260512)
    picks = qb.select_questions_composed(
        measure="quant", count=5, difficulty_band="hard",
        target_theta=None,
    )
    assert len(picks) == 5
    bands = [Question.get(Question.id == qid).difficulty_target
             for qid in picks]
    assert all(b >= 4 for b in bands), (
        f"legacy hard-band path violated by target_theta=None: bands={bands}"
    )


def test_target_theta_graceful_when_ratings_missing(temp_db, monkeypatch):
    """If rating_service returns no ratings for any candidate, we fall
    back to the legacy hard-WHERE band filter so the section ships."""
    from services.question_bank import QuestionBankService
    from services import rating_service

    _seed_quant_pool()
    # Kill all ratings so probe returns None everywhere.
    from models.database import ItemRating
    ItemRating.delete().execute()

    qb = QuestionBankService()
    random.seed(20260512)
    # target_theta=+1.0 but no ratings available — must not raise, must
    # return a full section, and legacy band filter must apply (band=hard
    # → every pick >= 4).
    picks = qb.select_questions_composed(
        measure="quant", count=5, difficulty_band="hard",
        target_theta=1.0,
    )
    assert len(picks) == 5
    from models.database import Question
    bands = [Question.get(Question.id == qid).difficulty_target
             for qid in picks]
    assert all(b >= 4 for b in bands), (
        f"graceful-fallback path violated: bands={bands}"
    )
