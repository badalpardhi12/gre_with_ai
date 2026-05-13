"""
P2.E4 — Elo item rating service tests.

Covers:
  * Seed idempotency (re-running ``seed_initial_ratings`` is a no-op).
  * Update direction: correct answer lowers item rating below theta,
    wrong answer raises it.
  * Convergence: a theta=0 simulated user answering a mixed-difficulty
    pool keeps ratings near the seed band — no runaway drift.
  * K decays as n_responses grows (Polya-urn style).
"""
from __future__ import annotations

import math
import random

import pytest


# ── Helpers ──────────────────────────────────────────────────────────


def _seed_questions(bands=(1, 2, 3, 4, 5), per_band=4):
    """Create live questions across difficulty bands; return list of (qid, band)."""
    from models.database import Question
    out = []
    for band in bands:
        for i in range(per_band):
            q = Question.create(
                measure="quant", subtype="mcq_single",
                prompt=f"band-{band}-#{i}",
                difficulty_target=band,
                time_target_seconds=90,
                concept_tags="[]", explanation="",
                status="live",
            )
            out.append((q.id, band))
    return out


# ── Tests ────────────────────────────────────────────────────────────


def test_migration_seeds_item_rating(temp_db):
    """``init_db`` runs the migration which seeds one ItemRating per live Q."""
    from models.database import ItemRating
    # No questions yet ⇒ no ItemRating rows.
    assert ItemRating.select().count() == 0

    _seed_questions()

    # Re-run the seed explicitly (migration ran on empty DB in conftest).
    from services.rating_service import seed_initial_ratings
    inserted = seed_initial_ratings()
    assert inserted == 20
    assert ItemRating.select().count() == 20

    # Band 1 → -1.2, band 5 → +1.2.
    from models.database import Question
    q1 = Question.get(Question.difficulty_target == 1)
    q5 = Question.get(Question.difficulty_target == 5)
    r1 = ItemRating.get(ItemRating.question_id == q1.id)
    r5 = ItemRating.get(ItemRating.question_id == q5.id)
    assert r1.rating == pytest.approx(-1.2, abs=1e-6)
    assert r5.rating == pytest.approx(1.2, abs=1e-6)


def test_seed_initial_ratings_idempotent(temp_db):
    """Re-running the seeder neither duplicates rows nor overwrites values."""
    from models.database import ItemRating
    from services.rating_service import seed_initial_ratings

    _seed_questions()
    seed_initial_ratings()
    count_first = ItemRating.select().count()
    assert count_first == 20

    # Mutate a rating; re-seeding must not revert it.
    row = ItemRating.get(ItemRating.question_id == ItemRating.select().first().question_id)
    row.rating = 0.42
    row.n_responses = 7
    row.save()

    inserted = seed_initial_ratings()
    assert inserted == 0
    assert ItemRating.select().count() == count_first

    refetched = ItemRating.get(ItemRating.id == row.id)
    assert refetched.rating == pytest.approx(0.42)
    assert refetched.n_responses == 7


def test_update_on_correct_answer_lowers_item_rating(temp_db):
    """Correct answer at theta=0 → item rating shifts down toward user."""
    from models.database import ItemRating
    from services.rating_service import (
        seed_initial_ratings, update_on_response, get_user_theta,
    )

    qs = _seed_questions(bands=(4,), per_band=1)
    qid, _ = qs[0]
    seed_initial_ratings()

    before = ItemRating.get(ItemRating.question_id == qid).rating
    assert before == pytest.approx(0.6)
    assert get_user_theta() == 0.0  # no responses yet

    update_on_response(user_id="local", question_id=qid, is_correct=True)

    after = ItemRating.get(ItemRating.question_id == qid).rating
    assert after < before, (
        f"correct answer at theta=0 should lower item rating (was {before}, "
        f"now {after})"
    )


def test_update_on_wrong_answer_raises_item_rating(temp_db):
    """Wrong answer at theta=0 on an easy item → rating shifts up."""
    from models.database import ItemRating
    from services.rating_service import (
        seed_initial_ratings, update_on_response,
    )

    qs = _seed_questions(bands=(2,), per_band=1)
    qid, _ = qs[0]
    seed_initial_ratings()

    before = ItemRating.get(ItemRating.question_id == qid).rating
    assert before == pytest.approx(-0.6)

    update_on_response(user_id="local", question_id=qid, is_correct=False)

    after = ItemRating.get(ItemRating.question_id == qid).rating
    assert after > before, (
        f"wrong answer should raise item rating (was {before}, now {after})"
    )


