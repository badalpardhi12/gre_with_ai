"""
ExamChrome — the shared ETS "Test Preview Tool" top chrome.

Every in-test screen (question, AWA, instructions, review, transition pages)
mounts this so the header is pixel-consistent with the real GRE:

  ┌───────────────────────────────────────────────────────────────────────┐
  │ *gre  Test Preview Tool          [Exit][Calc][Mark][Review][Help][Back][Next] │  charcoal + maroon rule
  ├───────────────────────────────────────────────────────────────────────┤
  │ Section X of Y | Question N of M                 00:19:46  ⊖ Hide Time  │  pink bar
  └───────────────────────────────────────────────────────────────────────┘

The ribbon is configured per screen via ``set_buttons([...])`` — each spec is a
dict ``{"id","label","icon","kind","enabled"}``; clicking fires the registered
callback (``set_on(id, cb)``). The timer is created here and exposed as
``self.timer`` so screens can drive it. ``set_section_label(text)`` /
``hide_section_line()`` control the pink bar (transition pages show only
"Section X of Y" with no question counter; the AWA-statement and section-intro
pages keep the timer, the Section-Finished page hides it).
"""
import wx

from widgets import ui_scale
from widgets.theme import ExamColor
from widgets.exam_tool_button import ExamToolButton
from widgets.timer import TimerWidget


# Canonical ribbon button order + presentation (the screen picks a subset).
BUTTONS = {
    "exit":     {"label": "Exit Section", "icon": "exit",   "kind": "plum"},
    "calc":     {"label": "Calc",         "icon": "calc",   "kind": "gray"},
    "mark":     {"label": "Mark",         "icon": "mark",   "kind": "gray"},
    "review":   {"label": "Review",       "icon": "review", "kind": "gray"},
    "help":     {"label": "Help",         "icon": "help",   "kind": "gray"},
    "back":     {"label": "Back",         "icon": "back",   "kind": "blue"},
    "next":     {"label": "Next",         "icon": "next",   "kind": "blue"},
    "return":   {"label": "Return",       "icon": "back",   "kind": "blue"},
    "continue": {"label": "Continue",     "icon": "next",   "kind": "blue"},
}


class ExamChrome(wx.Panel):
    """Charcoal header + maroon rule + tool ribbon + pink section bar."""

    def __init__(self, parent, with_timer=True):
        super().__init__(parent)
        self.SetBackgroundColour(ExamColor.HEADER_CHARCOAL)
        self._callbacks = {}
        self._btns = {}
        self._with_timer = with_timer
        self.timer = None
        self._build()

    def _build(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # ── Charcoal header row ───────────────────────────────────────
        self.header = wx.Panel(self)
        self.header.SetBackgroundColour(ExamColor.HEADER_CHARCOAL)
        hs = wx.BoxSizer(wx.HORIZONTAL)

        logo = wx.StaticText(self.header, label="✳gre")  # ✳gre
        logo.SetForegroundColour(ExamColor.TEXT_ON_NAVY)
        logo.SetFont(ui_scale.exam_sans(ui_scale.EXAM_STEM_PT + 2, wx.FONTWEIGHT_BOLD))
        hs.Add(logo, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, ui_scale.space(3))
        title = wx.StaticText(self.header, label="Test Preview Tool")
        title.SetForegroundColour(ExamColor.TEXT_ON_NAVY)
        title.SetFont(ui_scale.exam_sans(ui_scale.EXAM_COUNTER_PT, wx.FONTWEIGHT_BOLD))
        hs.Add(title, 0, wx.ALIGN_CENTER_VERTICAL)

        hs.AddStretchSpacer()

        # Ribbon host (right-aligned); populated by set_buttons().
        self.ribbon = wx.Panel(self.header)
        self.ribbon.SetBackgroundColour(ExamColor.HEADER_CHARCOAL)
        self.ribbon_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.ribbon.SetSizer(self.ribbon_sizer)
        hs.Add(self.ribbon, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, ui_scale.space(2))
        self.header.SetSizer(hs)
        outer.Add(self.header, 0, wx.EXPAND)

        # ── Maroon hairline ───────────────────────────────────────────
        rule = wx.Panel(self, size=(-1, max(2, ui_scale.font_size(3))))
        rule.SetBackgroundColour(ExamColor.HEADER_RULE_MAROON)
        outer.Add(rule, 0, wx.EXPAND)

        # ── Pink section bar ──────────────────────────────────────────
        self.section_bar = wx.Panel(self)
        self.section_bar.SetBackgroundColour(ExamColor.SECTION_BAR_PINK)
        ss = wx.BoxSizer(wx.HORIZONTAL)
        self.section_label = wx.StaticText(self.section_bar, label="")
        self.section_label.SetForegroundColour(ExamColor.SECTION_BAR_TEXT)
        self.section_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT,
                                                      wx.FONTWEIGHT_BOLD))
        ss.Add(self.section_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
               ui_scale.space(2))
        ss.AddStretchSpacer()
        if self._with_timer:
            self.timer = TimerWidget(self.section_bar)
            self.timer.set_compact_bar_style()  # restyle for the pink bar
            ss.Add(self.timer, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, ui_scale.space(5))
        self.section_bar.SetSizer(ss)
        outer.Add(self.section_bar, 0, wx.EXPAND)

        self.SetSizer(outer)

    # ── Public API ────────────────────────────────────────────────────
    def set_buttons(self, ids):
        """Show exactly these ribbon button ids (in given order). Reuses
        existing button widgets so callbacks/timers survive a reconfigure."""
        self.ribbon_sizer.Clear(delete_windows=True)
        self._btns = {}
        for bid in ids:
            spec = BUTTONS.get(bid)
            if spec is None:
                continue
            btn = ExamToolButton(self.ribbon, spec["label"], spec["icon"],
                                 kind=spec["kind"])
            btn.Bind(wx.EVT_BUTTON, lambda _e, i=bid: self._fire(i))
            self.ribbon_sizer.Add(btn, 0, wx.ALL, ui_scale.space(0))
            self._btns[bid] = btn
        self.ribbon.Layout()
        self.header.Layout()
        self.Layout()

    def set_on(self, button_id, callback):
        self._callbacks[button_id] = callback

    def _fire(self, button_id):
        cb = self._callbacks.get(button_id)
        if cb:
            cb()

    def enable_button(self, button_id, enabled):
        b = self._btns.get(button_id)
        if b:
            b.Enable(enabled)

    def set_button_label(self, button_id, label, icon=None):
        b = self._btns.get(button_id)
        if b:
            b.set_label(label, icon)
            self.ribbon.Layout(); self.header.Layout()

    def has_button(self, button_id):
        return button_id in self._btns

    def set_section_label(self, text):
        self.section_label.SetLabel(text)
        self.section_bar.Layout()

    def show_section_bar(self, show):
        self.section_bar.Show(show)
        self.Layout()
