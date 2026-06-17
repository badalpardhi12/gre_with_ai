"""
Instructions screen — the ETS "Test Preview Tool" section-intro page shown
before each section begins.

Mounts the shared ExamChrome (charcoal header + maroon rule + tool ribbon +
pink section bar carrying the timer) over a black-bordered white content box.
The content box shows a bold serif heading (e.g. "Quantitative Reasoning") with
a count/time subheading (e.g. "11 Questions   21 Minutes") and the standard
section directions in serif body text (ovals = one answer, squares = one or
more, figures-not-to-scale, etc.).

Ribbon: ``[help, continue]`` — the simplest faithful version of the ETS
section-intro page where the whole tool ribbon is shown but only Continue is
active. ``Continue`` fires ``set_on_begin``; the chrome has no Cancel, so a
``cancel_btn`` is provided in the content area for "Back to Dashboard".

Public API consumed by main_frame.py is preserved verbatim:
``set_section(section_type, section_state=None)``, ``set_on_begin(callback)``,
``set_on_cancel(callback)`` and the ``display_label`` override path for mixed
Quick Drills. ``title_label`` / ``body_text`` / ``begin_btn`` / ``cancel_btn``
attributes are preserved for tests/callers.
"""
import wx

from models.exam_session import SectionType, SECTION_META
from widgets import ui_scale
from widgets.exam_button import ExamButton
from widgets.exam_chrome import ExamChrome
from widgets.theme import ExamColor


# Quant section directions carry this caveat on the real test (spec §3.4 /
# §9). Appended in `set_section` for any Quant section so the SECTION_INSTRUCTIONS
# body text stays the canonical source for everything else.
FIGURES_CAVEAT = "Figures are not necessarily drawn to scale."


SECTION_INSTRUCTIONS = {
    SectionType.AWA: {
        "title": "Analytical Writing — Analyze an Issue",
        "body": (
            "You will be presented with an Issue topic. You have 30 minutes to plan and "
            "compose a response in which you discuss the extent to which you agree or disagree "
            "with the statement and explain your reasoning.\n\n"
            "• Support your position with relevant reasons and/or examples.\n"
            "• Use standard written English.\n"
            "• There is no minimum or maximum word count, but aim for at least 300 words.\n"
            "• Your essay will be scored on a 0–6 scale by an AI grader."
        ),
    },
    SectionType.VERBAL_S1: {
        "title": "Verbal Reasoning — Section 1",
        "body": (
            "This section contains 12 questions. You have 18 minutes.\n\n"
            "Question types:\n"
            "• Reading Comprehension (single answer, multiple answers, select-in-passage)\n"
            "• Text Completion (1–3 blanks)\n"
            "• Sentence Equivalence (select exactly 2 answers)\n\n"
            "You may navigate freely within this section.\n"
            "You may mark questions for review.\n"
            "You cannot return to this section after moving on."
        ),
    },
    SectionType.VERBAL_S2: {
        "title": "Verbal Reasoning — Section 2",
        "body": (
            "This section contains 15 questions. You have 23 minutes.\n\n"
            "Question types are the same as Section 1.\n"
            "Difficulty is adapted based on your Section 1 performance.\n\n"
            "You may navigate freely within this section.\n"
            "You cannot return to previous sections."
        ),
    },
    SectionType.QUANT_S1: {
        "title": "Quantitative Reasoning — Section 1",
        "body": (
            "This section contains 12 questions. You have 21 minutes.\n\n"
            "Question types:\n"
            "• Quantitative Comparison (A/B/C/D)\n"
            "• Multiple Choice (single answer, multiple answers)\n"
            "• Numeric Entry\n"
            "• Data Interpretation\n\n"
            "An on-screen calculator is available.\n"
            "You may navigate freely within this section.\n"
            "You cannot return to this section after moving on."
        ),
    },
    SectionType.QUANT_S2: {
        "title": "Quantitative Reasoning — Section 2",
        "body": (
            "This section contains 15 questions. You have 26 minutes.\n\n"
            "Question types are the same as Section 1.\n"
            "Difficulty is adapted based on your Section 1 performance.\n\n"
            "An on-screen calculator is available.\n"
            "You may navigate freely within this section.\n"
            "You cannot return to previous sections."
        ),
    },
}