def test_k_decays_with_response_count(temp_db):
    """K adaptive: high early, settles toward K_MIN after many responses."""
    from services.rating_service import _compute_k, K_INITIAL, K_MIN

    k_new = _compute_k(0)
    k_mid = _compute_k(40)
    k_old = _compute_k(1000)

    assert k_new == pytest.approx(K_INITIAL)
    assert k_mid < k_new
    assert k_old < k_mid
    assert k_old >= K_MIN
    # Floor honoured.
    assert _compute_k(10_000_000) == pytest.approx(K_MIN)


def test_simulated_theta_zero_user_keeps_ratings_near_seed(temp_db):
    """100 responses from a theta=0 user → per-band mean within 0.2 of seed.

    Simulates graded responses against a 25-item bank where each item's
    ``p_correct = sigmoid((theta - seed_rating) / THETA_SCALE)``. The
    simulated theta uses the *same* estimator the service uses so the
    generation process and the update rule are self-consistent — a
    theta estimate that drifts up during the run correspondingly lowers
    the correctness rate, keeping item-rating drift bounded.
    """
    from models.database import ItemRating, Response, SectionResult, Session
    from services.rating_service import (
        seed_initial_ratings, update_on_response, get_user_theta,
        THETA_SCALE,
    )

    rng = random.Random(20260512)
    qs = _seed_questions(bands=(1, 2, 3, 4, 5), per_band=5)
    seed_initial_ratings()

    # Baseline — seed values per band.
    seed_by_band = {1: -1.2, 2: -0.6, 3: 0.0, 4: 0.6, 5: 1.2}

    # We need a Response row per update for get_user_theta() to observe
    # history, plus a Session/SectionResult because Response has NOT NULL
    # FKs to both.
    sess = Session.create(test_type="drill", mode="learning",
                          section_order="[]", state="in_progress")
    sr = SectionResult.create(
        session=sess, section_name="drill", measure="quant",
        section_index=1, time_limit_seconds=0, question_ids="[]",
    )

    # 100 response rounds: random qid each round.
    bank = [qid for qid, _ in qs]
    for _ in range(100):
        qid = rng.choice(bank)
        item = ItemRating.get(ItemRating.question_id == qid)
        theta = get_user_theta()  # what the service will see
        p_correct = 1.0 / (1.0 + math.exp(
            (float(item.rating) - theta) / THETA_SCALE * math.log(10)
        ))
        is_correct = rng.random() < p_correct

        Response.create(
            session=sess, section_result=sr, question=qid,
            response_payload="{}", is_correct=is_correct,
            time_spent_seconds=10,
        )
        update_on_response(user_id="local", question_id=qid,
                           is_correct=is_correct)

    # Per-band mean should stay near seed (within 0.2 logits).
    band_means = {}
    for qid, band in qs:
        r = ItemRating.get(ItemRating.question_id == qid).rating
        band_means.setdefault(band, []).append(r)

    for band, vals in band_means.items():
        mean = sum(vals) / len(vals)
        assert abs(mean - seed_by_band[band]) < 0.2, (
            f"band {band}: mean rating {mean:.3f} drifted from seed "
            f"{seed_by_band[band]:.3f} by more than 0.2"
        )


def test_get_rating_returns_none_for_unknown(temp_db):
    from services.rating_service import get_rating
    assert get_rating(999_999) is None


def test_ensure_rating_autoseeds_missing_question(temp_db):
    """A question answered before its seed row exists is still rated."""
    from models.database import Question, ItemRating
    from services.rating_service import update_on_response, get_rating

    q = Question.create(
        measure="quant", subtype="mcq_single", prompt="orphan",
        difficulty_target=4, time_target_seconds=90,
        concept_tags="[]", explanation="", status="live",
    )
    # Delete any ItemRating the migration might have auto-seeded.
    ItemRating.delete().where(ItemRating.question_id == q.id).execute()
    assert get_rating(q.id) is None

    update_on_response(user_id="local", question_id=q.id, is_correct=True)

    rating = get_rating(q.id)
    assert rating is not None
    # Seeded at 0.6, correct answer at theta=0 should drop it.
    assert rating < 0.6
