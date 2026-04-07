"""
Progress screen — historical test results, score trends, and topic diagnostics.
"""
import wx
from datetime import datetime

from models.database import (
    db, Session as DBSession, ScoringResult, SectionResult, Response, Question,
)
from services.analytics import AnalyticsService


class ProgressScreen(wx.Panel):
    """Displays test history, score trends, and per-topic diagnostics."""

    def __init__(self, parent):
        super().__init__(parent)
        self._on_home = None
        self._build_ui()

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Title
        title = wx.StaticText(self, label="Progress Dashboard")
        title.SetFont(wx.Font(22, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                               wx.FONTWEIGHT_BOLD))
        main_sizer.Add(title, 0, wx.ALIGN_CENTER | wx.TOP, 20)

        # ── Score summary cards ───────────────────────────────────────
        summary_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.total_tests_label = self._make_stat_card(summary_sizer, "Tests Taken", "0")
        self.avg_verbal_label = self._make_stat_card(summary_sizer, "Avg Verbal", "—")
        self.avg_quant_label = self._make_stat_card(summary_sizer, "Avg Quant", "—")
        self.avg_awa_label = self._make_stat_card(summary_sizer, "Avg AWA", "—")

        main_sizer.Add(summary_sizer, 0, wx.EXPAND | wx.ALL, 16)

        # ── Test history list ─────────────────────────────────────────
        hist_label = wx.StaticText(self, label="Test History")
        hist_label.SetFont(wx.Font(13, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                    wx.FONTWEIGHT_BOLD))
        main_sizer.Add(hist_label, 0, wx.LEFT | wx.TOP, 16)

        self.history_list = wx.ListCtrl(self, style=wx.LC_REPORT, size=(-1, 180))
        self.history_list.InsertColumn(0, "Date", width=160)
        self.history_list.InsertColumn(1, "Type", width=120)
        self.history_list.InsertColumn(2, "Verbal", width=100)
        self.history_list.InsertColumn(3, "Quant", width=100)
        self.history_list.InsertColumn(4, "AWA", width=80)
        self.history_list.InsertColumn(5, "Mode", width=100)
        main_sizer.Add(self.history_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 16)

        # ── Topic breakdown ───────────────────────────────────────────
        topic_label = wx.StaticText(self, label="Topic Performance (All Sessions)")
        topic_label.SetFont(wx.Font(13, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                     wx.FONTWEIGHT_BOLD))
        main_sizer.Add(topic_label, 0, wx.LEFT | wx.TOP, 16)

        self.topic_list = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.topic_list.InsertColumn(0, "Topic", width=200)
        self.topic_list.InsertColumn(1, "Measure", width=100)
        self.topic_list.InsertColumn(2, "Questions", width=100)
        self.topic_list.InsertColumn(3, "Correct", width=100)
        self.topic_list.InsertColumn(4, "Accuracy", width=100)
        main_sizer.Add(self.topic_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 16)

        # ── Back button ──────────────────────────────────────────────
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        home_btn = wx.Button(self, label="  Back to Home  ", size=(160, 40))
        home_btn.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                  wx.FONTWEIGHT_BOLD))
        home_btn.Bind(wx.EVT_BUTTON, self._on_home_click)
        btn_sizer.Add(home_btn, 0, wx.ALL, 12)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND)

        self.SetSizer(main_sizer)

    def _make_stat_card(self, parent_sizer, title, value):
        """Create a stat card and return the value label."""
        panel = wx.Panel(self, style=wx.BORDER_SIMPLE)
        panel.SetBackgroundColour(wx.Colour(245, 245, 250))
        sizer = wx.BoxSizer(wx.VERTICAL)

        lbl = wx.StaticText(panel, label=title)
        lbl.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                              wx.FONTWEIGHT_BOLD))
        lbl.SetForegroundColour(wx.Colour(60, 60, 60))
        sizer.Add(lbl, 0, wx.ALIGN_CENTER | wx.TOP, 10)

        val = wx.StaticText(panel, label=value)
        val.SetFont(wx.Font(22, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                              wx.FONTWEIGHT_BOLD))
        val.SetForegroundColour(wx.Colour(25, 80, 160))
        sizer.Add(val, 0, wx.ALIGN_CENTER | wx.ALL, 6)

        panel.SetSizer(sizer)
        parent_sizer.Add(panel, 1, wx.EXPAND | wx.ALL, 6)
        return val

    def load_data(self):
        """Load and display all progress data from the database."""
        db.connect(reuse_if_open=True)

        # Fetch completed sessions with scores
        sessions = (DBSession.select()
                    .where(DBSession.state == "completed")
                    .order_by(DBSession.created_at.desc()))

        self.history_list.DeleteAllItems()

        verbal_scores = []
        quant_scores = []
        awa_scores = []
        session_ids = []

        for sess in sessions:
            score = ScoringResult.get_or_none(ScoringResult.session == sess.id)
            if not score:
                continue

            session_ids.append(sess.id)
            date_str = sess.created_at.strftime("%Y-%m-%d %H:%M") if sess.created_at else "—"
            idx = self.history_list.InsertItem(self.history_list.GetItemCount(), date_str)
            self.history_list.SetItem(idx, 1, sess.test_type)

            v_low = score.verbal_estimated_low
            v_high = score.verbal_estimated_high
            if v_low is not None:
                self.history_list.SetItem(idx, 2, f"{v_low}–{v_high}")
                verbal_scores.append((v_low + v_high) / 2)
            else:
                self.history_list.SetItem(idx, 2, "N/A")

            q_low = score.quant_estimated_low
            q_high = score.quant_estimated_high
            if q_low is not None:
                self.history_list.SetItem(idx, 3, f"{q_low}–{q_high}")
                quant_scores.append((q_low + q_high) / 2)
            else:
                self.history_list.SetItem(idx, 3, "N/A")

            awa = score.awa_estimated
            if awa is not None:
                self.history_list.SetItem(idx, 4, f"{awa:.1f}")
                awa_scores.append(awa)
            else:
                self.history_list.SetItem(idx, 4, "N/A")

            self.history_list.SetItem(idx, 5, sess.mode)

        # Summary stats
        total = len(session_ids)
        self.total_tests_label.SetLabel(str(total))
        self.avg_verbal_label.SetLabel(
            f"{sum(verbal_scores)/len(verbal_scores):.0f}" if verbal_scores else "—")
        self.avg_quant_label.SetLabel(
            f"{sum(quant_scores)/len(quant_scores):.0f}" if quant_scores else "—")
        self.avg_awa_label.SetLabel(
            f"{sum(awa_scores)/len(awa_scores):.1f}" if awa_scores else "—")

        # Topic breakdown across all sessions
        self.topic_list.DeleteAllItems()
        if session_ids:
            self._load_topic_breakdown(session_ids)

        self.Layout()

    def _load_topic_breakdown(self, session_ids):
        """Aggregate topic performance across multiple sessions."""
        from collections import defaultdict
        breakdown = defaultdict(lambda: {"total": 0, "correct": 0, "measure": ""})

        for sid in session_ids:
            query = (Response.select(Response, Question)
                     .join(Question)
                     .where(Response.session == sid,
                            Response.is_correct.is_null(False)))
            for r in query:
                tags = r.question.get_tags()
                measure = r.question.measure
                for tag in tags:
                    key = (tag, measure)
                    breakdown[key]["total"] += 1
                    breakdown[key]["measure"] = measure
                    if r.is_correct:
                        breakdown[key]["correct"] += 1

        # Sort by total questions descending
        sorted_topics = sorted(breakdown.items(), key=lambda x: x[1]["total"], reverse=True)

        for (tag, measure), stats in sorted_topics:
            idx = self.topic_list.InsertItem(self.topic_list.GetItemCount(), tag)
            self.topic_list.SetItem(idx, 1, measure)
            self.topic_list.SetItem(idx, 2, str(stats["total"]))
            self.topic_list.SetItem(idx, 3, str(stats["correct"]))
            acc = stats["correct"] / stats["total"] if stats["total"] > 0 else 0
            self.topic_list.SetItem(idx, 4, f"{acc:.0%}")

            # Color-code accuracy
            if acc >= 0.8:
                self.topic_list.SetItemBackgroundColour(idx, wx.Colour(230, 255, 230))
                self.topic_list.SetItemTextColour(idx, wx.Colour(0, 0, 0))
            elif acc < 0.5:
                self.topic_list.SetItemBackgroundColour(idx, wx.Colour(255, 235, 235))
                self.topic_list.SetItemTextColour(idx, wx.Colour(0, 0, 0))

    # ── Callbacks ─────────────────────────────────────────────────────

    def set_on_home(self, callback):
        self._on_home = callback

    def _on_home_click(self, event):
        if self._on_home:
            self._on_home()
