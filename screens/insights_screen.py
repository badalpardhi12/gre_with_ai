"""
Insights screen — deep-dive analytics. Replaces ProgressScreen.

Layout (top to bottom):
- Score forecast card with range bar + 10-point sparkline.
- Per-measure mastery roll-up (Quant / Verbal / AWA).
- Active study plan card with "Update plan" CTA.
- Mistake-coach card with "Run coach now" button (the trigger that was
  previously buried behind the every-50-mistakes counter).
- Test history list (the old ProgressScreen table, restyled).
"""
import json
from datetime import datetime
from typing import Callable, Optional

import wx

from config import load_llm_config
from models.database import (
    Session as DBSession, ScoringResult, Question, Response,
    MasteryRecord, StudyPlan,
)
from services.log import get_logger
from services.score_forecast import overall_forecast, forecast_history
from services.study_plan import get_active_plan
from widgets import ui_scale
from widgets.card import Card
from widgets.primary_button import PrimaryButton
from widgets.range_bar import RangeBar
from widgets.secondary_button import SecondaryButton
from widgets.sparkline import Sparkline
from widgets.theme import Color

logger = get_logger("insights")


class InsightsScreen(wx.Panel):
    """Forecast + mastery + plan + history + coach in a single deep-dive tab."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)
        self._on_update_plan: Optional[Callable] = None
        self._on_run_coach: Optional[Callable] = None
        self._build_ui()

    def set_handlers(self, update_plan=None, run_coach=None):
        if update_plan is not None:
            self._on_update_plan = update_plan
        if run_coach is not None:
            self._on_run_coach = run_coach

    def refresh(self):
        for fn, name in (
            (self._refresh_forecast, "forecast"),
            (self._refresh_mastery, "mastery"),
            (self._refresh_plan, "plan"),
            (self._refresh_history, "history"),
        ):
            try:
                fn()
            except Exception:
                logger.exception("%s refresh failed", name)
        self.content.Layout()
        self.content.FitInside()
        self.Layout()

    # ── layout ────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(self, label="Insights")
        title.SetForegroundColour(Color.TEXT_PRIMARY)
        title.SetFont(wx.Font(
            ui_scale.text_2xl(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        outer.Add(title, 0, wx.ALL, ui_scale.space(5))

        self.content = wx.ScrolledWindow(self)
        self.content.SetBackgroundColour(Color.BG_PAGE)
        self.content.SetScrollRate(0, 14)
        col = wx.BoxSizer(wx.VERTICAL)

        # Top row: forecast + mastery
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(self._build_forecast_card(), 1, wx.EXPAND |
                 wx.LEFT, ui_scale.space(5))
        row1.Add(self._build_mastery_card(), 1, wx.EXPAND |
                 wx.LEFT | wx.RIGHT, ui_scale.space(5))
        col.Add(row1, 0, wx.EXPAND | wx.TOP, ui_scale.space(2))

        # Plan + coach row
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(self._build_plan_card(), 1, wx.EXPAND |
                 wx.LEFT | wx.TOP, ui_scale.space(5))
        row2.Add(self._build_coach_card(), 1, wx.EXPAND |
                 wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(5))
        col.Add(row2, 0, wx.EXPAND)

        # History list
        col.Add(self._build_history_card(), 0, wx.EXPAND |
                wx.ALL, ui_scale.space(5))

        self.content.SetSizer(col)
        outer.Add(self.content, 1, wx.EXPAND)
        self.SetSizer(outer)

    def _build_forecast_card(self):
        card = Card(self.content, title="SCORE FORECAST")
        body = card.body

        self._forecast_text = wx.StaticText(card, label="Loading…")
        self._forecast_text.SetForegroundColour(Color.TEXT_PRIMARY)
        self._forecast_text.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        body.Add(self._forecast_text, 0, wx.BOTTOM, ui_scale.space(2))

        self._range_bar = RangeBar(card, lo=260, hi=340,
                                    label="Combined V+Q (260–340)")
        body.Add(self._range_bar, 0, wx.EXPAND | wx.BOTTOM,
                 ui_scale.space(3))

        self._spark = Sparkline(card)
        body.Add(self._spark, 0, wx.EXPAND)
        self._forecast_card = card
        return card

    def _build_mastery_card(self):
        card = Card(self.content, title="MASTERY OVERVIEW")
        self._mastery_body = card.body
        self._mastery_card = card
        return card

    def _build_plan_card(self):
        card = Card(self.content, title="STUDY PLAN")
        self._plan_body = card.body

        self._plan_text = wx.StaticText(card, label="Loading…")
        self._plan_text.SetForegroundColour(Color.TEXT_PRIMARY)
        self._plan_text.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        self._plan_body.Add(self._plan_text, 0, wx.BOTTOM, ui_scale.space(3))

        self._plan_btn = SecondaryButton(card, label="Update plan")
        self._plan_btn.SetMinSize((ui_scale.font_size(180), ui_scale.space(10)))
        self._plan_btn.Bind(wx.EVT_BUTTON,
                            lambda _: (self._on_update_plan and
                                       self._on_update_plan()))
        self._plan_body.Add(self._plan_btn, 0)
        self._plan_card = card
        return card

    def _build_coach_card(self):
        card = Card(self.content, title="MISTAKE-PATTERN COACH")
        self._coach_body = card.body

        self._coach_text = wx.StaticText(
            card,
            label="The coach surfaces patterns in your wrong answers. "
                  "It auto-runs every 50 mistakes — or click below.",
        )
        self._coach_text.SetForegroundColour(Color.TEXT_SECONDARY)
        self._coach_text.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        self._coach_text.Wrap(ui_scale.font_size(300))
        self._coach_body.Add(self._coach_text, 0, wx.BOTTOM, ui_scale.space(3))

        self._coach_btn = SecondaryButton(card, label="Run coach now")
        self._coach_btn.SetMinSize((ui_scale.font_size(180), ui_scale.space(10)))
        self._coach_btn.Bind(wx.EVT_BUTTON,
                             lambda _: (self._on_run_coach and
                                        self._on_run_coach()))
        self._coach_body.Add(self._coach_btn, 0)
        return card

    def _build_history_card(self):
        card = Card(self.content, title="TEST HISTORY")
        self._history_list = wx.ListCtrl(
            card, style=wx.LC_REPORT | wx.LC_HRULES,
            size=(-1, ui_scale.font_size(220)),
        )
        self._history_list.SetBackgroundColour(Color.BG_SURFACE)
        self._history_list.SetForegroundColour(Color.TEXT_PRIMARY)
        self._history_list.InsertColumn(0, "Date", width=ui_scale.font_size(140))
        self._history_list.InsertColumn(1, "Type", width=ui_scale.font_size(120))
        self._history_list.InsertColumn(2, "Verbal", width=ui_scale.font_size(100))
        self._history_list.InsertColumn(3, "Quant", width=ui_scale.font_size(100))
        self._history_list.InsertColumn(4, "AWA", width=ui_scale.font_size(60))
        self._history_list.InsertColumn(5, "Mode", width=ui_scale.font_size(100))
        card.body.Add(self._history_list, 1, wx.EXPAND)
        return card

    # ── refresh ───────────────────────────────────────────────────────

    def _refresh_forecast(self):
        f = overall_forecast()
        v_lo, v_hi = f["verbal_low"], f["verbal_high"]
        q_lo, q_hi = f["quant_low"], f["quant_high"]
        t_lo, t_hi = f["total_low"], f["total_high"]
        if t_lo is None or t_hi is None:
            self._forecast_text.SetLabel(
                "Take a few drills to unlock your forecast.")
            self._range_bar.update(None, None,
                                   label="Combined V+Q (260–340)")
            self._spark.set_values([])
        else:
            self._forecast_text.SetLabel(
                f"Verbal {v_lo}–{v_hi}    ·    Quant {q_lo}–{q_hi}")
            self._range_bar.update(t_lo, t_hi,
                                   label="Combined V+Q (260–340)")
            self._spark.set_values(forecast_history(n=10))

    def _refresh_mastery(self):
        self._mastery_body.Clear(True)

        # Group mastery by measure (verbal/quant) for the roll-up.
        from peewee import fn
        rows = (Question
                .select(Question.measure,
                        Question.subtopic)
                .where(Question.subtopic != "")
                .distinct())
        sub_to_measure = {r.subtopic: r.measure for r in rows}

        bands = {"verbal": [], "quant": [], "awa": []}
        for m in MasteryRecord.select():
            measure = sub_to_measure.get(m.subtopic)
            if measure not in bands:
                # Subtopic exists in mastery but not in the live question
                # bank — most likely a stale row from a question that was
                # retired. Skip; harmless drop.
                logger.debug("orphan mastery subtopic: %s (no questions)",
                             m.subtopic)
                continue
            bands[measure].append(m.mastery_score)

        if not any(bands.values()):
            empty = wx.StaticText(
                self._mastery_card,
                label="No mastery data yet — complete a drill or two.",
            )
            empty.SetForegroundColour(Color.TEXT_SECONDARY)
            empty.SetFont(wx.Font(
                ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            self._mastery_body.Add(empty, 0)
            return

        for measure, label in (("quant", "Quant"),
                                ("verbal", "Verbal"),
                                ("awa", "AWA")):
            scores = bands.get(measure, [])
            avg = sum(scores) / len(scores) if scores else 0.0
            self._mastery_body.Add(self._render_mastery_row(label, avg,
                                                            len(scores)),
                                   0, wx.BOTTOM, ui_scale.space(2))

    def _render_mastery_row(self, label, fraction, n) -> wx.BoxSizer:
        row = wx.BoxSizer(wx.HORIZONTAL)
        lbl = wx.StaticText(self._mastery_card, label=f"{label:8s}")
        lbl.SetForegroundColour(Color.TEXT_PRIMARY)
        lbl.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        row.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                ui_scale.space(3))

        bar = _SegmentedBar(self._mastery_card, fraction=fraction)
        bar.SetMinSize((ui_scale.font_size(180), ui_scale.space(4)))
        row.Add(bar, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                ui_scale.space(3))

        pct = wx.StaticText(self._mastery_card,
                             label=f"{int(fraction * 100):3d}%  ({n})")
        pct.SetForegroundColour(Color.TEXT_SECONDARY)
        pct.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        row.Add(pct, 0, wx.ALIGN_CENTER_VERTICAL)
        return row

    def _refresh_plan(self):
        plan = get_active_plan()
        if not plan:
            self._plan_text.SetLabel("No active study plan.")
            self._plan_btn.set_label("Create plan")
            return
        try:
            data = json.loads(plan.plan_json or "{}")
        except (ValueError, TypeError):
            data = {}
        weeks = data.get("weeks", [])
        summary = data.get("summary", "")
        days_to_test = (plan.test_date - datetime.now()).days
        msg_lines = [f"Target: {plan.target_score}",
                     f"Test in: {days_to_test} days"]
        if weeks:
            msg_lines.append(f"Weeks scheduled: {len(weeks)}")
        if summary:
            msg_lines.append(summary[:120] + ("…" if len(summary) > 120 else ""))
        self._plan_text.SetLabel("\n".join(msg_lines))
        self._plan_btn.set_label("Update plan")
        # Show / hide the coach button based on LLM key availability.
        self._sync_coach_state()

    def _sync_coach_state(self):
        try:
            has_key = bool(load_llm_config().get("api_key"))
        except Exception:
            has_key = False
        if has_key:
            self._coach_btn.Enable(True)
            self._coach_btn.set_label("Run coach now")
        else:
            self._coach_btn.Enable(False)
            self._coach_btn.set_label("Configure LLM key in Settings")

    def _refresh_history(self):
        self._history_list.DeleteAllItems()
        rows = (DBSession
                .select()
                .where(DBSession.state == "completed")
                .order_by(DBSession.created_at.desc())
                .limit(50))
        for sess in rows:
            sc = ScoringResult.get_or_none(ScoringResult.session == sess.id)
            if not sc:
                continue
            idx = self._history_list.InsertItem(
                self._history_list.GetItemCount(),
                sess.created_at.strftime("%Y-%m-%d %H:%M") if sess.created_at else "—",
            )
            self._history_list.SetItem(idx, 1, sess.test_type)
            v = (f"{sc.verbal_estimated_low}–{sc.verbal_estimated_high}"
                 if sc.verbal_estimated_low is not None else "—")
            q = (f"{sc.quant_estimated_low}–{sc.quant_estimated_high}"
                 if sc.quant_estimated_low is not None else "—")
            self._history_list.SetItem(idx, 2, v)
            self._history_list.SetItem(idx, 3, q)
            self._history_list.SetItem(idx, 4,
                                        f"{sc.awa_estimated:.1f}"
                                        if sc.awa_estimated is not None else "—")
            self._history_list.SetItem(idx, 5, sess.mode)


class _SegmentedBar(wx.Panel):
    """Tiny horizontal progress bar for the mastery roll-up."""

    def __init__(self, parent, fraction: float = 0.0):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_SURFACE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._fraction = max(0.0, min(1.0, fraction))
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        radius = ui_scale.space(1)
        gc.SetBrush(wx.Brush(Color.BG_ELEVATED))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(0, 0, w, h, radius)
        if self._fraction > 0:
            color = (Color.SUCCESS if self._fraction >= 0.8
                     else Color.WARNING if self._fraction >= 0.4
                     else Color.DANGER)
            gc.SetBrush(wx.Brush(color))
            gc.DrawRoundedRectangle(0, 0, max(2, w * self._fraction), h,
                                    radius)
