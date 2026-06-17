"""
TransitionScreen — the ETS "Test Preview Tool" section-transition pages.

A single reusable screen that renders the shared ExamChrome (charcoal header +
maroon rule + tool ribbon + pink section bar) over a black-bordered white
content box carrying a bold serif title and serif body paragraphs. The exact
title / body / ribbon / section-bar are switched per *kind* via
``configure(kind, ...)``:

  * ``"end_of_section"``    — "End of Section". Ribbon [Review, Return,
    Continue]; section bar "Section X of Y" + timer.
  * ``"section_finished"``  — "Section Finished". Ribbon [Continue]; section bar
    "Section X of Y", NO timer.
  * ``"confirm_exit_awa"``  — "Confirm Early Exit on Analytical Writing
    Section". Ribbon [Return, Continue]; section bar "Section X of Y |
    Question 1 of 1" + timer.

The page floats on ``ExamColor.PAGE_GRAY`` with a black-bordered white content
box, matching the rest of the in-test screens. Callbacks are wired through the
chrome ribbon (``on_review`` / ``on_return`` / ``on_continue``); the same
callbacks are stored so a kind can be reconfigured without re-binding.
"""
import wx

from widgets import ui_scale
from widgets.exam_chrome import ExamChrome
from widgets.theme import ExamColor


# Canonical copy per transition kind. Title is bold serif; body is a list of
# serif paragraphs. Ribbon is the ExamChrome button-id subset; ``timer`` drives
# whether the pink bar carries the countdown.
_KINDS = {
    "end_of_section": {
        "title": "End of Section",
        "body": [
            "You have reached the end of this section. You have time remaining "
            "to review. As long as there is time remaining, you can check your "
            "work. Once you leave this section, you WILL NOT be able to return "
            "to it.",
            "Select Review to go back to the Review screen.",
            "Select Return to go to the last question in this section.",
            "Select Continue to proceed to the next section of the test.",
        ],
        "buttons": ["review", "return", "continue"],
        "timer": True,
    },
    "section_finished": {
        "title": "Section Finished",
        "body": [
            "You have finished this section and now will begin the next one.",
            "Select Continue to proceed.",
        ],
        "buttons": ["continue"],
        "timer": False,
    },
    "confirm_exit_awa": {
        "title": "Confirm Early Exit on Analytical Writing Section",
        "body": [
            "You still have time remaining on this section.",
            "Select Return to continue working on your response.",
            "Select Continue to leave this section now.",
            "Once you leave this section you WILL NOT be able to return to it.",
        ],
        "buttons": ["return", "continue"],
        "timer": True,
    },
}


