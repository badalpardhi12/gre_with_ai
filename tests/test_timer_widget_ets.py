"""
Headless tests for the ETS section timer (widgets/timer.py, spec §3.7).

Covers the new behavior added on top of the legacy countdown:
  * HH:MM:SS down-counting format (leading "00:" hour for sub-hour values).
  * set_hidden() hides the digits but the clock keeps counting.
  * At 5:00 remaining the timer force-reappears, warns (amber), and can no
    longer be re-hidden.
  * <= 1:00 remaining shows the critical color.
  * The expiry callback still fires when time runs out (legacy API intact).

We never run a real wx.Timer loop (that would need MainLoop and wall-clock
time); instead we drive the internal monotonic anchor by setting
``_started_at`` into the past and invoking ``_on_tick`` directly, which is
exactly what the real EVT_TIMER handler calls.

Whole file is skipped when wxPython isn't importable (headless CI).
"""
import time

import pytest

pytest.importorskip("wx", reason="TimerWidget requires wxPython")

import wx  # noqa: E402

from widgets.timer import TimerWidget  # noqa: E402
from widgets.theme import ExamColor  # noqa: E402


@pytest.fixture(scope="module")
def wx_app():
    """One wx.App per module — creating many leaks resources on macOS."""
    app = wx.App(False)
    yield app
    # Don't MainLoop; just let the app go out of scope.


@pytest.fixture
def frame(wx_app):
    f = wx.Frame(None)
    yield f
    f.Destroy()


def _make_timer(frame, seconds):
    """Build a TimerWidget on a hidden frame, set its duration, no real loop."""
    t = TimerWidget(frame, seconds)
    t.set_time(seconds)
    return t


def _drive_to(timer, remaining_seconds):
    """Pretend the section started long enough ago that `remaining_seconds`
    are left, then run one tick exactly as EVT_TIMER would.

    This avoids depending on a real wx event loop or wall-clock sleeps. We
    pin ``time.monotonic`` to a fixed value for the duration of the tick so
    the computed elapsed is exact (otherwise the few microseconds between
    anchoring ``_started_at`` and the read inside ``_on_tick`` truncate one
    second off via ``int()``).
    """
    timer._running = True
    timer._paused = False
    elapsed = timer.total_time - remaining_seconds
    timer._paused_total = 0.0

    import widgets.timer as timer_mod
    now = 1_000_000.0
    timer._started_at = now - elapsed
    real_monotonic = timer_mod.time.monotonic
    timer_mod.time.monotonic = lambda: now
    try:
        timer._on_tick(None)
    finally:
        timer_mod.time.monotonic = real_monotonic


# ── H:MM:SS formatting ──────────────────────────────────────────────────

def test_format_hmmss_sub_hour(frame):
    t = _make_timer(frame, 11 * 60 + 42)   # 0:11:42
    assert t.format_remaining() == "00:11:42"
    assert t.display.GetLabel() == "00:11:42"


def test_format_hmmss_over_hour(frame):
    t = _make_timer(frame, 3600 + 5 * 60 + 9)   # 1:05:09
    assert t.format_remaining() == "01:05:09"


def test_format_counts_down(frame):
    t = _make_timer(frame, 30 * 60)   # 0:30:00
    assert t.display.GetLabel() == "00:30:00"
    _drive_to(t, 18 * 60 + 7)
    assert t.get_remaining() == 18 * 60 + 7
    assert t.display.GetLabel() == "00:18:07"


# ── Hide / Show keeps counting ──────────────────────────────────────────

