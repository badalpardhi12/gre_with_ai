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
from typing import Optional, Tuple

from models.database import FlashcardReview, VocabWord


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