class TransitionScreen(wx.Panel):
    """Reusable ETS section-transition page mounted on ExamChrome.

    Public API:
        TransitionScreen(parent)
        configure(kind, section_label=..., on_review=None, on_return=None,
                  on_continue=None)

    Attributes (stable for tests / callers):
        chrome        — the ExamChrome instance (header/ribbon/section bar)
        title_label   — bold serif title StaticText
        body_panel    — host panel for the serif body paragraphs
        kind          — the currently configured kind string (or None)
    """

    # Wrap width for the serif body — DPI-scaled so prose doesn't sit in a
    # narrow column on a 4K display.
    _WRAP_BASE = 760

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(ExamColor.PAGE_GRAY)
        self.kind = None
        self._on_review = None
        self._on_return = None
        self._on_continue = None
        # A timer-bearing chrome is built once and reused; section_finished just
        # hides the timer. Building with_timer=True keeps a single chrome whose
        # countdown a caller can drive on the timed kinds.
        self._build_ui()

    # ── construction ──────────────────────────────────────────────────

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        self.chrome = ExamChrome(self, with_timer=True)
        outer.Add(self.chrome, 0, wx.EXPAND)
        self.chrome.set_on("review", self._fire_review)
        self.chrome.set_on("return", self._fire_return)
        self.chrome.set_on("continue", self._fire_continue)

        # ── Black-bordered white content box floating on the gray page ──
        page = wx.BoxSizer(wx.VERTICAL)
        self.content_border = wx.Panel(self)
        self.content_border.SetBackgroundColour(ExamColor.CONTENT_BORDER)
        border_sizer = wx.BoxSizer(wx.VERTICAL)

        self.content_box = wx.Panel(self.content_border)
        self.content_box.SetBackgroundColour(ExamColor.CONTENT_BG)
        box_sizer = wx.BoxSizer(wx.VERTICAL)
        box_sizer.AddSpacer(ui_scale.space(8))

        self.title_label = wx.StaticText(self.content_box, label="")
        self.title_label.SetForegroundColour(ExamColor.TEXT)
        self.title_label.SetFont(
            ui_scale.exam_serif(ui_scale.BASE_TITLE, wx.FONTWEIGHT_BOLD)
        )
        box_sizer.Add(self.title_label, 0,
                      wx.LEFT | wx.RIGHT | wx.BOTTOM, ui_scale.space(6))

        # Hairline under the title (matches the ETS transition pages).
        rule = wx.Panel(self.content_box, size=(-1, max(1, ui_scale.font_size(1))))
        rule.SetBackgroundColour(ExamColor.TRANSITION_RULE)
        box_sizer.Add(rule, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
                      ui_scale.space(6))

        # Body paragraphs live in their own panel so configure() can rebuild
        # just the prose without disturbing the title/rule.
        self.body_panel = wx.Panel(self.content_box)
        self.body_panel.SetBackgroundColour(ExamColor.CONTENT_BG)
        self.body_panel.SetSizer(wx.BoxSizer(wx.VERTICAL))
        box_sizer.Add(self.body_panel, 1,
                      wx.EXPAND | wx.LEFT | wx.RIGHT, ui_scale.space(6))

        self.content_box.SetSizer(box_sizer)
        border_sizer.Add(self.content_box, 1, wx.EXPAND | wx.ALL,
                         max(1, ui_scale.font_size(2)))
        self.content_border.SetSizer(border_sizer)

        page.Add(self.content_border, 1, wx.EXPAND | wx.ALL, ui_scale.space(5))
        outer.Add(page, 1, wx.EXPAND)

        self.SetSizer(outer)

    def _set_body(self, paragraphs):
        """Rebuild the serif body paragraphs from a list of strings."""
        sizer = self.body_panel.GetSizer()
        sizer.Clear(delete_windows=True)
        self._body_labels = []
        for para in paragraphs:
            lbl = wx.StaticText(self.body_panel, label=para)
            lbl.SetForegroundColour(ExamColor.TEXT)
            lbl.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
            lbl.Wrap(ui_scale.font_size(self._WRAP_BASE))
            sizer.Add(lbl, 0, wx.BOTTOM, ui_scale.space(3))
            self._body_labels.append(lbl)
        self.body_panel.Layout()

    # ── public API ────────────────────────────────────────────────────

    def configure(self, kind, section_label=None, on_review=None,
                  on_return=None, on_continue=None):
        """Configure the page for a transition ``kind``.

        ``kind`` is one of ``"end_of_section"``, ``"section_finished"``,
        ``"confirm_exit_awa"``. ``section_label`` overrides the pink section-bar
        text (e.g. ``"Section 2 of 6"``). The ``on_*`` callbacks are wired to
        the matching ribbon buttons; pass only those relevant to the kind
        (unwired buttons are harmless no-ops).
        """
        spec = _KINDS.get(kind)
        if spec is None:
            raise ValueError("unknown transition kind: {!r}".format(kind))
        self.kind = kind

        if on_review is not None:
            self._on_review = on_review
        if on_return is not None:
            self._on_return = on_return
        if on_continue is not None:
            self._on_continue = on_continue

        self.title_label.SetLabel(spec["title"])
        self._set_body(spec["body"])
        self.chrome.set_buttons(spec["buttons"])

        # Pink section bar: caller-supplied label, else a sensible default.
        if section_label is not None:
            self.chrome.set_section_label(section_label)
        elif not self.chrome.section_label.GetLabel():
            self.chrome.set_section_label("Section")

        # Timer: timed kinds keep the countdown; section_finished hides it.
        timer = self.chrome.timer
        if timer is not None:
            timer.Show(bool(spec["timer"]))

        self.content_box.Layout()
        self.Layout()

    def set_section_label(self, text):
        """Convenience passthrough to the chrome's pink section bar."""
        self.chrome.set_section_label(text)

    # ── event plumbing ────────────────────────────────────────────────

    def _fire_review(self):
        if self._on_review:
            self._on_review()

    def _fire_return(self):
        if self._on_return:
            self._on_return()

    def _fire_continue(self):
        if self._on_continue:
            self._on_continue()
