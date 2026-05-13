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
from services.mastery import heatmap_data
from services.score_forecast import overall_forecast, forecast_history
from services.study_plan import get_active_plan
from services.timing_analytics import per_subtype_p50_p90, outliers
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
            (self._refresh_timing, "timing"),
            (self._refresh_freshness, "freshness"),
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

        # Timing panel (P2.E3): per-subtype P50/P90 bars + outlier count.
        col.Add(self._build_timing_card(), 0, wx.EXPAND |
                wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(5))

        # Subtopic strength × freshness grid (P3.S4).
        col.Add(self._build_freshness_card(), 0, wx.EXPAND |
                wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(5))

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

    def _build_timing_card(self):
        card = Card(self.content, title="TIMING (LAST 30 DAYS)")
        self._timing_body = card.body
        self._timing_card = card

        self._timing_summary = wx.StaticText(card, label="Loading…")
        self._timing_summary.SetForegroundColour(Color.TEXT_SECONDARY)
        self._timing_summary.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        self._timing_body.Add(self._timing_summary, 0, wx.BOTTOM,
                              ui_scale.space(2))
        return card

    def _build_freshness_card(self):
        card = Card(self.content, title="SUBTOPIC STRENGTH & FRESHNESS")
        self._freshness_body = card.body
        self._freshness_card = card

        self._freshness_summary = wx.StaticText(card, label="Loading…")
        self._freshness_summary.SetForegroundColour(Color.TEXT_SECONDARY)
        self._freshness_summary.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        self._freshness_body.Add(self._freshness_summary, 0, wx.BOTTOM,
                                 ui_scale.space(2))

        self._freshness_grid = _FreshnessGrid(card)
        self._freshness_body.Add(self._freshness_grid, 1, wx.EXPAND)
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

    def _refresh_timing(self):
        # Clear previous rows (keep the summary label at index 0).
        self._timing_body.Clear(True)
        self._timing_summary = wx.StaticText(self._timing_card, label="")
        self._timing_summary.SetForegroundColour(Color.TEXT_SECONDARY)
        self._timing_summary.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        self._timing_body.Add(self._timing_summary, 0, wx.BOTTOM,
                              ui_scale.space(2))

        try:
            stats = per_subtype_p50_p90(days=30)
            outlier_count = len(outliers(days=30))
        except Exception:
            logger.exception("timing analytics query failed")
            stats, outlier_count = {}, 0

        if not stats:
            self._timing_summary.SetLabel(
                "No timing data yet — answer a few questions to unlock "
                "per-subtype pacing.")
            return

        total_n = sum(s["n"] for s in stats.values())
        self._timing_summary.SetLabel(
            f"{total_n} responses across {len(stats)} subtypes  ·  "
            f"{outlier_count} outlier{'s' if outlier_count != 1 else ''} "
            "(>=2 SDs over mean)"
        )

        # Determine max P90 for bar scaling so every subtype shares a scale.
        max_ms = max(s["p90"] for s in stats.values()) or 1
        for subtype in sorted(stats.keys()):
            s = stats[subtype]
            self._timing_body.Add(
                self._render_timing_row(subtype, s, max_ms),
                0, wx.BOTTOM, ui_scale.space(2),
            )

    def _render_timing_row(self, subtype, stats, max_ms) -> wx.BoxSizer:
        row = wx.BoxSizer(wx.HORIZONTAL)

        lbl = wx.StaticText(self._timing_card,
                            label=f"{subtype[:14]:14s}")
        lbl.SetForegroundColour(Color.TEXT_PRIMARY)
        lbl.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        row.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                ui_scale.space(3))

        bar = _SegmentedBar(self._timing_card,
                            fraction=stats["p90"] / max_ms)
        bar.SetMinSize((ui_scale.font_size(180), ui_scale.space(4)))
        row.Add(bar, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                ui_scale.space(3))

        def _fmt(ms):
            return f"{ms / 1000:.1f}s"

        stat_lbl = wx.StaticText(
            self._timing_card,
            label=f"P50 {_fmt(stats['p50'])}  P90 {_fmt(stats['p90'])}  "
                  f"(n={stats['n']})",
        )
        stat_lbl.SetForegroundColour(Color.TEXT_SECONDARY)
        stat_lbl.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        row.Add(stat_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
        return row

    def _refresh_freshness(self):
        rows = heatmap_data()
        if not rows:
            self._freshness_summary.SetLabel(
                "No subtopics found yet — seed the question bank or "
                "complete a drill to populate the heatmap.")
            self._freshness_grid.set_data([])
            return

        seen = [r for r in rows if r["days_since_seen"] is not None]
        stale = [r for r in seen if (r["days_since_seen"] or 0) > 14]
        weak_and_forgotten = [
            r for r in seen
            if r["mastery_decayed"] < 0.3 and (r["days_since_seen"] or 0) > 7
        ]
        self._freshness_summary.SetLabel(
            f"{len(seen)} subtopic{'s' if len(seen) != 1 else ''} attempted  ·  "
            f"{len(stale)} stale (>14d)  ·  "
            f"{len(weak_and_forgotten)} weak + forgotten  ·  "
            "row colour = mastery band, saturation = freshness"
        )
        self._freshness_grid.set_data(rows)

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


# ── Subtopic × freshness heatmap (P3.S4) ──────────────────────────────

# Freshness bins (days-since-last-seen, right-exclusive upper bounds).
_FRESHNESS_BINS = [
    ("<7d", 0, 7),
    ("7–14d", 7, 14),
    ("14–30d", 14, 30),
    (">30d", 30, float("inf")),
]


def _freshness_bin_index(days):
    """Return 0..3 for the bin that ``days`` falls into. Unseen → None."""
    if days is None:
        return None
    for i, (_label, lo, hi) in enumerate(_FRESHNESS_BINS):
        if lo <= days < hi:
            return i
    return len(_FRESHNESS_BINS) - 1


def _mastery_band_color(m: float) -> wx.Colour:
    """Red < 0.3, yellow 0.3–0.7, green > 0.7."""
    if m < 0.3:
        return Color.DANGER
    if m < 0.7:
        return Color.WARNING
    return Color.SUCCESS


def _blend(fg: wx.Colour, bg: wx.Colour, saturation: float) -> wx.Colour:
    """Linear blend between ``bg`` (saturation=0) and ``fg`` (saturation=1)."""
    s = max(0.0, min(1.0, saturation))
    return wx.Colour(
        int(bg.Red()   + (fg.Red()   - bg.Red())   * s),
        int(bg.Green() + (fg.Green() - bg.Green()) * s),
        int(bg.Blue()  + (fg.Blue()  - bg.Blue())  * s),
    )


class _FreshnessGrid(wx.Panel):
    """Custom-painted grid: rows = subtopics, cols = freshness bins.

    Cell fill uses the mastery-band colour, blended toward BG_SURFACE as
    the subtopic goes stale (fresher = more saturated). Hover / click
    reveals a tooltip with the exact numbers.
    """

    ROW_H = 22
    HEADER_H = 22
    LABEL_W = 220
    CELL_W = 90
    PADDING = 6
    MAX_ROWS = 40   # scrolling handled by the parent ScrolledWindow

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_SURFACE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._rows: list = []
        self._cells: list = []   # list of (wx.Rect, row_dict, bin_index)
        self._hover_idx = None
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_LEFT_UP, self._on_click)

    def set_data(self, rows):
        # heatmap_data already sorts by decayed ascending; cap to MAX_ROWS
        # so the card stays scannable. The rest is still reachable via
        # study-plan priority.
        self._rows = list(rows or [])[: self.MAX_ROWS]
        n = len(self._rows)
        row_h = ui_scale.font_size(self.ROW_H)
        header_h = ui_scale.font_size(self.HEADER_H)
        pad = ui_scale.space(self.PADDING // 2 or 1)
        height = header_h + n * row_h + 2 * pad
        self.SetMinSize((-1, max(ui_scale.font_size(60), height)))
        if self.GetParent():
            self.GetParent().Layout()
        self.Refresh()

    # ── painting ──────────────────────────────────────────────────────

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(Color.BG_SURFACE))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)

        self._cells = []

        w, _ = self.GetClientSize()
        pad = ui_scale.space(1)
        label_w = ui_scale.font_size(self.LABEL_W)
        n_bins = len(_FRESHNESS_BINS)
        # Shrink cell_w if the card is narrower than the nominal layout.
        available = max(ui_scale.font_size(120), w - label_w - 2 * pad)
        cell_w = max(ui_scale.font_size(40), available // n_bins)
        row_h = ui_scale.font_size(self.ROW_H)
        header_h = ui_scale.font_size(self.HEADER_H)

        header_font = wx.Font(
            ui_scale.text_xs(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        )
        sub_font = wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        )
        meta_font = wx.Font(
            ui_scale.text_xs(), wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        )

        # Column headers
        gc.SetFont(header_font, Color.TEXT_SECONDARY)
        gc.DrawText("subtopic", pad, pad)
        for i, (label, _lo, _hi) in enumerate(_FRESHNESS_BINS):
            x = pad + label_w + i * cell_w
            tw, _ = gc.GetTextExtent(label)
            gc.DrawText(label, x + (cell_w - tw) // 2, pad)

        if not self._rows:
            gc.SetFont(sub_font, Color.TEXT_TERTIARY)
            gc.DrawText(
                "No subtopic data yet.", pad, pad + header_h,
            )
            return

        # Rows
        for row_i, row in enumerate(self._rows):
            y = pad + header_h + row_i * row_h

            # Row label (subtopic)
            sub = row["subtopic"]
            # Truncate to the label column width in chars.
            gc.SetFont(sub_font, Color.TEXT_PRIMARY)
            label_text = sub if len(sub) <= 26 else sub[:25] + "…"
            gc.DrawText(label_text, pad, y + ui_scale.space(1))

            bin_idx = _freshness_bin_index(row["days_since_seen"])
            m = row["mastery_decayed"]
            raw = row["mastery_raw"]
            n_resp = row["n_responses"]

            band_color = _mastery_band_color(m)
            # Saturation: 1.0 at 0 days, 0.15 floor at >=30d so stale cells
            # stay visible but muted. Unseen → flat elevated grey.
            days = row["days_since_seen"]
            if days is None:
                saturation = 0.0
            else:
                saturation = max(0.15, 1.0 - min(1.0, days / 30.0))

            for c_i, (_label, _lo, _hi) in enumerate(_FRESHNESS_BINS):
                cell_x = pad + label_w + c_i * cell_w
                rect = wx.Rect(
                    cell_x + 1, y + 1, cell_w - 2, row_h - 2,
                )
                # Empty cells (outside this row's bin) use a flat neutral
                # tile so the grid's layout is still visible.
                if bin_idx is None or c_i != bin_idx:
                    gc.SetBrush(wx.Brush(Color.BG_ELEVATED))
                    gc.SetPen(wx.TRANSPARENT_PEN)
                    gc.DrawRectangle(rect.x, rect.y, rect.width, rect.height)
                else:
                    fill = _blend(band_color, Color.BG_ELEVATED, saturation)
                    gc.SetBrush(wx.Brush(fill))
                    gc.SetPen(wx.TRANSPARENT_PEN)
                    gc.DrawRectangle(rect.x, rect.y, rect.width, rect.height)
                    # Overlay numeric mastery % inside the cell.
                    gc.SetFont(meta_font, Color.TEXT_PRIMARY)
                    pct = f"{int(round(m * 100))}%"
                    tw, th = gc.GetTextExtent(pct)
                    gc.DrawText(
                        pct,
                        rect.x + (rect.width - tw) // 2,
                        rect.y + (rect.height - th) // 2,
                    )
                    self._cells.append((rect, row, c_i))

        # Hover tooltip
        if self._hover_idx is not None and 0 <= self._hover_idx < len(self._cells):
            rect, row, _ = self._cells[self._hover_idx]
            days = row["days_since_seen"]
            days_txt = "never" if days is None else f"{days:.1f}d ago"
            tip = (
                f"{row['subtopic']}  ·  raw {int(round(row['mastery_raw']*100))}%  "
                f"→ decayed {int(round(row['mastery_decayed']*100))}%  ·  "
                f"n={row['n_responses']}  ·  last seen {days_txt}"
            )
            gc.SetFont(meta_font, Color.TEXT_SECONDARY)
            tw, th = gc.GetTextExtent(tip)
            # Draw tooltip at bottom-left of the widget so it's always
            # visible even when hovering the last row.
            tip_x = pad
            tip_y = self.GetClientSize().GetHeight() - th - pad
            gc.SetBrush(wx.Brush(Color.BG_PAGE))
            gc.SetPen(wx.Pen(Color.BORDER, 1))
            gc.DrawRoundedRectangle(
                tip_x - 4, tip_y - 2, tw + 8, th + 4,
                ui_scale.space(1),
            )
            gc.SetFont(meta_font, Color.TEXT_PRIMARY)
            gc.DrawText(tip, tip_x, tip_y)

    # ── events ────────────────────────────────────────────────────────

    def _hit(self, pos):
        for i, (rect, _row, _c) in enumerate(self._cells):
            if rect.Contains(pos):
                return i
        return None

    def _on_motion(self, evt):
        idx = self._hit(evt.GetPosition())
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.Refresh()

    def _on_leave(self, _):
        if self._hover_idx is not None:
            self._hover_idx = None
            self.Refresh()

    def _on_click(self, evt):
        idx = self._hit(evt.GetPosition())
        if idx is not None:
            self._hover_idx = idx
            self.Refresh()
