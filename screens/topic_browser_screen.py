"""
Topic browser screen — tree view of the taxonomy with lesson + drill access per subtopic.
"""
import wx
from collections import Counter

from models.database import Question, Lesson, MasteryRecord
from models.taxonomy import (
    QUANT_TAXONOMY, VERBAL_TAXONOMY, get_subtopic_meta,
)
from widgets import ui_scale


class TopicBrowserScreen(wx.Panel):
    """Tree view of all subtopics with lesson + question count + mastery."""

    def __init__(self, parent):
        super().__init__(parent)
        self._on_back = None
        self._on_open_lesson = None
        self._on_start_drill = None
        self._build_ui()

    def _build_ui(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        hdr = wx.BoxSizer(wx.HORIZONTAL)
        self.back_btn = wx.Button(self, label="← Back to Dashboard",
                                  size=(-1, ui_scale.font_size(36)))
        self.back_btn.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.back_btn.Bind(wx.EVT_BUTTON, lambda _: self._on_back() if self._on_back else None)
        hdr.Add(self.back_btn, 0, wx.ALL, 8)

        title = wx.StaticText(self, label="Topic Browser")
        title.SetFont(wx.Font(ui_scale.large(), wx.FONTFAMILY_DEFAULT,
                              wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        hdr.Add(title, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        sizer.Add(hdr, 0, wx.EXPAND)
        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND)

        # Tree
        self.tree = wx.TreeCtrl(self, style=wx.TR_HAS_BUTTONS | wx.TR_DEFAULT_STYLE)
        # Set tree font
        self.tree.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                   wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        sizer.Add(self.tree, 1, wx.EXPAND | wx.ALL, 8)

        # Action bar
        actions = wx.BoxSizer(wx.HORIZONTAL)
        actions.AddStretchSpacer()

        self.open_lesson_btn = wx.Button(self, label="Open Lesson",
                                         size=(ui_scale.font_size(180), ui_scale.font_size(40)))
        self.open_lesson_btn.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                              wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.open_lesson_btn.Bind(wx.EVT_BUTTON, self._on_lesson_clicked)
        actions.Add(self.open_lesson_btn, 0, wx.ALL, 6)

        self.drill_btn = wx.Button(self, label="Start Drill (10 questions)",
                                   size=(ui_scale.font_size(240), ui_scale.font_size(40)))
        self.drill_btn.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                        wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.drill_btn.Bind(wx.EVT_BUTTON, self._on_drill_clicked)
        actions.Add(self.drill_btn, 0, wx.ALL, 6)
        actions.AddStretchSpacer()

        sizer.Add(actions, 0, wx.EXPAND | wx.BOTTOM, 8)

        self.SetSizer(sizer)

    def set_on_back(self, h): self._on_back = h
    def set_on_open_lesson(self, h): self._on_open_lesson = h
    def set_on_start_drill(self, h): self._on_start_drill = h

    def refresh(self):
        """Build the tree from the current taxonomy + DB stats."""
        self.tree.DeleteAllItems()
        root = self.tree.AddRoot("All Topics")

        # Per-subtopic question counts
        sub_counts = Counter()
        for q in Question.select(Question.subtopic):
            if q.subtopic:
                sub_counts[q.subtopic] += 1

        # Per-subtopic lesson availability
        lesson_subs = {l.subtopic for l in Lesson.select(Lesson.subtopic)}

        # Mastery
        mastery = {m.subtopic: m.mastery_score for m in MasteryRecord.select()}

        # Quant
        quant_node = self.tree.AppendItem(root, "📊 Quantitative")
        for topic_id, td in QUANT_TAXONOMY.items():
            topic_node = self.tree.AppendItem(quant_node, td["display_name"])
            for sub_id, sd in td["subtopics"].items():
                qc = sub_counts.get(sub_id, 0)
                lesson = "📖" if sub_id in lesson_subs else "  "
                m = mastery.get(sub_id, 0)
                m_pct = int(m * 100) if m > 0 else None
                m_str = f" — Mastery: {m_pct}%" if m_pct is not None else ""
                label = f"{lesson} {sd['display_name']}  ({qc} qs){m_str}"
                node = self.tree.AppendItem(topic_node, label)
                self.tree.SetItemData(node, sub_id)

        # Verbal
        verbal_node = self.tree.AppendItem(root, "📚 Verbal")
        for topic_id, td in VERBAL_TAXONOMY.items():
            topic_node = self.tree.AppendItem(verbal_node, td["display_name"])
            for sub_id, sd in td["subtopics"].items():
                qc = sub_counts.get(sub_id, 0)
                lesson = "📖" if sub_id in lesson_subs else "  "
                m = mastery.get(sub_id, 0)
                m_pct = int(m * 100) if m > 0 else None
                m_str = f" — Mastery: {m_pct}%" if m_pct is not None else ""
                label = f"{lesson} {sd['display_name']}  ({qc} qs){m_str}"
                node = self.tree.AppendItem(verbal_node, label)
                self.tree.SetItemData(node, sub_id)

        self.tree.Expand(root)
        self.tree.Expand(quant_node)
        self.tree.Expand(verbal_node)

    def _selected_subtopic(self):
        item = self.tree.GetSelection()
        if not item.IsOk():
            return None
        return self.tree.GetItemData(item)

    def _on_lesson_clicked(self, _):
        sub = self._selected_subtopic()
        if sub and self._on_open_lesson:
            self._on_open_lesson(sub)
        elif not sub:
            wx.MessageBox("Select a subtopic in the tree first.", "Info",
                          wx.OK | wx.ICON_INFORMATION)

    def _on_drill_clicked(self, _):
        sub = self._selected_subtopic()
        if sub and self._on_start_drill:
            self._on_start_drill(sub)
        elif not sub:
            wx.MessageBox("Select a subtopic in the tree first.", "Info",
                          wx.OK | wx.ICON_INFORMATION)
