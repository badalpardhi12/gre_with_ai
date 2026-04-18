"""
Streak ledger tests — exercises the gap / freeze / longest-streak logic.

Uses the temp_db fixture so we don't touch the user's real UserStats row.
"""
from datetime import date, timedelta

import pytest


@pytest.fixture
def streak_module(temp_db):
    """Lazy-import after DB is patched so the migrator + UserStats bind to tmp DB."""
    from services import streak
    return streak


def test_first_activity_creates_streak_of_one(streak_module):
    rec = streak_module.record_activity(today=date(2026, 4, 18))
    assert rec.current_streak == 1
    assert rec.longest_streak == 1
    assert rec.last_active_date == date(2026, 4, 18)


def test_consecutive_days_grow_streak(streak_module):
    streak_module.record_activity(today=date(2026, 4, 18))
    rec = streak_module.record_activity(today=date(2026, 4, 19))
    assert rec.current_streak == 2
    rec = streak_module.record_activity(today=date(2026, 4, 20))
    assert rec.current_streak == 3
    assert rec.longest_streak == 3


def test_same_day_is_idempotent(streak_module):
    streak_module.record_activity(today=date(2026, 4, 18))
    rec = streak_module.record_activity(today=date(2026, 4, 18))
    assert rec.current_streak == 1


def test_one_day_gap_consumes_freeze(streak_module):
    streak_module.record_activity(today=date(2026, 4, 18))
    # Skip Apr 19; come back Apr 20. We have 1 freeze by default → continue.
    rec = streak_module.record_activity(today=date(2026, 4, 20))
    assert rec.current_streak == 2
    assert rec.streak_freezes_left == 0


def test_two_day_gap_with_one_freeze_resets(streak_module):
    streak_module.record_activity(today=date(2026, 4, 18))
    # Skip Apr 19 and Apr 20; only 1 freeze available → reset.
    rec = streak_module.record_activity(today=date(2026, 4, 21))
    assert rec.current_streak == 1


def test_longest_streak_persists_after_reset(streak_module):
    for d in (18, 19, 20, 21, 22):
        streak_module.record_activity(today=date(2026, 4, d))
    rec = streak_module.record_activity(today=date(2026, 4, 30))   # gap > freezes
    assert rec.longest_streak == 5
    assert rec.current_streak == 1


def test_sunday_tops_up_freezes(streak_module):
    # Apr 19 2026 is a Sunday.
    streak_module.record_activity(today=date(2026, 4, 17))   # Friday
    rec = streak_module.record_activity(today=date(2026, 4, 19))  # Sunday
    # Recovered the freeze used by the Friday → Sunday gap (1 freeze burnt,
    # then Sunday top-up adds it back, capped at MAX_FREEZES=3).
    assert rec.streak_freezes_left >= 1


def test_streak_label(streak_module):
    assert streak_module.streak_label() == ""
    streak_module.record_activity(today=date(2026, 4, 18))
    assert streak_module.streak_label() == "🔥 1-day streak"
    streak_module.record_activity(today=date(2026, 4, 19))
    assert "2-day streak" in streak_module.streak_label()


def test_onboarding_state(streak_module):
    assert streak_module.is_onboarded() is False
    streak_module.mark_onboarding_complete()
    assert streak_module.is_onboarded() is True
