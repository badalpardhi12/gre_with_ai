"""
Results screen — displays final scores with section breakdown and per-question review.
"""
import wx

from widgets.theme import Color


class ResultsScreen(wx.Panel):
    """Post-exam results display with scores, breakdown, and question review."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)
        self._on_home = None
        self._question_details = []
        self._build_ui()

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Title
        title = wx.StaticText(self, label="Test Results")
        title.SetFont(wx.Font(24, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                               wx.FONTWEIGHT_BOLD))
        main_sizer.Add(title, 0, wx.ALIGN_CENTER | wx.TOP, 24)

        # ── Score cards ──────────────────────────────────────────────
        score_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # Verbal score card
        self.verbal_panel = self._create_score_card("Verbal Reasoning", "—")
        score_sizer.Add(self.verbal_panel, 1, wx.EXPAND | wx.ALL, 12)

        # Quant score card
        self.quant_panel = self._create_score_card("Quantitative Reasoning", "—")
        score_sizer.Add(self.quant_panel, 1, wx.EXPAND | wx.ALL, 12)

        # AWA score card
        self.awa_panel = self._create_score_card("Analytical Writing", "—")
        score_sizer.Add(self.awa_panel, 1, wx.EXPAND | wx.ALL, 12)

        main_sizer.Add(score_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 20)

        # ── Section breakdown ─────────────────────────────────────────
        breakdown_label = wx.StaticText(self, label="Section Breakdown")
        breakdown_label.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                         wx.FONTWEIGHT_BOLD))
        main_sizer.Add(breakdown_label, 0, wx.LEFT | wx.TOP, 20)

        self.breakdown_list = wx.ListCtrl(self, style=wx.LC_REPORT, size=(-1, 140))
        self.breakdown_list.InsertColumn(0, "Section", width=180)
        self.breakdown_list.InsertColumn(1, "Difficulty", width=100)
        self.breakdown_list.InsertColumn(2, "Correct", width=100)
        self.breakdown_list.InsertColumn(3, "Accuracy", width=100)
        self.breakdown_list.InsertColumn(4, "Time Used", width=120)
        self.breakdown_list.InsertColumn(5, "Time Limit", width=120)
        main_sizer.Add(self.breakdown_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 20)

        # ── Question-by-question review ───────────────────────────────
        detail_label = wx.StaticText(self, label="Question Detail")
        detail_label.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                      wx.FONTWEIGHT_BOLD))
        main_sizer.Add(detail_label, 0, wx.LEFT | wx.TOP, 20)

        self.detail_list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.detail_list.InsertColumn(0, "#", width=40)
        self.detail_list.InsertColumn(1, "Section", width=120)
        self.detail_list.InsertColumn(2, "Type", width=120)
        self.detail_list.InsertColumn(3, "Result", width=100)
        self.detail_list.InsertColumn(4, "Time (s)", width=80)
        self.detail_list.InsertColumn(5, "Difficulty", width=80)
        main_sizer.Add(self.detail_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 20)

        # ── Buttons ───────────────────────────────────────────────────
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()

        home_btn = wx.Button(self, label="  Return to Home  ", size=(180, 40))
        home_btn.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                  wx.FONTWEIGHT_BOLD))
        home_btn.Bind(wx.EVT_BUTTON, self._on_home_click)
        btn_sizer.Add(home_btn, 0, wx.ALL, 12)

        main_sizer.Add(btn_sizer, 0, wx.EXPAND)
        self.SetSizer(main_sizer)

    def _create_score_card(self, title, score_text):
        """Create a score display card."""
        panel = wx.Panel(self, style=wx.BORDER_SIMPLE)
        panel.SetBackgroundColour(Color.BG_SURFACE)
        sizer = wx.BoxSizer(wx.VERTICAL)

        label = wx.StaticText(panel, label=title)
        label.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                               wx.FONTWEIGHT_BOLD))
        label.SetForegroundColour(Color.TEXT_SECONDARY)
        sizer.Add(label, 0, wx.ALIGN_CENTER | wx.TOP, 12)

        score = wx.StaticText(panel, label=score_text, name="score_value")
        score.SetFont(wx.Font(28, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                               wx.FONTWEIGHT_BOLD))
        score.SetForegroundColour(Color.ACCENT)
        sizer.Add(score, 0, wx.ALIGN_CENTER | wx.ALL, 8)

        panel.SetSizer(sizer)
        return panel

    def _update_score_card(self, panel, score_text):
        for child in panel.GetChildren():
            if child.GetName() == "score_value":
                child.SetLabel(score_text)
                break

    # ── Public API ────────────────────────────────────────────────────

    def load_results(self, scores, section_summaries, question_details):
        """
        Populate the results screen.

        scores: dict with verbal_estimated_low/high, quant_estimated_low/high, awa_estimated
        section_summaries: list of section summary dicts
        question_details: list of per-question detail dicts
        """
        # Score cards
        v_low = scores.get("verbal_estimated_low")
        v_high = scores.get("verbal_estimated_high")
        if v_low is not None:
            self._update_score_card(self.verbal_panel, f"{v_low}–{v_high}")
        else:
            self._update_score_card(self.verbal_panel, "N/A")

        q_low = scores.get("quant_estimated_low")
        q_high = scores.get("quant_estimated_high")
        if q_low is not None:
            self._update_score_card(self.quant_panel, f"{q_low}–{q_high}")
        else:
            self._update_score_card(self.quant_panel, "N/A")

        awa = scores.get("awa_estimated")
        if awa is not None:
            self._update_score_card(self.awa_panel, f"{awa:.1f}")
        else:
            self._update_score_card(self.awa_panel, "N/A")

        # Section breakdown
        self.breakdown_list.DeleteAllItems()
        for sec in section_summaries:
            idx = self.breakdown_list.InsertItem(
                self.breakdown_list.GetItemCount(), sec.get("section_name", ""))
            self.breakdown_list.SetItem(idx, 1, sec.get("difficulty_band", "medium"))
            total = sec.get("total_questions", 0)
            correct = sec.get("correct", 0)
            self.breakdown_list.SetItem(idx, 2, f"{correct}/{total}")
            acc = sec.get("accuracy", 0)
            self.breakdown_list.SetItem(idx, 3, f"{acc:.0%}")

            def fmt_time(s):
                m, sec_r = divmod(s, 60)
                return f"{m}m {sec_r}s"

            self.breakdown_list.SetItem(idx, 4, fmt_time(sec.get("time_used", 0)))
            self.breakdown_list.SetItem(idx, 5, fmt_time(sec.get("time_limit", 0)))

        # Question details
        self.detail_list.DeleteAllItems()
        for i, q in enumerate(question_details):
            idx = self.detail_list.InsertItem(self.detail_list.GetItemCount(), str(i + 1))
            self.detail_list.SetItem(idx, 1, q.get("measure", ""))
            self.detail_list.SetItem(idx, 2, q.get("subtype", ""))

            correct = q.get("is_correct")
            if correct is True:
                self.detail_list.SetItem(idx, 3, "✓ Correct")
                self.detail_list.SetItemBackgroundColour(idx, Color.MASTERY[4])
                self.detail_list.SetItemTextColour(idx, Color.TEXT_INVERSE)
            elif correct is False:
                self.detail_list.SetItem(idx, 3, "✗ Incorrect")
                self.detail_list.SetItemBackgroundColour(idx, Color.MASTERY[1])
                self.detail_list.SetItemTextColour(idx, Color.TEXT_PRIMARY)
            else:
                self.detail_list.SetItem(idx, 3, "— Unanswered")

            self.detail_list.SetItem(idx, 4, str(q.get("time_spent", 0)))
            self.detail_list.SetItem(idx, 5, str(q.get("difficulty", "")))

    def set_on_home(self, callback):
        """callback()"""
        self._on_home = callback

    def _on_home_click(self, event):
        if self._on_home:
            self._on_home()
