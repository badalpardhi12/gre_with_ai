"""
Per-subtopic mastery tracking using EWMA (exponentially-weighted moving average).

Mastery score is 0-1 representing how well the user knows a subtopic.
Updated after every answered question. Used by:
- Adaptive next-question selector (target weak subtopics)
- Study plan generator (focus on low-mastery)
- Dashboard heatmap (visualize strengths/weaknesses)
"""
from datetime import datetime
from typing import Optional

from models.database import (
    db, MasteryRecord, Question, Response, SectionResult, Session,
)


# EWMA decay: alpha=0.3 means recent answers weight more
ALPHA = 0.3
# Difficulty weighting: a hard question correct weighs more than easy
DIFFICULTY_WEIGHTS = {1: 0.6, 2: 0.8, 3: 1.0, 4: 1.3, 5: 1.6}


def update_mastery(subtopic: str, is_correct: bool, difficulty: int,
                   user_id: str = "local") -> MasteryRecord:
    """Update mastery for a subtopic after a question response."""
    if not subtopic:
        return None

    rec, _ = MasteryRecord.get_or_create(
        user_id=user_id,
        subtopic=subtopic,
        defaults={
            "attempts": 0,
            "correct": 0,
            "mastery_score": 0.5,  # neutral prior so cold start isn't anchored at 0/1
            "last_attempt_at": datetime.now(),
        },
    )

    rec.attempts += 1
    if is_correct:
        rec.correct += 1

    weight = DIFFICULTY_WEIGHTS.get(difficulty, 1.0)
    # Symmetric scoring around 0.5: correct answers always raise mastery,
    # wrong answers always lower it. Magnitude scales with difficulty so a
    # hard question matters more than an easy one. Previously, a correct
    # easy answer (raw=0.6, normalised=0.375) would *lower* mastery if the
    # current score was already above 0.375.
    delta = 0.5 * weight / 1.6
    new_observation = 0.5 + delta if is_correct else 0.5 - delta

    if rec.attempts == 1:
        # Pull halfway toward the prior so a single attempt doesn't slam
        # the score to ~0 / ~1.
        rec.mastery_score = 0.5 * (rec.mastery_score) + 0.5 * new_observation
    else:
        rec.mastery_score = (1 - ALPHA) * rec.mastery_score + ALPHA * new_observation

    # Clamp into [0, 1] in case of any rounding drift.
    rec.mastery_score = max(0.0, min(1.0, rec.mastery_score))

    rec.last_attempt_at = datetime.now()
    rec.last_updated_at = datetime.now()
    rec.save()
    return rec


def get_mastery(subtopic: str, user_id: str = "local") -> float:
    """Return mastery score 0-1 (default 0 if no record)."""
    rec = MasteryRecord.get_or_none(
        (MasteryRecord.user_id == user_id) & (MasteryRecord.subtopic == subtopic)
    )
    return rec.mastery_score if rec else 0.0


def get_all_mastery(user_id: str = "local") -> dict:
    """Return {subtopic: mastery_score} for all tracked subtopics."""
    out = {}
    for rec in MasteryRecord.select().where(MasteryRecord.user_id == user_id):
        out[rec.subtopic] = rec.mastery_score
    return out


def weakness_ranking(user_id: str = "local", limit: int = 10):
    """Return subtopics with lowest mastery (excluding never-attempted).

    Returns list of (subtopic, mastery_score, attempts) tuples.
    """
    recs = (MasteryRecord.select()
            .where(MasteryRecord.user_id == user_id)
            .order_by(MasteryRecord.mastery_score.asc())
            .limit(limit))
    return [(r.subtopic, r.mastery_score, r.attempts) for r in recs]


def is_mastered(subtopic: str, user_id: str = "local",
                threshold: float = 0.8, min_attempts: int = 10) -> bool:
    """Check if a subtopic is considered mastered."""
    rec = MasteryRecord.get_or_none(
        (MasteryRecord.user_id == user_id) & (MasteryRecord.subtopic == subtopic)
    )
    if rec is None or rec.attempts < min_attempts:
        return False
    return rec.mastery_score >= threshold


def backfill_from_responses(user_id: str = "local"):
    """Recompute mastery from existing Response history.

    Useful after migrations or for initial population from past sessions.
    """
    # Wipe existing records for user
    MasteryRecord.delete().where(MasteryRecord.user_id == user_id).execute()

    responses = (Response
                 .select(Response, Question)
                 .join(Question)
                 .where(Response.is_correct.is_null(False))
                 .order_by(Response.created_at.asc()))

    n = 0
    for r in responses:
        q = r.question
        if not q.subtopic:
            continue
        update_mastery(q.subtopic, r.is_correct, q.difficulty_target, user_id)
        n += 1
    return n