def test_set_hidden_hides_digits_but_keeps_counting(frame):
    t = _make_timer(frame, 20 * 60)
    assert t.is_hidden() is False

    t.set_hidden(True)
    assert t.is_hidden() is True
    # Digits are masked, not the real time.
    assert t.display.GetLabel() == TimerWidget.HIDDEN_PLACEHOLDER
    assert t.toggle_btn.GetLabel() == TimerWidget.SHOW_LABEL

    # Clock still advances while hidden.
    _drive_to(t, 12 * 60 + 30)
    assert t.get_remaining() == 12 * 60 + 30          # real time advanced
    assert t.display.GetLabel() == TimerWidget.HIDDEN_PLACEHOLDER  # still masked

    # Show again reveals the (now smaller) real time.
    t.set_hidden(False)
    assert t.is_hidden() is False
    assert t.display.GetLabel() == "00:12:30"


def test_toggle_hidden(frame):
    t = _make_timer(frame, 20 * 60)
    assert t.toggle_hidden() is True
    assert t.toggle_hidden() is False


# ── 5:00 force-reappear + warn + lock ───────────────────────────────────

def test_five_minute_forces_reappear_and_warn_color(frame):
    t = _make_timer(frame, 20 * 60)
    t.set_hidden(True)
    assert t.is_hidden() is True

    # Cross the 5:00 threshold (drive to exactly 5:00 remaining).
    _drive_to(t, 5 * 60)

    # Forced visible despite having been hidden.
    assert t.is_hidden() is False
    assert t.display.GetLabel() == "00:05:00"
    # Amber warning color, legible on navy.
    assert t.display.GetForegroundColour() == ExamColor.TIMER_WARN


def test_cannot_rehide_after_five_minutes(frame):
    t = _make_timer(frame, 20 * 60)
    assert t.can_hide() is True

    _drive_to(t, 4 * 60 + 30)   # past 5:00
    assert t.can_hide() is False

    # set_hidden(True) is refused; stays visible.
    result = t.set_hidden(True)
    assert result is False
    assert t.is_hidden() is False
    # toggle_hidden also refuses.
    assert t.toggle_hidden() is False
    # Affordance is disabled so the user can't even try.
    assert t.toggle_btn.IsEnabled() is False


def test_can_hide_above_five_minutes(frame):
    t = _make_timer(frame, 20 * 60)
    _drive_to(t, 6 * 60)        # still above threshold
    assert t.can_hide() is True
    assert t.set_hidden(True) is True
    assert t.is_hidden() is True


# ── <= 1:00 critical color ──────────────────────────────────────────────

def test_one_minute_critical_color(frame):
    t = _make_timer(frame, 20 * 60)
    _drive_to(t, 60)
    assert t.display.GetLabel() == "00:01:00"
    assert t.display.GetForegroundColour() == ExamColor.TIMER_CRITICAL

    _drive_to(t, 12)
    assert t.display.GetLabel() == "00:00:12"
    assert t.display.GetForegroundColour() == ExamColor.TIMER_CRITICAL


def test_normal_color_above_warning(frame):
    t = _make_timer(frame, 20 * 60)
    _drive_to(t, 15 * 60)
    assert t.display.GetForegroundColour() == ExamColor.TIMER_NORMAL


# ── Expiry callback still fires (legacy API intact) ─────────────────────

def test_expiry_callback_fires(frame):
    t = _make_timer(frame, 20 * 60)
    fired = {"n": 0}
    t.set_on_expire(lambda: fired.__setitem__("n", fired["n"] + 1))

    _drive_to(t, 0)
    assert t.get_remaining() == 0
    assert fired["n"] == 1
    # And the timer stopped running on expiry.
    assert t._running is False


def test_warning_callback_still_fires(frame):
    t = _make_timer(frame, 20 * 60)
    seen = []
    t.set_on_warning(lambda rem: seen.append(rem))

    # One tick that crosses both 5:00 and stays above 1:00 -> 5:00 warning.
    _drive_to(t, 4 * 60 + 59)
    assert 4 * 60 + 59 in seen


def test_tick_callback_reports_delta(frame):
    t = _make_timer(frame, 20 * 60)
    deltas = []
    t.set_on_tick(lambda d: deltas.append(d))

    _drive_to(t, 20 * 60 - 3)   # 3 seconds elapsed since start
    assert deltas == [3]
