"""
Elo-style item rating. Bridges prep-book difficulty labels to a
theta-scale signal before IRT calibration has enough data.

Seed:
    ``difficulty_target`` 1-5 maps to -1.2, -0.6, 0.0, +0.6, +1.2 logits.

Update rule (per response, item side only for now):
    E         = 1 / (1 + 10 ** ((item_rating - user_theta) / 0.4))
    new_item  = item_rating + K * (E - actual)    # actual ∈ {0, 1}
    K         = max(K_MIN, K_INITIAL * K_DECAY_PIVOT / (K_DECAY_PIVOT + n))

User theta is estimated from the last N responses as a theta-scale
average: items the user got right push theta up by a fraction of the
item's rating, wrong answers push it down. This is a cheap bridge to
real theta estimation (Phase 2 IRT) and keeps every update local —
no optimisation, no global fit.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from models.database import db, ItemRating, Question, Response
from services.log import get_logger

logger = get_logger("rating_service")


# ── Tunables ──────────────────────────────────────────────────────────

# Adaptive K: start high for uncalibrated items, decay as responses
# accumulate. At n = K_DECAY_PIVOT responses K is ~half of K_INITIAL;
# asymptotes toward K_MIN as n grows.
K_INITIAL = 0.3
K_MIN = 0.05
K_DECAY_PIVOT = 40

# Theta-scale divisor in the Elo expected-score formula. 0.4 logits
# maps roughly to one ``difficulty_target`` band — so a user at
# theta=0 has ~75% expected win over a band-1 item and ~25% over a
# band-5 item, matching prep-book calibration intuition.
THETA_SCALE = 0.4

_INITIAL_RATING_FROM_DIFFICULTY = {
    1: -1.2,
    2: -0.6,
    3: 0.0,
    4: 0.6,
    5: 1.2,
}


def _initial_rating_for(difficulty_target: Optional[int]) -> float:
    if difficulty_target is None:
        return 0.0
    return _INITIAL_RATING_FROM_DIFFICULTY.get(int(difficulty_target), 0.0)


def _compute_k(n_responses: int) -> float:
    """K shrinks as n grows; bounded below by K_MIN."""
    if n_responses < 0:
        n_responses = 0
    adaptive = K_INITIAL * K_DECAY_PIVOT / (K_DECAY_PIVOT + n_responses)
    return max(K_MIN, adaptive)


def _expected_score(item_rating: float, user_theta: float) -> float:
    """Classic Elo expected score with theta-scale divisor."""
    return 1.0 / (1.0 + 10.0 ** ((item_rating - user_theta) / THETA_SCALE))


# ── Public API ────────────────────────────────────────────────────────


def seed_initial_ratings() -> int:
    """Populate ``item_rating`` for every live question. Idempotent.

    Uses an INSERT OR IGNORE against ``(question_id)`` so existing rows
    — including those that have already drifted from seed via real
    responses — are never overwritten. Returns the number of rows
    inserted by this call (0 if fully seeded already).
    """
    before = ItemRating.select().count()
    # Raw SQL mirrors the migration exactly so this function works as a
    # standalone bootstrap (e.g. when called from tests or a future
    # backfill script) without re-implementing the band-to-logit map.
    db.execute_sql(
        "INSERT OR IGNORE INTO itemrating "
        "  (question_id, rating, n_responses, updated_at) "
        "SELECT id, "
        "       CASE difficulty_target "
        "         WHEN 1 THEN -1.2 "
        "         WHEN 2 THEN -0.6 "
        "         WHEN 3 THEN  0.0 "
        "         WHEN 4 THEN  0.6 "
        "         WHEN 5 THEN  1.2 "
        "         ELSE 0.0 END, "
        "       0, "
        "       CURRENT_TIMESTAMP "
        "  FROM question "
        " WHERE status = 'live'"
    )
    after = ItemRating.select().count()
    inserted = max(0, after - before)
    if inserted:
        logger.info("seeded %d ItemRating rows", inserted)
    return inserted


def get_rating(question_id: int) -> Optional[float]:
    """Return current rating for an item, or ``None`` if not seeded."""
    row = ItemRating.get_or_none(ItemRating.question_id == question_id)
    if row is None:
        return None
    return float(row.rating)


def _ensure_rating(question_id: int) -> ItemRating:
    """Fetch the ItemRating row for a question, creating it on demand.

    Handles the edge case where a question is answered before the seed
    migration has run (e.g. a freshly imported item that slipped past
    ``_025_item_rating_2026_05_12``).
    """
    row = ItemRating.get_or_none(ItemRating.question_id == question_id)
    if row is not None:
        return row
    q = Question.get_or_none(Question.id == question_id)
    seed = _initial_rating_for(q.difficulty_target if q else None)
    row = ItemRating.create(
        question_id=question_id,
        rating=seed,
        n_responses=0,
        updated_at=datetime.now(),
    )
    return row


def get_user_theta(user_id: str = "local", window: int = 40) -> float:
    """Estimate user theta from the last ``window`` graded responses.

    Returns 0.0 if the user has no graded responses yet. The estimate is
    a theta-scale average: for each of the user's recent responses, we
    nudge theta toward the item rating when the user answered correctly,
    and away when they answered wrong. This is a stand-in until the
    Phase 2 IRT stack lands; it's stable enough to drive the per-item
    Elo update without oscillating.
    """
    # Pull the most recent `window` graded responses for this user.
    # Response has no user_id column (single-user app); we key on
    # Session.user_id when we add multi-user, but for now "local" always
    # matches — so we just take the last window regardless of user_id.
    # Kept the param in the signature for future-compat.
    del user_id  # unused today; reserved for multi-user rollout
    rows = list(
        Response
        .select(Response.question_id, Response.is_correct)
        .where(Response.is_correct.is_null(False))
        .order_by(Response.created_at.desc())
        .limit(window)
    )
    if not rows:
        return 0.0

    # Average theta signal: item rating if correct, -rating if wrong.
    # Clip item rating to the seed range so a single wildly-drifted item
    # can't dominate the average.
    signals = []
    for r in rows:
        item = ItemRating.get_or_none(
            ItemRating.question_id == r.question_id
        )
        if item is None:
            continue
        rating = max(-3.0, min(3.0, float(item.rating)))
        # If the user got a hard item right, theta is at least that hard.
        # If they got an easy item wrong, theta is at most that easy.
        signals.append(rating if r.is_correct else -rating)
    if not signals:
        return 0.0
    return sum(signals) / len(signals)


def update_on_response(
    user_id: str,
    question_id: int,
    is_correct: bool,
) -> None:
    """Apply an Elo update to the item rating for one graded response.

    Item-side only (user side is deferred until we have a real theta
    tracker). Called fire-and-forget after a ``Response`` row is
    committed; the caller is expected to wrap this in try/except so a
    rating-service failure never blocks answer submission.
    """
    if is_correct is None:
        return  # ungraded — AWA or skipped response

    item = _ensure_rating(question_id)
    theta = get_user_theta(user_id=user_id)
    expected = _expected_score(item.rating, theta)
    actual = 1.0 if is_correct else 0.0
    k = _compute_k(item.n_responses)

    item.rating = float(item.rating) + k * (expected - actual)
    item.n_responses = int(item.n_responses) + 1
    item.updated_at = datetime.now()
    item.save()
