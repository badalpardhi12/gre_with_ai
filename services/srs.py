"""
Spaced Repetition Scheduler — uses a simplified FSRS-inspired algorithm.

Tracks per-word review state (stability, difficulty) and computes the next review date
based on user response quality. Conservative defaults; tuned for vocab learning.

Response codes:
  1 = Again (forgot completely)
  2 = Hard (recalled with difficulty)
  3 = Good (recalled correctly)
  4 = Easy (recalled effortlessly)
"""
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from models.database import FlashcardReview, ItemReview, VocabWord


# FSRS-inspired constants (tuned for vocab learning)
MIN_INTERVAL = 1
MAX_INTERVAL = 365
EASY_BONUS = 1.3


def update_review(card: FlashcardReview, response: int) -> FlashcardReview:
    """Apply user response to a card and schedule its next review.

    Args:
        card: an existing FlashcardReview row
        response: 1=again, 2=hard, 3=good, 4=easy

    Returns: the updated card (already saved)
    """
    if response not in (1, 2, 3, 4):
        raise ValueError(f"Invalid response code: {response}")

    now = datetime.now()
    card.review_count += 1
    card.last_response = response
    card.last_reviewed_at = now

    # Update difficulty (0-10 scale, lower is easier)
    if response == 1:  # Again
        card.difficulty = min(10.0, card.difficulty + 1.5)
    elif response == 2:  # Hard
        card.difficulty = min(10.0, card.difficulty + 0.5)
    elif response == 3:  # Good
        card.difficulty = max(1.0, card.difficulty - 0.1)
    elif response == 4:  # Easy
        card.difficulty = max(1.0, card.difficulty - 0.5)

    # Update stability (in days)
    if response == 1:
        card.stability = max(1.0, card.stability * 0.5)
        new_interval = 1
    elif response == 2:
        card.stability = card.stability * 1.2
        new_interval = max(1, int(card.stability * 0.8))
    elif response == 3:
        card.stability = card.stability * (2.5 - 0.1 * card.difficulty)
        new_interval = max(1, int(card.stability))
    else:  # easy
        card.stability = card.stability * (2.5 - 0.1 * card.difficulty) * EASY_BONUS
        new_interval = max(1, int(card.stability * EASY_BONUS))

    new_interval = max(MIN_INTERVAL, min(MAX_INTERVAL, new_interval))
    card.interval_days = new_interval
    card.next_review_at = now + timedelta(days=new_interval)
    card.save()
    return card


def get_or_create_review(word: VocabWord, user_id: str = "local") -> FlashcardReview:
    """Get the review record for a word, creating if absent."""
    card = FlashcardReview.get_or_none(
        (FlashcardReview.word == word) & (FlashcardReview.user_id == user_id)
    )
    if card is None:
        card = FlashcardReview.create(
            word=word,
            user_id=user_id,
            review_count=0,
            ease_factor=2.5,
            interval_days=1,
            stability=1.0,
            difficulty=5.0,
            next_review_at=datetime.now(),
        )
    return card


def due_cards(user_id: str = "local", limit: Optional[int] = None):
    """Return cards due for review (next_review_at <= now), oldest first."""
    now = datetime.now()
    query = (FlashcardReview.select()
             .where((FlashcardReview.user_id == user_id) &
                    (FlashcardReview.next_review_at <= now))
             .order_by(FlashcardReview.next_review_at.asc()))
    if limit:
        query = query.limit(limit)
    return list(query)


def new_cards(user_id: str = "local", limit: int = 20,
              tier_filter: Optional[int] = None):
    """Return new (never-reviewed) words for the user.

    Smart ordering:
    - Prefer lower-tier (more important) words first
    - Within a tier, randomize so the user doesn't always see the same
      alphabetical run on consecutive sessions
    - Filter out words with no definition AND words marked retired (status='retired')

    Args:
        tier_filter: if given, only return words at this frequency_tier (1=most common)
    """
    from peewee import fn
    # NOT EXISTS subquery instead of `id NOT IN (...long list...)` so we don't
    # blow past SQLite's 999-parameter limit once the user has reviewed many
    # words.
    reviewed_subq = (FlashcardReview
                     .select(FlashcardReview.word_id)
                     .where((FlashcardReview.user_id == user_id) &
                            (FlashcardReview.word_id == VocabWord.id)))

    query = (VocabWord.select()
             .where(~fn.EXISTS(reviewed_subq))
             .where(VocabWord.definition != "")
             .where(VocabWord.definition.is_null(False)))

    # Exclude retired words from active study (use source field as marker)
    query = query.where(~VocabWord.source.contains("retired"))

    if tier_filter:
        query = query.where(VocabWord.frequency_tier == tier_filter)

    # Order: tier first (lowest = most common), then random
    query = query.order_by(VocabWord.frequency_tier.asc(), fn.Random())

    return list(query.limit(limit))


