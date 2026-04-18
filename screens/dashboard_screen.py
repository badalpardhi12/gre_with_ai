"""
Main dashboard screen — the new home for the GRE platform.

Displays:
- Today's tasks from active study plan
- Mastery overview (per-topic strengths/weaknesses)
- Score forecast widget
- Recent activity
- Quick-action buttons (resume drill, vocab, lesson, practice test, diagnostic)
"""
import json
import wx

from models.database import (
    init_db, Question, Lesson, MasteryRecord,
    StudyPlan, DiagnosticResult,
)
from services.score_forecast import overall_forecast
from services.mastery import weakness_ranking, get_all_mastery
from services.study_plan import get_today_tasks, get_active_plan
from services.diagnostic import get_latest_diagnostic
from services.srs import stats as vocab_stats
from widgets import ui_scale


class DashboardScreen(wx.Panel):
    """Main app hub showing study plan, mastery, forecast, and quick actions."""

    def __init__(self, parent):
        super().__init__(parent)

        self._on_take_diagnostic = None
        self._on_start_drill = None
        self._on_start_lesson = None
        self._on_start_vocab = None
        self._on_start_practice_test = None
        self._on_start_full_mock = None
        self._on_progress = None
        self._on_settings = None  # set via set_handlers(settings=...)
        self._on_browse_topics = None

        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Header
        header = wx.BoxSizer(wx.HORIZONTAL)
        title = wx.StaticText(self, label="GRE with AI")
        title.SetFont(wx.Font(ui_scale.title(), wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                              wx.FONTWEIGHT_BOLD))
        header.Add(title, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 16)

        self.settings_btn = wx.Button(self, label="⚙ Settings",
                                      size=(-1, ui_scale.font_size(36)))
        self.settings_btn.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                          wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.settings_btn.Bind(wx.EVT_BUTTON, self._handle_settings)
        header.Add(self.settings_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)

        main_sizer.Add(header, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 14)
        main_sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        # Main content: 2-column scrollable
        self.content = wx.ScrolledWindow(self)
        self.content.SetScrollRate(0, 14)
        content_sizer = wx.BoxSizer(wx.VERTICAL)

        # Top row: Score forecast + Today's plan
        top_row = wx.BoxSizer(wx.HORIZONTAL)
        top_row.Add(self._build_forecast_card(), 1, wx.EXPAND | wx.ALL, 10)
        top_row.Add(self._build_today_card(), 1, wx.EXPAND | wx.ALL, 10)
        content_sizer.Add(top_row, 0, wx.EXPAND)

        # Quick actions row
        content_sizer.Add(self._build_actions_card(), 0, wx.EXPAND | wx.ALL, 10)

        # Mastery overview
        content_sizer.Add(self._build_mastery_card(), 0, wx.EXPAND | wx.ALL, 10)

        # Vocab stats
        content_sizer.Add(self._build_vocab_card(), 0, wx.EXPAND | wx.ALL, 10)

        self.content.SetSizer(content_sizer)
        main_sizer.Add(self.content, 1, wx.EXPAND)

        self.SetSizer(main_sizer)

    def _make_card(self, title_text):
        """Build a titled card panel and return (panel, content_sizer)."""
        card = wx.Panel(self.content)
        # Subtle dark background to look like a card
        card.SetBackgroundColour(wx.Colour(40, 40, 40))
        outer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(card, label=title_text)
        title.SetFont(wx.Font(ui_scale.large(), wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                              wx.FONTWEIGHT_BOLD))
        title.SetForegroundColour(wx.Colour(220, 220, 220))
        outer.Add(title, 0, wx.LEFT | wx.TOP, 12)

        outer.Add(wx.StaticLine(card), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        content = wx.BoxSizer(wx.VERTICAL)
        outer.Add(content, 1, wx.EXPAND | wx.ALL, 10)

        card.SetSizer(outer)
        return card, content

    def _build_forecast_card(self):
        card, content = self._make_card("📊 Score Forecast")
        self.forecast_text = wx.StaticText(card, label="Loading...")
        self.forecast_text.SetForegroundColour(wx.Colour(220, 220, 220))
        self.forecast_text.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                           wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        content.Add(self.forecast_text, 0, wx.EXPAND | wx.ALL, 8)
        return card

    def _build_today_card(self):
        card, content = self._make_card("✓ Today's Plan")
        self.today_list = wx.StaticText(card, label="No active study plan")
        self.today_list.SetForegroundColour(wx.Colour(220, 220, 220))
        self.today_list.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                        wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        content.Add(self.today_list, 0, wx.EXPAND | wx.ALL, 8)

        self.create_plan_btn = wx.Button(card, label="Create Study Plan",
                                         size=(-1, ui_scale.font_size(36)))
        self.create_plan_btn.Bind(wx.EVT_BUTTON, self._on_create_plan_clicked)
        content.Add(self.create_plan_btn, 0, wx.ALIGN_LEFT | wx.TOP, 8)
        return card

    def _build_actions_card(self):
        card, content = self._make_card("🚀 Quick Actions")
        grid = wx.GridSizer(rows=2, cols=4, hgap=10, vgap=10)

        actions = [
            ("📋 Diagnostic Test", self._on_diag_clicked),
            ("📝 Practice Drill", self._on_drill_clicked),
            ("📖 Browse Lessons", self._on_lesson_clicked),
            ("🔤 Vocab Flashcards", self._on_vocab_clicked),
            ("📊 Practice Test", self._on_practice_test_clicked),
            ("🎯 Full Mock Exam", self._on_full_mock_clicked),
            ("📈 View Progress", self._on_progress_clicked),
        ]
        btn_height = ui_scale.font_size(48)
        for label, handler in actions:
            btn = wx.Button(card, label=label, size=(-1, btn_height))
            btn.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            btn.Bind(wx.EVT_BUTTON, handler)
            grid.Add(btn, 0, wx.EXPAND)

        # Fill remaining grid slot for layout balance
        grid.AddSpacer(0)

        content.Add(grid, 0, wx.EXPAND | wx.ALL, 4)
        return card

    def _build_mastery_card(self):
        card, content = self._make_card("🎯 Mastery — Top Weakest Subtopics")
        self.mastery_text = wx.StaticText(card, label="No data yet")
        self.mastery_text.SetForegroundColour(wx.Colour(220, 220, 220))
        self.mastery_text.SetFont(wx.Font(ui_scale.small(), wx.FONTFAMILY_TELETYPE,
                                          wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        content.Add(self.mastery_text, 0, wx.EXPAND | wx.ALL, 8)
        return card

    def _build_vocab_card(self):
        card, content = self._make_card("🔤 Vocabulary Progress")
        self.vocab_text = wx.StaticText(card, label="Loading...")
        self.vocab_text.SetForegroundColour(wx.Colour(220, 220, 220))
        self.vocab_text.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                        wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        content.Add(self.vocab_text, 0, wx.EXPAND | wx.ALL, 8)
        return card

    # ── Refresh ──────────────────────────────────────────────────────

    def refresh(self):
        """Refresh all dashboard data from the database."""
        # Forecast
        try:
            f = overall_forecast()
            self.forecast_text.SetLabel(
                f"Verbal: {f['verbal_low']}–{f['verbal_high']}\n"
                f"Quant:  {f['quant_low']}–{f['quant_high']}\n"
                f"Total:  {f['total_low']}–{f['total_high']}\n\n"
                f"(forecast updates with each session)"
            )
        except Exception:
            self.forecast_text.SetLabel("Forecast unavailable")

        # Today's plan
        try:
            tasks = get_today_tasks()
            if tasks:
                self.today_list.SetLabel("\n".join(f"  • {t}" for t in tasks))
                self.create_plan_btn.SetLabel("Update Study Plan")
            else:
                plan = get_active_plan()
                if plan:
                    self.today_list.SetLabel("No tasks scheduled for today.\nCheck the Study Plan screen for the week ahead.")
                else:
                    self.today_list.SetLabel("No active study plan.\nCreate one to see daily tasks.")
                    self.create_plan_btn.SetLabel("Create Study Plan")
        except Exception as e:
            self.today_list.SetLabel(f"Plan unavailable: {e}")

        # Mastery
        try:
            weak = weakness_ranking(limit=5)
            if weak:
                lines = [f"  {sub:35s} {int(score*100):3d}%   ({attempts} attempts)"
                         for sub, score, attempts in weak]
                self.mastery_text.SetLabel("\n".join(lines))
            else:
                self.mastery_text.SetLabel("Take a diagnostic or complete drills to see mastery scores.")
        except Exception:
            self.mastery_text.SetLabel("Mastery data unavailable")

        # Vocab stats
        try:
            v = vocab_stats()
            self.vocab_text.SetLabel(
                f"Total words in bank: {v['total_words']}\n"
                f"Studied: {v['reviewed']}\n"
                f"Mastered (interval ≥30d): {v['mastered']}\n"
                f"Due for review today: {v['due_today']}\n"
                f"Remaining to learn: {v['remaining_to_learn']}"
            )
        except Exception as e:
            self.vocab_text.SetLabel(f"Vocab module not initialized: {e}")

        self.Layout()
        self.content.FitInside()

    # ── Callbacks setters ────────────────────────────────────────────

    def set_handlers(self, **kwargs):
        for k, v in kwargs.items():
            attr = f"_on_{k}"
            if hasattr(self, attr):
                setattr(self, attr, v)

    # ── Event handlers ───────────────────────────────────────────────

    def _on_diag_clicked(self, _):
        if self._on_take_diagnostic:
            self._on_take_diagnostic()

    def _on_drill_clicked(self, _):
        if self._on_start_drill:
            self._on_start_drill()

    def _on_lesson_clicked(self, _):
        if self._on_start_lesson:
            self._on_start_lesson()

    def _on_vocab_clicked(self, _):
        if self._on_start_vocab:
            self._on_start_vocab()

    def _on_practice_test_clicked(self, _):
        if self._on_start_practice_test:
            self._on_start_practice_test()

    def _on_full_mock_clicked(self, _):
        if self._on_start_full_mock:
            self._on_start_full_mock()

    def _on_progress_clicked(self, _):
        # Keep using existing progress screen
        if hasattr(self, "_on_progress") and self._on_progress:
            self._on_progress()

    def _handle_settings(self, _=None):
        """Handler for both header and quick-action settings buttons."""
        if self._on_settings:
            self._on_settings()
        else:
            # Fallback: show a friendly message
            wx.MessageBox(
                "Settings not yet available.",
                "Settings",
                wx.OK | wx.ICON_INFORMATION,
                self,
            )

    def _on_create_plan_clicked(self, _):
        # Show plan creation dialog
        dlg = StudyPlanDialog(self)
        if dlg.ShowModal() == wx.ID_OK:
            params = dlg.get_params()
            try:
                from services.study_plan import generate_plan
                from datetime import datetime, timedelta
                test_date = datetime.now() + timedelta(weeks=params["weeks"])
                generate_plan(
                    target_score=params["target_score"],
                    test_date=test_date,
                    hours_per_week=params["hours_per_week"],
                    diagnostic=get_latest_diagnostic(),
                )
                wx.MessageBox("Study plan created!", "Success",
                              wx.OK | wx.ICON_INFORMATION)
                self.refresh()
            except Exception as e:
                wx.MessageBox(f"Failed to create plan: {e}", "Error",
                              wx.OK | wx.ICON_ERROR)
        dlg.Destroy()


class StudyPlanDialog(wx.Dialog):
    """Simple dialog to capture study plan params (target, weeks, hours)."""

    def __init__(self, parent):
        super().__init__(parent, title="Create Study Plan", size=(400, 280))
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(self, label="Target combined score (260-340):"),
                  0, wx.ALL, 8)
        self.target = wx.SpinCtrl(self, min=260, max=340, initial=320)
        sizer.Add(self.target, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        sizer.Add(wx.StaticText(self, label="Weeks until test:"), 0, wx.ALL, 8)
        self.weeks = wx.SpinCtrl(self, min=1, max=52, initial=8)
        sizer.Add(self.weeks, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        sizer.Add(wx.StaticText(self, label="Hours per week:"), 0, wx.ALL, 8)
        self.hours = wx.SpinCtrl(self, min=1, max=80, initial=10)
        sizer.Add(self.hours, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(btn_sizer, 0, wx.ALL | wx.ALIGN_RIGHT, 12)

        self.SetSizer(sizer)

    def get_params(self):
        return {
            "target_score": self.target.GetValue(),
            "weeks": self.weeks.GetValue(),
            "hours_per_week": self.hours.GetValue(),
        }
