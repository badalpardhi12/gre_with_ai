"""
Today screen — the new home tab.

A single primary CTA at the top, plan checklist + forecast / countdown
beneath it, recent activity at the bottom. Replaces `dashboard_screen.py`
which was a 5-card / 7-button hub with no clear next-action.

This screen is intentionally lean: data lives in services, layout is one
column, and every secondary detail (weakness ranking, mastery heatmap,
test history) lives behind one of the other 4 tabs.
"""
from datetime import datetime
from typing import Callable, Optional

import wx

from services.diagnostic import get_latest_diagnostic
from services.log import get_logger
from services.score_forecast import overall_forecast, forecast_history
from services.streak import (
    get_stats as streak_stats, today_progress, streak_label,
)
from services.study_plan import get_today_tasks, get_active_plan
from services.srs import stats as vocab_stats
from widgets import ui_scale
from widgets.card import Card
from widgets.empty_state import EmptyState
from widgets.primary_button import PrimaryButton
from widgets.range_bar import RangeBar
from widgets.secondary_button import SecondaryButton
from widgets.sparkline import Sparkline
from widgets.theme import Color

logger = get_logger("today")


class TodayScreen(wx.Panel):
    """Today's plan + glance metrics + primary CTA."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)

        # Callbacks (wired by main_frame)
        self._on_take_diagnostic: Optional[Callable] = None
        self._on_start_drill: Optional[Callable] = None
        self._on_start_vocab: Optional[Callable] = None
        self._on_start_full_mock: Optional[Callable] = None
        self._on_open_plan_dialog: Optional[Callable] = None
        self._on_browse_topics: Optional[Callable] = None
        self._on_open_tutor: Optional[Callable] = None
        self._on_open_insights: Optional[Callable] = None

        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # Scrollable content so small windows still work.
        self.content = wx.ScrolledWindow(self)
        self.content.SetBackgroundColour(Color.BG_PAGE)
        self.content.SetScrollRate(0, 14)
        col = wx.BoxSizer(wx.VERTICAL)

        col.Add(self._build_header(), 0, wx.EXPAND |
                wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(7))

        # Primary CTA.
        self.primary_cta = PrimaryButton(
            self.content, label="Loading…",
            subtitle="", height=ui_scale.space(18),
        )
        self.primary_cta.Bind(wx.EVT_BUTTON, self._on_primary_clicked)
        col.Add(self.primary_cta, 0, wx.EXPAND |
                wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(7))

        # Two-column row: plan checklist + forecast / countdown.
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self._build_plan_card(), 1, wx.EXPAND |
                wx.LEFT | wx.TOP, ui_scale.space(7))
        row.Add(self._build_forecast_card(), 1, wx.EXPAND |
                wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(7))
        col.Add(row, 0, wx.EXPAND)

        # Recent activity.
        col.Add(self._build_activity_card(), 0, wx.EXPAND |
                wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(7))

        # Bottom action row: tutor + browse.
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        self.tutor_btn = SecondaryButton(self.content, label="🤖  Ask the AI tutor")
        self.tutor_btn.SetMinSize((ui_scale.font_size(220), ui_scale.space(11)))
        self.tutor_btn.Bind(wx.EVT_BUTTON, self._on_tutor_clicked)
        row2.Add(self.tutor_btn, 0)
        row2.AddStretchSpacer(1)
        self.browse_btn = SecondaryButton(self.content, label="Browse topics")
        self.browse_btn.SetMinSize((ui_scale.font_size(180), ui_scale.space(11)))
        self.browse_btn.Bind(wx.EVT_BUTTON, self._on_browse_clicked)
        row2.Add(self.browse_btn, 0)
        col.Add(row2, 0, wx.EXPAND |
                wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, ui_scale.space(7))

        self.content.SetSizer(col)
        outer.Add(self.content, 1, wx.EXPAND)
        self.SetSizer(outer)

    def _build_header(self) -> wx.BoxSizer:
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.greeting = wx.StaticText(self.content, label=self._greeting_text())
        self.greeting.SetForegroundColour(Color.TEXT_PRIMARY)
        self.greeting.SetFont(wx.Font(
            ui_scale.text_2xl(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        sizer.Add(self.greeting, 1, wx.ALIGN_CENTER_VERTICAL)

        self.streak = wx.StaticText(self.content, label="")
        self.streak.SetForegroundColour(Color.STREAK)
        self.streak.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        sizer.Add(self.streak, 0, wx.ALIGN_CENTER_VERTICAL)

        return sizer

    def _build_plan_card(self) -> wx.Panel:
        self.plan_card = Card(self.content, title="TODAY'S PLAN")
        self.plan_body = self.plan_card.body
        self._plan_placeholder()
        return self.plan_card

    def _plan_placeholder(self):
        # Filled by `refresh()`.
        self.plan_body.Clear(True)
        msg = wx.StaticText(self.plan_card, label="Loading…")
        msg.SetForegroundColour(Color.TEXT_TERTIARY)
        self.plan_body.Add(msg, 0)

    def _build_forecast_card(self) -> wx.Panel:
        self.forecast_card = Card(self.content, title="SCORE FORECAST")
        body = self.forecast_card.body

        self.forecast_text = wx.StaticText(self.forecast_card,
                                           label="Loading…")
        self.forecast_text.SetForegroundColour(Color.TEXT_PRIMARY)
        self.forecast_text.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        body.Add(self.forecast_text, 0, wx.BOTTOM, ui_scale.space(2))

        self.range_bar = RangeBar(self.forecast_card,
                                   lo=260, hi=340,
                                   label="Combined V+Q (260–340)")
        body.Add(self.range_bar, 0, wx.EXPAND | wx.BOTTOM,
                 ui_scale.space(3))

        self.spark = Sparkline(self.forecast_card)
        body.Add(self.spark, 0, wx.EXPAND | wx.BOTTOM,
                 ui_scale.space(2))

        self.test_date_label = wx.StaticText(self.forecast_card, label="")
        self.test_date_label.SetForegroundColour(Color.TEXT_TERTIARY)
        self.test_date_label.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL,
        ))
        body.Add(self.test_date_label, 0)

        return self.forecast_card

    def _build_activity_card(self) -> wx.Panel:
        self.activity_card = Card(self.content, title="RECENT ACTIVITY")
        self.activity_body = self.activity_card.body
        self._activity_placeholder()
        return self.activity_card

    def _activity_placeholder(self):
        self.activity_body.Clear(True)
        msg = wx.StaticText(self.activity_card,
                            label="Nothing yet — start a drill above.")
        msg.SetForegroundColour(Color.TEXT_TERTIARY)
        self.activity_body.Add(msg, 0)

    @staticmethod
    def _fmt_task(task) -> str:
        """Best-effort string for a study-plan task entry.

        Plans come from the LLM as strings; defend against dicts / models /
        unexpected types so the UI never renders `<object at 0x…>`.
        """
        if isinstance(task, str):
            return task[:100]
        if isinstance(task, dict):
            for key in ("title", "name", "label", "task"):
                v = task.get(key)
                if isinstance(v, str):
                    return v[:100]
            return ", ".join(f"{k}={v}" for k, v in task.items())[:100]
        return repr(task)[:100]

    # ── refresh ──────────────────────────────────────────────────────

    def refresh(self):
        """Re-pull all data sources and re-render. Cheap; safe to call often."""
        try:
            self.greeting.SetLabel(self._greeting_text())
            self.streak.SetLabel(streak_label() or "")
        except Exception:
            logger.exception("greeting/streak refresh failed")

        # Primary CTA: pick the most useful action.
        try:
            self._refresh_primary_cta()
        except Exception:
            logger.exception("primary CTA refresh failed")

        # Today's plan.
        try:
            self._refresh_plan()
        except Exception:
            logger.exception("plan refresh failed")

        # Forecast.
        try:
            self._refresh_forecast()
        except Exception:
            logger.exception("forecast refresh failed")

        # Activity.
        try:
            self._refresh_activity()
        except Exception:
            logger.exception("activity refresh failed")

        self.content.Layout()
        self.content.FitInside()
        self.Layout()

    def _greeting_text(self) -> str:
        hour = datetime.now().hour
        if hour < 12:
            return "Good morning"
        if hour < 17:
            return "Good afternoon"
        return "Good evening"

    def _refresh_primary_cta(self):
        """Decide what the single primary action should be right now."""
        diag = get_latest_diagnostic()
        plan = get_active_plan()
        v_due = 0
        try:
            v_due = vocab_stats().get("due_today", 0)
        except Exception:
            pass

        if diag is None:
            self.primary_cta.set_label(
                "▶  Take the diagnostic",
                "30 questions · ~30 min · sets your study plan",
            )
            self._primary_action = "diagnostic"
            return

        if plan is None:
            self.primary_cta.set_label(
                "▶  Build your study plan",
                "Personalized week-by-week plan from your diagnostic",
            )
            self._primary_action = "plan"
            return

        # Plan exists — figure out today's first uncompleted item.
        tasks = get_today_tasks()
        if tasks:
            first = self._fmt_task(tasks[0])
            self.primary_cta.set_label(
                f"▶  {first}",
                f"Part 1 of {len(tasks)} on today's plan",
            )
            self._primary_action = "plan_task"
            return

        if v_due > 0:
            self.primary_cta.set_label(
                f"▶  Review {v_due} due vocabulary card{'s' if v_due != 1 else ''}",
                "Daily SRS session · ~5–10 min",
            )
            self._primary_action = "vocab"
            return

        self.primary_cta.set_label(
            "▶  Quick drill",
            "10 smart-picked questions from your weakest topic",
        )
        self._primary_action = "drill"

    def _refresh_plan(self):
        self.plan_body.Clear(True)

        plan = get_active_plan()
        tasks = get_today_tasks()

        if not plan:
            empty = wx.StaticText(self.plan_card,
                                  label="No active plan yet.\n"
                                        "Take the diagnostic to build one.")
            empty.SetForegroundColour(Color.TEXT_SECONDARY)
            empty.SetFont(wx.Font(
                ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            self.plan_body.Add(empty, 0, wx.BOTTOM, ui_scale.space(3))

            cta = SecondaryButton(self.plan_card, label="Build study plan →")
            cta.SetMinSize((ui_scale.font_size(220), ui_scale.space(10)))
            cta.Bind(wx.EVT_BUTTON,
                     lambda _: (self._on_open_plan_dialog and
                                self._on_open_plan_dialog()))
            self.plan_body.Add(cta, 0)
            return

        if not tasks:
            note = wx.StaticText(
                self.plan_card,
                label=("No items scheduled for today. Take the day off — "
                       "or hit 'Quick drill' above."))
            note.SetForegroundColour(Color.TEXT_SECONDARY)
            note.SetFont(wx.Font(
                ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            note.Wrap(ui_scale.font_size(360))
            self.plan_body.Add(note, 0)
            return

        for i, task in enumerate(tasks[:5]):
            task_text = self._fmt_task(task)
            row = wx.BoxSizer(wx.HORIZONTAL)
            checkbox_label = wx.StaticText(self.plan_card, label="◯ ")
            checkbox_label.SetForegroundColour(Color.TEXT_TERTIARY)
            checkbox_label.SetFont(wx.Font(
                ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            row.Add(checkbox_label, 0)
            text = wx.StaticText(self.plan_card, label=task_text)
            text.SetForegroundColour(Color.TEXT_PRIMARY)
            text.SetFont(wx.Font(
                ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            text.Wrap(ui_scale.font_size(360))
            row.Add(text, 1)
            self.plan_body.Add(row, 0, wx.BOTTOM, ui_scale.space(2))

        # Daily-goal progress.
        try:
            tp = today_progress()
            done, goal = tp["minutes_done"], tp["minutes_goal"]
            pct = int(round(tp["fraction"] * 100))
            note = wx.StaticText(
                self.plan_card,
                label=f"Today's goal: {done} / {goal} min  ({pct}%)")
            note.SetForegroundColour(Color.TEXT_TERTIARY)
            note.SetFont(wx.Font(
                ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL,
            ))
            self.plan_body.Add(note, 0, wx.TOP, ui_scale.space(3))
        except Exception:
            pass

    def _refresh_forecast(self):
        f = overall_forecast()
        v_lo, v_hi = f["verbal_low"], f["verbal_high"]
        q_lo, q_hi = f["quant_low"], f["quant_high"]
        t_lo, t_hi = f["total_low"], f["total_high"]

        if t_lo is None or t_hi is None:
            self.forecast_text.SetLabel(
                "Take a few drills to unlock your score forecast.")
            self.range_bar.update(None, None, label="No forecast yet")
            self.spark.set_values([])
        else:
            self.forecast_text.SetLabel(
                f"Verbal {v_lo}–{v_hi}    ·    Quant {q_lo}–{q_hi}")
            self.range_bar.update(t_lo, t_hi,
                                  label="Combined V+Q (260–340)")
            self.spark.set_values(forecast_history(n=10))

        # Test-date countdown
        plan = get_active_plan()
        if plan and plan.test_date:
            days = (plan.test_date - datetime.now()).days
            if days > 0:
                self.test_date_label.SetLabel(
                    f"Test date: {plan.test_date.strftime('%b %d, %Y')} · "
                    f"{days} days away")
            elif days == 0:
                self.test_date_label.SetLabel("Test day — good luck!")
            else:
                self.test_date_label.SetLabel(
                    f"Test was {-days} days ago — update your plan if you "
                    "have a new date.")
        else:
            self.test_date_label.SetLabel("Test date not set.")

    def _refresh_activity(self):
        from models.database import Session as DBSession, ScoringResult
        self.activity_body.Clear(True)

        rows = (DBSession
                .select()
                .where(DBSession.state.in_(("completed", "abandoned")))
                .order_by(DBSession.created_at.desc())
                .limit(3))
        rows = list(rows)
        if not rows:
            self._activity_placeholder()
            return

        for sess in rows:
            label = sess.created_at.strftime("%a %b %d") if sess.created_at else "—"
            kind = sess.test_type.replace("_", " ")
            extra = ""
            try:
                sc = ScoringResult.get_or_none(ScoringResult.session == sess.id)
                if sc and sc.verbal_estimated_low is not None:
                    extra = (f" · V {sc.verbal_estimated_low}–"
                             f"{sc.verbal_estimated_high}, "
                             f"Q {sc.quant_estimated_low}–"
                             f"{sc.quant_estimated_high}")
            except Exception:
                pass
            line = wx.StaticText(self.activity_card,
                                 label=f"• {label} — {kind}{extra}")
            line.SetForegroundColour(Color.TEXT_SECONDARY)
            line.SetFont(wx.Font(
                ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            self.activity_body.Add(line, 0, wx.BOTTOM, ui_scale.space(1))

    # ── handlers (set_handlers + click routing) ───────────────────────

    def set_handlers(self, **kwargs):
        for k, v in kwargs.items():
            attr = f"_on_{k}"
            if hasattr(self, attr):
                setattr(self, attr, v)

    def _on_primary_clicked(self, _):
        action = getattr(self, "_primary_action", "drill")
        if action == "diagnostic" and self._on_take_diagnostic:
            self._on_take_diagnostic()
        elif action == "plan" and self._on_open_plan_dialog:
            self._on_open_plan_dialog()
        elif action == "vocab" and self._on_start_vocab:
            self._on_start_vocab()
        elif action == "plan_task":
            # Plan tasks are free-form text right now — route to Learn so the
            # user can pick the relevant topic. PRs after this can deep-link.
            if self._on_browse_topics:
                self._on_browse_topics()
        elif self._on_start_drill:
            self._on_start_drill()

    def _on_tutor_clicked(self, _):
        if self._on_open_tutor:
            self._on_open_tutor()

    def _on_browse_clicked(self, _):
        if self._on_browse_topics:
            self._on_browse_topics()
