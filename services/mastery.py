"""
Per-subtopic mastery tracking using EWMA (exponentially-weighted moving average).

Mastery score is 0-1 representing how well the user knows a subtopic.
Updated after every answered question. Used by:
- Adaptive next-question selector (target weak subtopics)
- Study plan generator (focus on low-mastery)
- Dashboard heatmap (visualize strengths/weaknesses)

Time-based forgetting curve (P3.S4):
Raw mastery in the DB is the "peak strength" at the moment of the most recent
attempt. For planning / heatmap purposes we apply an exponential decay
parameterized by FORGETTING_HALF_LIFE_DAYS — mastery halves for every
half-life window elapsed since the last exposure. The raw score is never
mutated on disk; decay is computed at read time so a single correct answer
immediately resets the clock.
"""
from datetime import datetime
from typing import List, Optional

from models.database import (
    db, MasteryRecord, Question, Response, SectionResult, Session,
)


# EWMA decay: alpha=0.3 means recent answers weight more
ALPHA = 0.3
# Difficulty weighting: a hard question correct weighs more than easy
DIFFICULTY_WEIGHTS = {1: 0.6, 2: 0.8, 3: 1.0, 4: 1.3, 5: 1.6}

# Forgetting curve: mastery halves every 14 days of no exposure.
FORGETTING_HALF_LIFE_DAYS = 14


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


def decayed_weakness_ranking(user_id: str = "local", limit: int = 10,
                             now: Optional[datetime] = None):
    """Weakness ranking that incorporates the forgetting curve.

    Same shape as :func:`weakness_ranking` but the score column is the
    time-decayed mastery — so a formerly-strong subtopic the student hasn't
    touched in weeks rises in priority over a freshly-drilled one.
    Only subtopics with at least one attempt are returned.
    """
    now = now or datetime.now()
    recs = (MasteryRecord.select()
            .where((MasteryRecord.user_id == user_id) &
                   (MasteryRecord.attempts > 0)))
    rows = []
    for r in recs:
        if r.last_attempt_at is None:
            continue
        days = _days_since(r.last_attempt_at, now)
        rows.append((r.subtopic, _apply_decay(r.mastery_score, days),
                     r.attempts))
    rows.sort(key=lambda t: (t[1], t[0]))
    return rows[:limit]


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


# ── Forgetting-curve decay (P3.S4) ───────────────────────────────────

def _days_since(ts: Optional[datetime], now: Optional[datetime] = None) -> Optional[float]:
    """Elapsed days between ``ts`` and ``now`` (defaults to wall clock).

    Returns ``None`` if ``ts`` is missing. A negative delta (clock skew /
    just-now attempt) is clamped to 0 so decay never boosts mastery.
    """
    if ts is None:
        return None
    now = now or datetime.now()
    delta = (now - ts).total_seconds() / 86400.0
    return max(0.0, delta)


def _apply_decay(raw: float, days: Optional[float]) -> float:
    """Apply the half-life decay curve. Never-seen subtopics → 0.0."""
    if days is None:
        return 0.0
    if days <= 0:
        return raw
    return raw * (0.5 ** (days / FORGETTING_HALF_LIFE_DAYS))


def decayed_mastery(user_id: str, subtopic: str,
                    now: Optional[datetime] = None) -> float:
    """Return mastery with time-decay applied.

    stored_mastery * 0.5 ** (days_since_last_seen / HALF_LIFE)

    If the subtopic has never been seen (no MasteryRecord, or no
    last_attempt_at), returns 0.0.
    """
    if not subtopic:
        return 0.0
    rec = MasteryRecord.get_or_none(
        (MasteryRecord.user_id == user_id) & (MasteryRecord.subtopic == subtopic)
    )
    if rec is None or rec.last_attempt_at is None:
        return 0.0
    days = _days_since(rec.last_attempt_at, now)
    return _apply_decay(rec.mastery_score, days)


def heatmap_data(user_id: str = "local",
                 now: Optional[datetime] = None) -> List[dict]:
    """Return per-subtopic decay + freshness rows for the Insights heatmap.

    Every subtopic that exists in the question bank OR has any mastery
    history shows up. Rows sorted by ``mastery_decayed`` ascending so the
    weakest + most-forgotten subtopics appear first.
    """
    now = now or datetime.now()

    bank_subs = {
        q.subtopic for q in
        Question.select(Question.subtopic)
                .where((Question.subtopic.is_null(False)) &
                       (Question.subtopic != ""))
                .distinct()
    }

    recs = {
        r.subtopic: r for r in
        MasteryRecord.select().where(MasteryRecord.user_id == user_id)
    }

    all_subs = set(bank_subs) | set(recs.keys())
    rows: List[dict] = []
    for sub in all_subs:
        rec = recs.get(sub)
        if rec is None or rec.last_attempt_at is None:
            rows.append({
                "subtopic": sub,
                "mastery_raw": 0.0,
                "mastery_decayed": 0.0,
                "days_since_seen": None,
                "n_responses": rec.attempts if rec else 0,
            })
            continue
        days = _days_since(rec.last_attempt_at, now)
        rows.append({
            "subtopic": sub,
            "mastery_raw": rec.mastery_score,
            "mastery_decayed": _apply_decay(rec.mastery_score, days),
            "days_since_seen": days,
            "n_responses": rec.attempts,
        })

    rows.sort(key=lambda r: (r["mastery_decayed"], r["subtopic"]))
    return rows


def get_all_decayed_mastery(user_id: str = "local",
                            now: Optional[datetime] = None) -> dict:
    """Return {subtopic: mastery_decayed} for every tracked subtopic.

    Mirrors :func:`get_all_mastery` but applies the forgetting-curve decay.
    Subtopics without a recorded last_attempt_at are omitted (their decayed
    mastery is 0 by definition and study-plan callers already have to
    handle "unseen" specially).
    """
    now = now or datetime.now()
    out = {}
    for rec in MasteryRecord.select().where(MasteryRecord.user_id == user_id):
        if rec.last_attempt_at is None:
            continue
        days = _days_since(rec.last_attempt_at, now)
        out[rec.subtopic] = _apply_decay(rec.mastery_score, days)
    return out
