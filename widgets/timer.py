"""
Countdown timer widget — the ETS GRE section timer (spec §3.7).

Displays the remaining section time as ``H:MM:SS`` counting DOWN (e.g.
``0:11:42``) on the navy footer, with a "Hide Time" / "Show Time" toggle and
warning colors that stay legible on navy.

ETS behaviors implemented (spec §3.7, all [C] except where noted):
  * Format ``H:MM:SS`` counting down; sub-hour values keep a leading "0:".
  * "Hide Time" toggle can hide the digits but **cannot stop** the clock — the
    countdown keeps running while hidden.
  * At 5:00 remaining the timer auto-REAPPEARS (force-show) if hidden and turns
    amber; once that threshold is crossed it can no longer be re-hidden.
  * Warning colors: ``<= 5:00`` amber, ``<= 1:00`` critical (red), normal white.

Time is anchored to a ``time.monotonic()`` reading at start so a long UI stall
(slow WebView render, modal dialog) doesn't drift the displayed countdown — we
read the actual elapsed wall-clock each tick instead of blindly subtracting 1.
"""
import time

import wx

from config import TIMER_WARNING_SECONDS
from widgets.theme import ExamColor
from widgets import ui_scale


# Threshold (seconds) at and below which the timer force-reappears, warns, and
# can no longer be re-hidden. The official UI does this at 5:00 remaining.
REAPPEAR_THRESHOLD = TIMER_WARNING_SECONDS   # 300 (5:00)
CRITICAL_THRESHOLD = 60                       # 1:00


def _c_warn_on_pink():
    """Amber is illegible on the pink section bar; use a darker amber there."""
    return wx.Colour(0xb8, 0x6a, 0x00)


