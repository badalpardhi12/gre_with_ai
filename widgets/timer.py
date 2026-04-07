"""
Countdown timer widget — displays remaining time with visual warnings.
"""
import wx

from config import TIMER_WARNING_SECONDS


class TimerWidget(wx.Panel):
    """
    Section countdown timer. Shows MM:SS.
    Changes colour at warning thresholds.
    """

    def __init__(self, parent, time_seconds=0):
        super().__init__(parent)
        self.total_time = time_seconds
        self.remaining = time_seconds
        self._paused = False
        self._running = False

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

        # Timer (1 second interval)
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
        self._update_display()

    def start(self):
        """Start the countdown."""
        self._running = True
        self._paused = False
        self.timer.Start(1000)

    def pause(self):
        """Pause the countdown."""
        self._paused = True
        self.timer.Stop()

    def resume(self):
        """Resume the countdown."""
        if self._running:
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
        """Set callback invoked every tick. callback(elapsed_seconds=1)"""
        self._on_tick_cb = callback

    def _on_tick(self, event):
        if self._paused:
            return
        self.remaining = max(0, self.remaining - 1)
        self._update_display()

        # External tick callback (for per-question timing)
        if self._on_tick_cb:
            self._on_tick_cb(1)

        # Warning thresholds
        if self._on_warning and self.remaining in (300, 60):
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
