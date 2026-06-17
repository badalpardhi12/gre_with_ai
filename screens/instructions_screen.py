"""
Instructions screen — the ETS GRE section-intro page shown before each
section begins.

Re-skinned to "exam mode" (docs/gre_ui_spec_2026_06.md §7): a navy header
strip carrying the "ETS  GRE" lockup over a white, serif content area, with a
prominent blue "Continue" button and a grey "Back to Dashboard"/"Cancel"
button bottom-aligned. Distinct from the dark study-app chrome.

Public API consumed by main_frame.py is preserved verbatim:
``set_section(section_type, section_state=None)``, ``set_on_begin(callback)``,
``set_on_cancel(callback)`` and the ``display_label`` override path for mixed
Quick Drills.
"""
import wx

from models.exam_session import SectionType, SECTION_META
from widgets import ui_scale
from widgets.exam_button import ExamButton
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
    """ETS-skinned section-intro page with a Continue affordance."""

    # Wrap width for the serif body — scales with DPI so the prose doesn't
    # sit in a narrow column on a 4K display.
    _WRAP_BASE = 760

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(ExamColor.CONTENT_BG)
        self._on_begin = None
        self._on_cancel = None
        self._build_ui()

    # ── construction ──────────────────────────────────────────────────

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        main_sizer.Add(self._build_header(), 0, wx.EXPAND)

        # ── White content area (serif title + body) ───────────────────
        content = wx.BoxSizer(wx.VERTICAL)
        content.AddSpacer(ui_scale.space(8))

        self.title_label = wx.StaticText(self, label="Section Instructions")
        self.title_label.SetForegroundColour(ExamColor.TEXT)
        self.title_label.SetFont(
            ui_scale.exam_serif(ui_scale.BASE_TITLE, wx.FONTWEIGHT_BOLD)
        )
        content.Add(self.title_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM,
                    ui_scale.space(5))

        self.body_text = wx.StaticText(self, label="")
        self.body_text.SetForegroundColour(ExamColor.TEXT)
        self.body_text.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
        self.body_text.Wrap(ui_scale.font_size(self._WRAP_BASE))
        content.Add(self.body_text, 0, wx.LEFT | wx.RIGHT, ui_scale.space(5))

        main_sizer.Add(content, 1, wx.EXPAND | wx.ALL, ui_scale.space(6))

        # ── Bottom-aligned button row (Cancel · Continue) ─────────────
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.cancel_btn = ExamButton(self, "Back to Dashboard", kind="grey",
                                     icon="◀")
        self.cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel_click)
        btn_row.Add(self.cancel_btn, 0, wx.RIGHT, ui_scale.space(4))

        btn_row.AddStretchSpacer()

        self.begin_btn = ExamButton(self, "Continue", kind="next", icon="▶",
                                    icon_after=True)
        self.begin_btn.Bind(wx.EVT_BUTTON, self._on_begin_click)
        btn_row.Add(self.begin_btn, 0)

        main_sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, ui_scale.space(6))

        self.SetSizer(main_sizer)

    def _build_header(self):
        """Navy strip with the inline 'ETS  GRE' lockup (matches the
        question screen header: white 'ETS' on navy + italic white 'GRE')."""
        self.header = wx.Panel(self)
        self.header.SetBackgroundColour(ExamColor.HEADER_NAVY)
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)

        logo = wx.StaticText(self.header, label="ETS")
        logo.SetForegroundColour(ExamColor.HEADER_NAVY)
        logo.SetBackgroundColour(ExamColor.TEXT_ON_NAVY)
        logo.SetFont(ui_scale.exam_sans(ui_scale.EXAM_COUNTER_PT, wx.FONTWEIGHT_BOLD))
        header_sizer.Add(logo, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(3))

        gre = wx.StaticText(self.header, label="GRE")
        gre.SetForegroundColour(ExamColor.TEXT_ON_NAVY)
        gre.SetFont(ui_scale.exam_sans(ui_scale.EXAM_STEM_PT, wx.FONTWEIGHT_BOLD,
                                       wx.FONTSTYLE_ITALIC))
        header_sizer.Add(gre, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, ui_scale.space(3))

        header_sizer.AddStretchSpacer()
        self.header.SetSizer(header_sizer)
        return self.header

    # ── public API (preserved for main_frame.py) ──────────────────────

    def set_section(self, section_type, section_state=None):
        """Configure for a specific section.

        When `section_state` carries a `display_label` (mixed Quick
        Drill), the screen overrides the canonical title and body so
        the user doesn't see "Verbal Reasoning — Section 1" before a
        drill that actually mixes both measures.

        For Quant sections the "Figures are not necessarily drawn to
        scale." caveat is appended to the body (it isn't in the
        SECTION_INSTRUCTIONS text but belongs on the real ETS page).
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
                f"This drill contains {n} questions targeting your weak areas. "
                f"You have ~{mins} minutes.\n\n"
                "Questions are mixed across Verbal Reasoning and Quantitative "
                "Reasoning. Each question's measure (Verbal / Quant) is shown "
                "above it; the on-screen calculator appears only on quant "
                "questions.\n\n"
                "You may navigate freely within this drill, mark questions for "
                "review, and end the drill at any time."
            )
        elif _is_quant_section(section_type) and FIGURES_CAVEAT not in body:
            body = body + "\n\n" + FIGURES_CAVEAT

        self.title_label.SetLabel(title)
        self.body_text.SetLabel(body)
        self.body_text.Wrap(ui_scale.font_size(self._WRAP_BASE))
        self.Layout()

    def set_on_begin(self, callback):
        """callback()"""
        self._on_begin = callback

    def set_on_cancel(self, callback):
        """callback() — called when user clicks Back/Cancel"""
        self._on_cancel = callback

    # ── event plumbing ────────────────────────────────────────────────

    def _on_cancel_click(self, event):
        if self._on_cancel:
            self._on_cancel()

    def _on_begin_click(self, event):
        if self._on_begin:
            self._on_begin()