def _is_quant_section(section_type):
    """True when this section's measure is Quant (needs the figures caveat)."""
    meta = SECTION_META.get(section_type)
    return bool(meta) and meta[0] == "quant"


class InstructionsScreen(wx.Panel):
    """ETS Test-Preview section-intro page mounted on ExamChrome."""

    # Wrap width for the serif body — scales with DPI so the prose doesn't
    # sit in a narrow column on a 4K display.
    _WRAP_BASE = 760

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(ExamColor.PAGE_GRAY)
        self._on_begin = None
        self._on_cancel = None
        self._build_ui()

    # ── construction ──────────────────────────────────────────────────

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # ── Shared chrome: ribbon = [Help, Continue] ────────────────────
        self.chrome = ExamChrome(self, with_timer=True)
        self.chrome.set_buttons(["help", "continue"])
        self.chrome.set_section_label("Section")
        self.chrome.set_on("continue", self._on_begin_click)
        self.chrome.set_on("help", self._on_help)
        # Continue is the live action on the section-intro page.
        self.begin_btn = self.chrome._btns.get("continue")
        outer.Add(self.chrome, 0, wx.EXPAND)

        # ── Black-bordered white content box on the gray page ───────────
        self.content_border = wx.Panel(self)
        self.content_border.SetBackgroundColour(ExamColor.CONTENT_BORDER)
        border_sizer = wx.BoxSizer(wx.VERTICAL)

        self.content_box = wx.Panel(self.content_border)
        self.content_box.SetBackgroundColour(ExamColor.CONTENT_BG)
        box_sizer = wx.BoxSizer(wx.VERTICAL)
        box_sizer.AddSpacer(ui_scale.space(6))

        # Bold serif title.
        self.title_label = wx.StaticText(self.content_box, label="Section Instructions")
        self.title_label.SetForegroundColour(ExamColor.TEXT)
        self.title_label.SetFont(ui_scale.exam_serif(ui_scale.BASE_TITLE,
                                                     wx.FONTWEIGHT_BOLD))
        box_sizer.Add(self.title_label, 0,
                      wx.LEFT | wx.RIGHT | wx.BOTTOM, ui_scale.space(5))

        # Count / time subheading (serif, slightly smaller, semibold).
        self.subtitle_label = wx.StaticText(self.content_box, label="")
        self.subtitle_label.SetForegroundColour(ExamColor.TEXT_MUTED)
        self.subtitle_label.SetFont(ui_scale.exam_serif(ui_scale.EXAM_STEM_PT,
                                                        wx.FONTWEIGHT_BOLD))
        box_sizer.Add(self.subtitle_label, 0,
                      wx.LEFT | wx.RIGHT | wx.BOTTOM, ui_scale.space(5))

        # Standard directions body (serif).
        self.body_text = wx.StaticText(self.content_box, label="")
        self.body_text.SetForegroundColour(ExamColor.TEXT)
        self.body_text.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
        self.body_text.Wrap(ui_scale.font_size(self._WRAP_BASE))
        box_sizer.Add(self.body_text, 0,
                      wx.LEFT | wx.RIGHT, ui_scale.space(5))

        box_sizer.AddStretchSpacer()

        # ── Bottom button row: Back to Dashboard (left) ────────────────
        # The chrome ribbon carries Continue; the content area carries the
        # "Back to Dashboard" affordance (no Cancel exists in the ETS ribbon).
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.cancel_btn = ExamButton(self.content_box, "Back to Dashboard",
                                     kind="grey", icon="◀",
                                     min_width=ui_scale.font_size(190))
        self.cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel_click)
        btn_row.Add(self.cancel_btn, 0)
        btn_row.AddStretchSpacer()
        box_sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, ui_scale.space(5))

        self.content_box.SetSizer(box_sizer)
        border_sizer.Add(self.content_box, 1, wx.EXPAND | wx.ALL,
                         max(1, ui_scale.font_size(2)))
        self.content_border.SetSizer(border_sizer)
        outer.Add(self.content_border, 1, wx.EXPAND | wx.ALL, ui_scale.space(4))

        self.SetSizer(outer)

    def _on_help(self):
        wx.MessageBox(
            "Read the directions, then select Continue to begin the section.",
            "Help", wx.OK | wx.ICON_INFORMATION)

    # ── public API (preserved for main_frame.py) ──────────────────────

    def set_section(self, section_type, section_state=None):
        """Configure for a specific section.

        When ``section_state`` carries a ``display_label`` (mixed Quick Drill),
        the screen overrides the canonical title and body so the user doesn't
        see "Verbal Reasoning — Section 1" before a drill that mixes both
        measures.

        For Quant sections the "Figures are not necessarily drawn to scale."
        caveat is appended to the body.
        """
        info = SECTION_INSTRUCTIONS.get(section_type, {})
        title = info.get("title", "Section")
        body = info.get("body", "")
        override = section_state and getattr(section_state, "display_label", None)
        if override:
            title = override
            n = len(getattr(section_state, "question_ids", []) or []) or 10
            mins = max(1, getattr(section_state, "time_limit", 0) // 60)
            body = (
                "This drill contains {n} questions targeting your weak areas. "
                "You have ~{mins} minutes.\n\n"
                "Questions are mixed across Verbal Reasoning and Quantitative "
                "Reasoning. Each question's measure (Verbal / Quant) is shown "
                "above it; the on-screen calculator appears only on quant "
                "questions.\n\n"
                "You may navigate freely within this drill, mark questions for "
                "review, and end the drill at any time."
            ).format(n=n, mins=mins)
        elif _is_quant_section(section_type) and FIGURES_CAVEAT not in body:
            body = body + "\n\n" + FIGURES_CAVEAT

        self.title_label.SetLabel(title)
        self.subtitle_label.SetLabel(self._subtitle_for(section_type, section_state))
        self.body_text.SetLabel(body)
        self.body_text.Wrap(ui_scale.font_size(self._WRAP_BASE))

        # Pink section bar reflects which measure is about to start.
        self.chrome.set_section_label(self._section_bar_for(section_type))
        self.content_box.Layout()
        self.Layout()

    def _subtitle_for(self, section_type, section_state):
        """A "N Questions    M Minutes" subheading derived from SECTION_META
        (or the drill state for the mixed-drill override)."""
        override = section_state and getattr(section_state, "display_label", None)
        if override:
            n = len(getattr(section_state, "question_ids", []) or []) or 10
            mins = max(1, getattr(section_state, "time_limit", 0) // 60)
            return "{n} Questions    ~{mins} Minutes".format(n=n, mins=mins)
        meta = SECTION_META.get(section_type)
        if not meta:
            return ""
        _measure, _idx, time_limit, q_count = meta
        mins = max(1, time_limit // 60)
        if section_type == SectionType.AWA:
            return "1 Task    {mins} Minutes".format(mins=mins)
        return "{n} Questions    {mins} Minutes".format(n=q_count, mins=mins)

    @staticmethod
    def _section_bar_for(section_type):
        """Pink-bar measure label for the section-intro page."""
        meta = SECTION_META.get(section_type)
        if not meta:
            return "Section"
        measure = meta[0]
        if measure == "awa":
            return "Analytical Writing"
        if measure == "verbal":
            return "Verbal Reasoning"
        if measure == "quant":
            return "Quantitative Reasoning"
        return "Section"

    def set_on_begin(self, callback):
        """callback()"""
        self._on_begin = callback

    def set_on_cancel(self, callback):
        """callback() — called when user clicks Back/Cancel"""
        self._on_cancel = callback

    # ── event plumbing ────────────────────────────────────────────────

    def _on_cancel_click(self, event=None):
        if self._on_cancel:
            self._on_cancel()

    def _on_begin_click(self, event=None):
        if self._on_begin:
            self._on_begin()
