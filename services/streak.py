"""
Streak + daily-goal habit tracking.

Habit formation is the highest-leverage retention mechanic for any learning
app — it's why Duolingo's home screen anchors to "your streak" rather than
"all the lessons available". For an adult test-prep audience we keep it
subtle: a flame icon + day count, no XP / hearts / level-ups.

Public API:
- `record_activity(user_id="local")` — call once per finished
  drill / vocab session / mock / section. Idempotent within a single day.
- `get_stats(user_id="local")` → dict with current_streak, longest_streak,
  freezes_left, last_active_date.
- `today_progress(user_id="local")` → dict with minutes_done /
  minutes_goal / items_done / items_planned for the goal-completion bar.
- `streak_label(user_id="local")` → e.g. "🔥 12-day streak" or "" when 0.
"""
from datetime import date, datetime, timedelta
from typing import Optional

from models.database import db, UserStats
from services.log import get_logger

logger = get_logger("streak")


MAX_FREEZES = 3
# Cap on how often we top-up the freeze pool. Once per Sunday: at most one
# freeze accrues per calendar week.
WEEKLY_FREEZE_TOP_UP_WEEKDAY = 6  # Sunday


def _get_or_create(user_id: str) -> UserStats:
    rec, created = UserStats.get_or_create(
        user_id=user_id,
        defaults={
            "current_streak": 0,
            "longest_streak": 0,
            "streak_freezes_left": 1,
            "daily_goal_minutes": 20,
        },
    )
    if created:
        logger.info("created UserStats row for %s", user_id)
    return rec


def record_activity(user_id: str = "local",
                    today: Optional[date] = None) -> UserStats:
    """Update the streak ledger.

    `today` is injectable for tests; defaults to wall-clock date.
    Returns the updated UserStats row.
    """
    today = today or date.today()
    rec = _get_or_create(user_id)

    # Top up the freeze pool if it's Sunday and we haven't already done so
    # this week (we infer "already topped up" from last_active_date being
    # today).
    if today.weekday() == WEEKLY_FREEZE_TOP_UP_WEEKDAY and rec.last_active_date != today:
        rec.streak_freezes_left = min(MAX_FREEZES, rec.streak_freezes_left + 1)

    last = rec.last_active_date
    if last == today:
        # Already counted today.
        rec.save()
        return rec

    if last is None:
        rec.current_streak = 1
    else:
        gap = (today - last).days
        if gap == 1:
            rec.current_streak += 1
        elif gap > 1:
            # Did the user miss day(s)? Burn freezes if available.
            missed = gap - 1
            if missed <= rec.streak_freezes_left:
                rec.streak_freezes_left -= missed
                rec.current_streak += 1
            else:
                rec.current_streak = 1
        # gap == 0 handled above; gap < 0 means clock skew → treat as today.

    rec.longest_streak = max(rec.longest_streak, rec.current_streak)
    rec.last_active_date = today
    rec.save()
    logger.info("activity recorded for %s, streak=%d", user_id, rec.current_streak)
    return rec


def get_stats(user_id: str = "local") -> dict:
    """Return a serializable snapshot of streak state."""
    rec = _get_or_create(user_id)
    return {
        "current_streak": rec.current_streak,
        "longest_streak": rec.longest_streak,
        "streak_freezes_left": rec.streak_freezes_left,
        "last_active_date": rec.last_active_date.isoformat() if rec.last_active_date else None,
        "daily_goal_minutes": rec.daily_goal_minutes,
        "onboarding_completed_at": (
            rec.onboarding_completed_at.isoformat()
            if rec.onboarding_completed_at else None
        ),
    }


def today_progress(user_id: str = "local") -> dict:
    """Daily-goal completion bar source.

    Returns minutes done / minutes goal — minutes done is computed from
    today's Response.time_spent_seconds.
    """
    from models.database import Response
    rec = _get_or_create(user_id)
    start = datetime.combine(date.today(), datetime.min.time())
    end = start + timedelta(days=1)
    minutes = (Response
               .select(Response.time_spent_seconds)
               .where((Response.created_at >= start) &
                      (Response.created_at < end))
               .scalar(as_tuple=False, convert=False))
    # Sum manually because Peewee's .scalar() of a SUM is fiddly across versions.
    total_seconds = sum(
        r.time_spent_seconds or 0
        for r in Response.select(Response.time_spent_seconds).where(
            (Response.created_at >= start) & (Response.created_at < end)
        )
    )
    minutes_done = total_seconds // 60
    return {
        "minutes_done": minutes_done,
        "minutes_goal": rec.daily_goal_minutes,
        "fraction": min(1.0, minutes_done / max(1, rec.daily_goal_minutes)),
    }


def streak_label(user_id: str = "local") -> str:
    """Sidebar-ready label. Empty string if streak is 0."""
    rec = _get_or_create(user_id)
    if rec.current_streak <= 0:
        return ""
    if rec.current_streak == 1:
        return "🔥 1-day streak"
    return f"🔥 {rec.current_streak}-day streak"


def mark_onboarding_complete(user_id: str = "local") -> None:
    rec = _get_or_create(user_id)
    rec.onboarding_completed_at = datetime.now()
    rec.save()


def is_onboarded(user_id: str = "local") -> bool:
    rec = _get_or_create(user_id)
    return rec.onboarding_completed_at is not None
