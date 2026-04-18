"""
Countdown timer widget — displays remaining time with visual warnings.
"""
import time

import wx

from config import TIMER_WARNING_SECONDS


class TimerWidget(wx.Panel):
    """
    Section countdown timer. Shows MM:SS.
    Changes colour at warning thresholds.

    Time is anchored to a `time.monotonic()` reading at start so a long UI
    stall (slow WebView render, modal dialog) doesn't drift the displayed
    countdown — we read the actual elapsed wall-clock each tick instead of
    blindly subtracting 1.
    """

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

        # UI
        self.label = wx.StaticText(self, label="Time Remaining")
        self.label.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                    wx.FONTWEIGHT_NORMAL))

        self.display = wx.StaticText(self, label=self._format_time())
        self.display.SetFont(wx.Font(20, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL,
                                      wx.FONTWEIGHT_BOLD))

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.label, 0, wx.ALIGN_CENTER | wx.BOTTOM, 2)
        sizer.Add(self.display, 0, wx.ALIGN_CENTER)
        self.SetSizer(sizer)

        # Timer (1 second interval — display refresh, NOT the source of truth)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_tick, self.timer)

        # Callbacks
        self._on_expire = None
        self._on_warning = None
        self._on_tick_cb = None

    def set_time(self, seconds):
        """Reset the timer to a new duration."""
        self.total_time = seconds
        self.remaining = seconds
        self._started_at = None
        self._paused_total = 0.0
        self._pause_start = None
        self._last_tick_remaining = seconds
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
        self._update_display()

        if self._on_tick_cb and delta > 0:
            self._on_tick_cb(delta)

        # Warning thresholds: fire once per crossing rather than only on the
        # exact-second match (so a missed tick doesn't skip the warning).
        if self._on_warning:
            for threshold in (300, 60):
                if (self.remaining <= threshold <
                        self.remaining + delta):
                    self._on_warning(self.remaining)

        if self.remaining <= 0:
            self.stop()
            if self._on_expire:
                self._on_expire()

    def _update_display(self):
        self.display.SetLabel(self._format_time())

        # Color coding
        if self.remaining <= 60:
            self.display.SetForegroundColour(wx.Colour(220, 30, 30))   # red
        elif self.remaining <= 300:
            self.display.SetForegroundColour(wx.Colour(220, 150, 0))   # orange
        else:
            self.display.SetForegroundColour(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))  # adapt to theme

        self.display.Refresh()

    def _format_time(self):
        mins, secs = divmod(max(0, self.remaining), 60)
        return f"{mins:02d}:{secs:02d}"

    def get_elapsed(self):
        return self.total_time - self.remaining
