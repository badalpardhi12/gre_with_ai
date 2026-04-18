"""
Instructions screen — displayed before each section begins.
"""
import wx

from models.exam_session import SectionType, SECTION_META


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


class InstructionsScreen(wx.Panel):
    """Displays section instructions with a Begin button."""

    def __init__(self, parent):
        super().__init__(parent)
        self._on_begin = None
        self._on_cancel = None
        self._build_ui()

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Top bar with back button
        top_bar = wx.BoxSizer(wx.HORIZONTAL)
        self.back_btn = wx.Button(self, label="← Back to Dashboard")
        self.back_btn.Bind(wx.EVT_BUTTON, self._on_cancel_click)
        top_bar.Add(self.back_btn, 0, wx.ALL, 8)
        top_bar.AddStretchSpacer()
        main_sizer.Add(top_bar, 0, wx.EXPAND)
        main_sizer.AddSpacer(20)

        self.title_label = wx.StaticText(self, label="Section Instructions")
        self.title_label.SetFont(wx.Font(22, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                          wx.FONTWEIGHT_BOLD))
        main_sizer.Add(self.title_label, 0, wx.ALIGN_CENTER | wx.BOTTOM, 20)

        self.body_text = wx.StaticText(self, label="")
        self.body_text.SetFont(wx.Font(13, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                        wx.FONTWEIGHT_NORMAL))
        self.body_text.Wrap(700)
        main_sizer.Add(self.body_text, 0, wx.LEFT | wx.RIGHT, 80)

        main_sizer.AddSpacer(30)

        # Buttons row
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer()

        self.cancel_btn = wx.Button(self, label="  Cancel  ", size=(120, 44))
        self.cancel_btn.SetFont(wx.Font(13, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                         wx.FONTWEIGHT_NORMAL))
        self.cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel_click)
        btn_row.Add(self.cancel_btn, 0, wx.RIGHT, 20)

        self.begin_btn = wx.Button(self, label="  Begin Section  ", size=(180, 44))
        self.begin_btn.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                        wx.FONTWEIGHT_BOLD))
        self.begin_btn.SetBackgroundColour(wx.Colour(46, 125, 50))
        self.begin_btn.SetForegroundColour(wx.WHITE)
        self.begin_btn.Bind(wx.EVT_BUTTON, self._on_begin_click)
        btn_row.Add(self.begin_btn, 0)
        btn_row.AddStretchSpacer()
        main_sizer.Add(btn_row, 0, wx.EXPAND)

        self.SetSizer(main_sizer)

    def set_section(self, section_type):
        """Configure for a specific section."""
        info = SECTION_INSTRUCTIONS.get(section_type, {})
        self.title_label.SetLabel(info.get("title", "Section"))
        self.body_text.SetLabel(info.get("body", ""))
        self.body_text.Wrap(700)
        self.Layout()

    def set_on_begin(self, callback):
        """callback()"""
        self._on_begin = callback

    def set_on_cancel(self, callback):
        """callback() — called when user clicks Back/Cancel"""
        self._on_cancel = callback

    def _on_cancel_click(self, event):
        if self._on_cancel:
            self._on_cancel()

    def _on_begin_click(self, event):
        if self._on_begin:
            self._on_begin()
