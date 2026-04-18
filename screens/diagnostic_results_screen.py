"""
Diagnostic results screen — shown after the 30-question diagnostic completes.

Renders the DiagnosticResult payload (per-topic accuracy, weakness ranking,
predicted scaled-score band) and offers a "Build Study Plan" CTA that pre-fills
the StudyPlan dialog with the diagnostic in scope.
"""
import json

import wx

from widgets import ui_scale


class DiagnosticResultsScreen(wx.Panel):
    """Renders a DiagnosticResult and links to study-plan generation."""

    def __init__(self, parent):
        super().__init__(parent)
        from widgets.theme import Color
        self.SetBackgroundColour(Color.BG_PAGE)
        self._on_back = None
        self._on_build_plan = None
        self._diag = None
        self._build_ui()

    def _build_ui(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Header
        title = wx.StaticText(self, label="Diagnostic Results")
        title.SetFont(wx.Font(ui_scale.title(), wx.FONTFAMILY_DEFAULT,
                              wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(title, 0, wx.ALIGN_CENTER | wx.TOP, 20)

        # Predicted bands
        self.bands_label = wx.StaticText(self, label="")
        self.bands_label.SetFont(wx.Font(ui_scale.large(), wx.FONTFAMILY_DEFAULT,
                                         wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        sizer.Add(self.bands_label, 0, wx.ALIGN_CENTER | wx.ALL, 16)

        # Per-topic table
        topic_label = wx.StaticText(self, label="Per-topic accuracy")
        topic_label.SetFont(wx.Font(ui_scale.large(), wx.FONTFAMILY_DEFAULT,
                                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(topic_label, 0, wx.LEFT | wx.TOP, 16)

        self.topic_list = wx.ListCtrl(self, style=wx.LC_REPORT, size=(-1, 180))
        self.topic_list.InsertColumn(0, "Topic", width=240)
        self.topic_list.InsertColumn(1, "Attempted", width=100)
        self.topic_list.InsertColumn(2, "Correct", width=100)
        self.topic_list.InsertColumn(3, "Accuracy", width=100)
        sizer.Add(self.topic_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 16)

        # Weakness ranking
        weak_label = wx.StaticText(self, label="Top weaknesses")
        weak_label.SetFont(wx.Font(ui_scale.large(), wx.FONTFAMILY_DEFAULT,
                                   wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(weak_label, 0, wx.LEFT | wx.TOP, 16)

        self.weak_list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.weak_list.InsertColumn(0, "Subtopic", width=300)
        self.weak_list.InsertColumn(1, "Attempted", width=100)
        self.weak_list.InsertColumn(2, "Accuracy", width=100)
        sizer.Add(self.weak_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 16)

        # CTA buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.back_btn = wx.Button(self, label="Back to Dashboard",
                                  size=(-1, ui_scale.font_size(40)))
        self.back_btn.Bind(wx.EVT_BUTTON, lambda _: self._on_back() if self._on_back else None)
        btn_sizer.Add(self.back_btn, 0, wx.ALL, 12)
        btn_sizer.AddStretchSpacer()
        self.plan_btn = wx.Button(self, label="Build a Study Plan from these results →",
                                  size=(-1, ui_scale.font_size(40)))
        self.plan_btn.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                      wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.plan_btn.Bind(wx.EVT_BUTTON,
                           lambda _: self._on_build_plan() if self._on_build_plan else None)
        btn_sizer.Add(self.plan_btn, 0, wx.ALL, 12)
        sizer.Add(btn_sizer, 0, wx.EXPAND)

        self.SetSizer(sizer)

    def set_on_back(self, handler):
        self._on_back = handler

    def set_on_build_plan(self, handler):
        self._on_build_plan = handler

    def load(self, diag):
        """Populate from a DiagnosticResult row."""
        self._diag = diag
        self.bands_label.SetLabel(
            f"Predicted Verbal: {diag.predicted_verbal_band or 'unknown'}    "
            f"Predicted Quant: {diag.predicted_quant_band or 'unknown'}"
        )

        try:
            scores = json.loads(diag.scores_per_topic_json or "{}")
        except (ValueError, TypeError):
            scores = {}
        self.topic_list.DeleteAllItems()
        for topic, data in sorted(scores.items()):
            idx = self.topic_list.InsertItem(self.topic_list.GetItemCount(), topic)
            self.topic_list.SetItem(idx, 1, str(data.get("attempted", 0)))
            self.topic_list.SetItem(idx, 2, str(data.get("correct", 0)))
            acc = data.get("accuracy", 0)
            self.topic_list.SetItem(idx, 3, f"{acc:.0%}")

        try:
            weak = json.loads(diag.weakness_ranking_json or "[]")
        except (ValueError, TypeError):
            weak = []
        self.weak_list.DeleteAllItems()
        for entry in weak[:10]:
            idx = self.weak_list.InsertItem(self.weak_list.GetItemCount(),
                                            entry.get("subtopic", ""))
            self.weak_list.SetItem(idx, 1, str(entry.get("attempted", 0)))
            self.weak_list.SetItem(idx, 2, f"{entry.get('accuracy', 0):.0%}")

        self.Layout()