def daily_session(user_id: str = "local",
                  new_count: int = 20,
                  tier_filter: Optional[int] = None) -> Tuple[list, list]:
    """Build today's flashcard session: due reviews + N new cards.

    Returns: (due_cards, new_words) — UI presents them in order
    """
    due = due_cards(user_id=user_id)
    new = new_cards(user_id=user_id, limit=new_count, tier_filter=tier_filter)
    return due, new


def stats(user_id: str = "local") -> dict:
    """Return session-level stats for the user."""
    # Only count words with definitions as the "real" bank
    total_words = VocabWord.select().where(
        (VocabWord.definition != "") & (VocabWord.definition.is_null(False))
    ).count()
    reviewed = (FlashcardReview.select()
                .where(FlashcardReview.user_id == user_id).count())
    mastered = (FlashcardReview.select()
                .where((FlashcardReview.user_id == user_id) &
                       (FlashcardReview.interval_days >= 30)).count())
    due_today = len(due_cards(user_id=user_id))
    return {
        "total_words": total_words,
        "reviewed": reviewed,
        "mastered": mastered,
        "due_today": due_today,
        "remaining_to_learn": total_words - reviewed,
    }


# ─────────────────────────────────────────────────────────────────────
# ITEM-SCOPE FSRS (P2.E2) — parallel to the vocab scheduler above.
#
# Shares the 1..4 rating contract (Again/Hard/Good/Easy). Writes to
# ``ItemReview`` keyed on (user_id, question_id). Hand-rolled to avoid
# the ``fsrs>=5.x`` Python-3.10+ constraint — the algorithm is a thin
# generalization of the vocab scheduler above with an explicit
# ``state`` machine (new → learning → review, and review → relearning
# on a lapse).
# ─────────────────────────────────────────────────────────────────────

# FSRS-style defaults, tuned so item review slots naturally into a
# prep schedule (conservative initial stability, aggressive-ish growth
# after a correct review).
_ITEM_INITIAL_STABILITY = {1: 0.4, 2: 0.6, 3: 1.0, 4: 2.0}  # days, from "new"
_ITEM_LEARNING_STEP_MIN = 10          # re-show an Again/Hard learner in ~10 min
_ITEM_LAPSE_INTERVAL_DAYS = 1         # Again on a mature card → tomorrow


def _clamp_interval(days: float) -> int:
    """Clamp a float-days interval to the shared [MIN, MAX] day window."""
    return max(MIN_INTERVAL, min(MAX_INTERVAL, int(round(days))))


def _get_or_create_item_review(user_id: str, question_id: int) -> ItemReview:
    """Return the ItemReview for (user, question), creating a fresh
    ``state='new'`` row if none exists. Never saves on the create path
    until the caller mutates — callers always mutate, so the row is
    persisted by ``review_item`` / ``schedule_redo`` below."""
    row = ItemReview.get_or_none(
        (ItemReview.user_id == user_id) &
        (ItemReview.question_id == question_id)
    )
    if row is None:
        row = ItemReview(
            user_id=user_id,
            question_id=question_id,
            state="new",
            stability=0.0,
            difficulty=5.0,
            n_reviews=0,
            n_lapses=0,
        )
    return row