class TimerWidget(wx.Panel):
    """
    Section countdown timer. Shows ``H:MM:SS`` and changes color at warning
    thresholds. Includes a Hide/Show toggle that never stops the clock.

    Public API (stable — callers in screens/ and main_frame depend on these):
        TimerWidget(parent, time_seconds=0)
        set_time(seconds), start(), pause(), resume(), stop()
        set_on_expire(cb), set_on_warning(cb), set_on_tick(cb)
        get_elapsed()
        attributes: remaining, total_time, display, label, timer

    Added for the ETS section timer (spec §3.7):
        set_hidden(bool), toggle_hidden(), is_hidden() -> bool
        can_hide() -> bool        # False once 5:00 reached
        get_remaining() -> int
        format_remaining() -> str # current "H:MM:SS" text
        attribute: toggle_btn     # the Hide/Show button
    """

    HIDE_LABEL = "Hide Time"
    SHOW_LABEL = "Show Time"
    HIDDEN_PLACEHOLDER = "–:––:––"  # en-dash placeholder

    def __init__(self, parent, time_seconds=0):
        super().__init__(parent)
        self.total_time = time_seconds
        self.remaining = time_seconds
        self._paused = False
        self._running = False
        self._started_at = None       # monotonic seconds when start() was called
        self._paused_total = 0.0      # accumulated pause duration
        self._pause_start = None      # monotonic seconds when pause() was called
        self._last_tick_remaining = time_seconds

        # Hide/Show state. Once the reappear threshold is crossed the timer is
        # forced visible and `_hide_locked` prevents re-hiding (spec §3.7).
        self._hidden = False
        self._hide_locked = False

        # Navy footer chrome so the white digits read correctly when this panel
        # is dropped into the footer "sandwich".
        self.SetBackgroundColour(ExamColor.HEADER_NAVY)

        # UI
        self.label = wx.StaticText(self, label="Time Remaining")
        self.label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_TIMER_PT - 4))
        self.label.SetForegroundColour(ExamColor.TEXT_ON_NAVY)

        self.display = wx.StaticText(self, label=self._format_time())
        self.display.SetFont(ui_scale.exam_sans(
            ui_scale.EXAM_TIMER_PT, weight=wx.FONTWEIGHT_BOLD))
        self.display.SetForegroundColour(ExamColor.TIMER_NORMAL)

        self.toggle_btn = wx.Button(self, label=self.HIDE_LABEL,
                                    style=wx.BU_EXACTFIT)
        self.toggle_btn.SetFont(ui_scale.exam_sans(ui_scale.EXAM_TIMER_PT - 2))
        self.toggle_btn.Bind(wx.EVT_BUTTON, self._on_toggle_clicked)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.label, 0, wx.ALIGN_CENTER | wx.BOTTOM, 2)
        sizer.Add(self.display, 0, wx.ALIGN_CENTER)
        sizer.Add(self.toggle_btn, 0, wx.ALIGN_CENTER | wx.TOP, 2)
        self.SetSizer(sizer)
        self._compact = False

        # Timer (1 second interval — display refresh, NOT the source of truth)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_tick, self.timer)

        # Callbacks
        self._on_expire = None
        self._on_warning = None
        self._on_tick_cb = None

    # ── Lifecycle / configuration (existing public API) ────────────────

    def set_time(self, seconds):
        """Reset the timer to a new duration. Also resets hide/show state."""
        self.total_time = seconds
        self.remaining = seconds
        self._started_at = None
        self._paused_total = 0.0
        self._pause_start = None
        self._last_tick_remaining = seconds
        # A fresh section starts visible and re-hideable.
        self._hidden = False
        self._hide_locked = False
        self._sync_toggle_button()
        self._update_display()

    def start(self):
        """Start the countdown."""
        self._running = True
        self._paused = False
        self._started_at = time.monotonic()
        self._paused_total = 0.0
        self._pause_start = None
        self._last_tick_remaining = self.total_time
        self.timer.Start(1000)
        # If the section starts already inside the warning window, enforce the
        # reappear/lock rules immediately.
        self._enforce_reappear()
        self._update_display()

    def pause(self):
        """Pause the countdown."""
        if not self._paused:
            self._paused = True
            self._pause_start = time.monotonic()
        self.timer.Stop()

    def resume(self):
        """Resume the countdown."""
        if self._running and self._paused:
            if self._pause_start is not None:
                self._paused_total += time.monotonic() - self._pause_start
                self._pause_start = None
            self._paused = False
            self.timer.Start(1000)

    def stop(self):
        """Stop the countdown entirely."""
        self._running = False
        self._paused = False
        self.timer.Stop()

    def set_on_expire(self, callback):
        """Set callback for when time runs out. callback()"""
        self._on_expire = callback

    def set_on_warning(self, callback):
        """Set callback for warning threshold. callback(remaining_seconds)"""
        self._on_warning = callback

    def set_on_tick(self, callback):
        """Set callback invoked every tick. callback(elapsed_seconds_since_last_tick)"""
        self._on_tick_cb = callback

    # ── Hide / Show (spec §3.7) ────────────────────────────────────────

    def is_hidden(self):
        """True when the digits are hidden (the clock still runs)."""
        return self._hidden

    def can_hide(self):
        """False once the 5:00 reappear threshold has been crossed.

        After that point the official UI does not allow re-hiding.
        """
        return not self._hide_locked

    def set_hidden(self, hidden):
        """Show/hide the clock digits without ever stopping the countdown.

        Hiding is ignored once the reappear threshold has locked the timer
        visible (spec §3.7: "reportedly cannot be re-hidden after that").
        Returns the resulting hidden state.
        """
        hidden = bool(hidden)
        if hidden and self._hide_locked:
            # Re-hiding is no longer allowed; stay visible.
            hidden = False
        self._hidden = hidden
        self._sync_toggle_button()
        self._update_display()
        return self._hidden

    def toggle_hidden(self):
        """Flip the hidden state (respecting the re-hide lock). Returns new state."""
        return self.set_hidden(not self._hidden)

    def _on_toggle_clicked(self, event):
        self.toggle_hidden()

    def _sync_toggle_button(self):
        """Keep the toggle button's label/enabled state in sync."""
        base = self.SHOW_LABEL if self._hidden else self.HIDE_LABEL
        self.toggle_btn.SetLabel(("⊖ " + base) if self._compact else base)
        # Once locked visible, the toggle can't hide anymore — disable it.
        self.toggle_btn.Enable(not self._hide_locked)
        # In compact mode re-fit so the swapped label ("⊖ Show Time" is a hair
        # wider than "⊖ Hide Time") is never clipped at the bar's right edge.
        if self._compact:
            self.toggle_btn.SetMinSize(self.toggle_btn.GetBestSize())
            sizer = self.GetSizer()
            if sizer is not None:
                sizer.Layout()

    def _enforce_reappear(self):
        """Force the clock visible + lock re-hiding once <= 5:00 remaining.

        Idempotent: safe to call every tick. Returns True if this call newly
        locked the timer (i.e. the threshold was just crossed).
        """
        if self._hide_locked or self.remaining > REAPPEAR_THRESHOLD:
            return False
        self._hide_locked = True
        self._hidden = False          # force-reappear
        self._sync_toggle_button()
        return True

    # ── Tick / rendering ───────────────────────────────────────────────

    def _on_tick(self, event):
        if self._paused or not self._running or self._started_at is None:
            return

        elapsed = time.monotonic() - self._started_at - self._paused_total
        new_remaining = max(0, int(self.total_time - elapsed))
        # Tick callback receives the *actual* delta (so per-question time
        # stays accurate even if a tick was missed during a UI stall).
        delta = max(0, self._last_tick_remaining - new_remaining)
        self.remaining = new_remaining
        self._last_tick_remaining = new_remaining

        # Force-reappear + lock at the 5:00 threshold before we repaint.
        self._enforce_reappear()
        self._update_display()

        if self._on_tick_cb and delta > 0:
            self._on_tick_cb(delta)

        # Warning thresholds: fire once per crossing rather than only on the
        # exact-second match (so a missed tick doesn't skip the warning).
        if self._on_warning:
            for threshold in (REAPPEAR_THRESHOLD, CRITICAL_THRESHOLD):
                if (self.remaining <= threshold <
                        self.remaining + delta):
                    self._on_warning(self.remaining)

        if self.remaining <= 0:
            self.stop()
            if self._on_expire:
                self._on_expire()

    def set_compact_bar_style(self):
        """Restyle for the pink ETS section bar: a single inline row
        ``HH:MM:SS  ⊖ Hide Time`` with DARK text (no "Time Remaining"
        label, no stacked navy layout). Idempotent."""
        self._compact = True
        self.SetBackgroundColour(ExamColor.SECTION_BAR_PINK)
        self.label.Hide()
        self.display.SetForegroundColour(ExamColor.SECTION_BAR_TEXT)
        self.display.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT,
                                                weight=wx.FONTWEIGHT_BOLD))
        # The Hide/Show toggle is a native wx.Button; on macOS it defaults to a
        # system foreground that reads as washed-out light text on the pink bar.
        # Pin it to the dark section-bar ink and the pink face so the label is
        # legible, then re-fit its best size so the "⊖ Hide Time" label (with
        # the wide ⊖ glyph) is never truncated.
        self.toggle_btn.SetBackgroundColour(ExamColor.SECTION_BAR_PINK)
        self.toggle_btn.SetForegroundColour(ExamColor.SECTION_BAR_TEXT)
        self.toggle_btn.SetLabel("⊖ " + self.HIDE_LABEL)
        self.toggle_btn.SetFont(ui_scale.exam_sans(ui_scale.EXAM_TIMER_PT - 2))
        self.toggle_btn.SetMinSize(self.toggle_btn.GetBestSize())
        # Rebuild the sizer as a horizontal inline row. Detach the widgets from
        # the old vertical sizer first (wx forbids adding a window that's still
        # held by another sizer).
        old = self.GetSizer()
        if old is not None:
            old.Clear(delete_windows=False)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.display, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, ui_scale.space(2))
        row.Add(self.toggle_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        self.SetSizer(row, deleteOld=True)
        self._update_display()
        self.Layout()

    def _update_display(self):
        if self._hidden:
            self.display.SetLabel(self.HIDDEN_PLACEHOLDER)
        else:
            self.display.SetLabel(self._format_time())

        # Color coding — legible on the navy footer (spec §1/§3.7), or dark
        # on the pink section bar in compact mode.
        if self.remaining <= CRITICAL_THRESHOLD:
            self.display.SetForegroundColour(ExamColor.TIMER_CRITICAL)
        elif self.remaining <= REAPPEAR_THRESHOLD:
            self.display.SetForegroundColour(
                _c_warn_on_pink() if self._compact else ExamColor.TIMER_WARN)
        else:
            self.display.SetForegroundColour(
                ExamColor.SECTION_BAR_TEXT if self._compact else ExamColor.TIMER_NORMAL)

        self.display.Refresh()

    def _format_time(self):
        """Render the remaining time as ``HH:MM:SS`` (e.g. ``00:19:46``),
        matching the ETS section-bar clock."""
        total = max(0, self.remaining)
        hours, rem = divmod(total, 3600)
        mins, secs = divmod(rem, 60)
        return f"{hours:02d}:{mins:02d}:{secs:02d}"

    # ── Accessors ──────────────────────────────────────────────────────

    def get_elapsed(self):
        return self.total_time - self.remaining

    def get_remaining(self):
        """Remaining seconds (the clock keeps counting even while hidden)."""
        return self.remaining

    def format_remaining(self):
        """The current ``H:MM:SS`` string (regardless of hidden state)."""
        return self._format_time()