def review_item(user_id: str, question_id: int, rating: int) -> dict:
    """Record an FSRS review for an item and schedule the next one.

    Args:
        user_id: owner of the review row (single-user app → "local").
        question_id: ``Question.id`` being reviewed.
        rating: 1=Again, 2=Hard, 3=Good, 4=Easy.

    State machine:
        new        →  rating 1/2 → learning   (next shown in minutes)
                   →  rating 3/4 → review     (next shown in days)
        learning   →  rating 1/2 → learning   (stays in short loop)
                   →  rating 3/4 → review
        review     →  rating 1   → relearning (lapse; ~1 day)
                   →  rating 2/3/4 → review   (stability grows)
        relearning →  rating 1/2 → relearning
                   →  rating 3/4 → review

    Returns the updated row as a plain dict (easier to assert in tests
    and to serialize for the UI; the saved model is the source of truth).
    """
    if rating not in (1, 2, 3, 4):
        raise ValueError(f"Invalid rating: {rating!r} (expected 1..4)")

    now = datetime.now()
    row = _get_or_create_item_review(user_id, question_id)
    prev_state = row.state
    row.n_reviews += 1
    row.last_review_at = now

    # Difficulty drift — same shape as the vocab scheduler.
    if rating == 1:
        row.difficulty = min(10.0, row.difficulty + 1.5)
    elif rating == 2:
        row.difficulty = min(10.0, row.difficulty + 0.5)
    elif rating == 3:
        row.difficulty = max(1.0, row.difficulty - 0.1)
    else:
        row.difficulty = max(1.0, row.difficulty - 0.5)

    # State transition + stability + due-date.
    if prev_state in ("new", "learning"):
        if rating <= 2:
            # Stay (or enter) learning. Short re-show window.
            row.state = "learning"
            # Seed stability so the first graduation step isn't zero.
            row.stability = max(row.stability, _ITEM_INITIAL_STABILITY[rating])
            row.next_due_at = now + timedelta(minutes=_ITEM_LEARNING_STEP_MIN)
        else:
            # Graduate to review.
            row.state = "review"
            row.stability = _ITEM_INITIAL_STABILITY[rating]
            row.next_due_at = now + timedelta(
                days=_clamp_interval(row.stability)
            )

    elif prev_state == "review":
        if rating == 1:
            row.n_lapses += 1
            row.state = "relearning"
            row.stability = max(1.0, row.stability * 0.5)
            row.next_due_at = now + timedelta(days=_ITEM_LAPSE_INTERVAL_DAYS)
        elif rating == 2:
            row.stability = row.stability * 1.2
            row.next_due_at = now + timedelta(
                days=_clamp_interval(row.stability * 0.8)
            )
        elif rating == 3:
            row.stability = row.stability * (2.5 - 0.1 * row.difficulty)
            row.next_due_at = now + timedelta(
                days=_clamp_interval(row.stability)
            )
        else:  # Easy
            row.stability = (
                row.stability * (2.5 - 0.1 * row.difficulty) * EASY_BONUS
            )
            row.next_due_at = now + timedelta(
                days=_clamp_interval(row.stability * EASY_BONUS)
            )

    else:  # relearning
        if rating <= 2:
            row.state = "relearning"
            row.next_due_at = now + timedelta(minutes=_ITEM_LEARNING_STEP_MIN)
        else:
            row.state = "review"
            # Fresh stability seed after a lapse loop.
            row.stability = max(row.stability, _ITEM_INITIAL_STABILITY[rating])
            row.next_due_at = now + timedelta(
                days=_clamp_interval(row.stability)
            )

    row.save()
    return {
        "user_id": row.user_id,
        "question_id": row.question_id,
        "state": row.state,
        "stability": row.stability,
        "difficulty": row.difficulty,
        "next_due_at": row.next_due_at,
        "last_review_at": row.last_review_at,
        "n_reviews": row.n_reviews,
        "n_lapses": row.n_lapses,
    }


def get_due_items(user_id: str = "local", limit: int = 20) -> List[int]:
    """Return question IDs whose ``next_due_at <= now`` for ``user_id``,
    ordered by urgency (oldest due first). ``state='new'`` rows with a
    non-null ``next_due_at`` are included; rows that have never been
    scheduled (``next_due_at IS NULL``) are intentionally skipped so a
    freshly-inserted row only surfaces once it's actually due."""
    now = datetime.now()
    query = (ItemReview
             .select(ItemReview.question_id)
             .where((ItemReview.user_id == user_id) &
                    (ItemReview.next_due_at.is_null(False)) &
                    (ItemReview.next_due_at <= now))
             .order_by(ItemReview.next_due_at.asc())
             .limit(max(1, int(limit))))
    return [r.question_id for r in query]


def due_items_count(user_id: str = "local") -> int:
    """Count items currently due for review (for the practice-screen
    "Due for Review (N)" tile)."""
    now = datetime.now()
    return (ItemReview
            .select()
            .where((ItemReview.user_id == user_id) &
                   (ItemReview.next_due_at.is_null(False)) &
                   (ItemReview.next_due_at <= now))
            .count())


def schedule_redo(user_id: str, question_id: int) -> ItemReview:
    """Create-or-reset the ItemReview for (user, question) so it surfaces
    in the very next review queue.

    Called by the error-log "Schedule Redo" button. Semantically
    equivalent to a rating=1 (Again) review — forces the card into the
    ``learning`` state with a short re-show window — but we use an
    explicit path so that calling it on an item the user has never seen
    doesn't inflate their lapse count.
    """
    now = datetime.now()
    row = _get_or_create_item_review(user_id, question_id)
    row.state = "learning"
    # Keep historical difficulty but pin stability low so the card is
    # treated as needing reinforcement.
    row.stability = max(row.stability, _ITEM_INITIAL_STABILITY[1])
    # Do NOT bump n_reviews — the user hasn't re-attempted yet.
    row.next_due_at = now + timedelta(minutes=_ITEM_LEARNING_STEP_MIN)
    row.save()
    return row
