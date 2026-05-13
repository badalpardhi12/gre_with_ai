"""
Error Log screen (P2.E1) — a primary view of every wrong answer.

Each row renders:
- Prompt preview (first 120 chars)
- User's answer vs the correct answer
- Time spent
- Auto-classified error category (careless / conceptual / timing / vocab_gap)
- "Schedule Redo" (stub — hooks to FSRS in E2) + "Ask Tutor" buttons

Filters at the top let the user narrow by subtype and date range; an
aggregate stacked-bar at the bottom shows category distribution per
subtype. Reuses the Card and segmented-bar idioms from insights_screen
for visual consistency.
"""
from datetime import datetime
from typing import Callable, Optional

import wx

from services.log import get_logger
from services.mistake_coach import (
    ERROR_CATEGORIES, error_category_distribution, list_errors,
)
from widgets import ui_scale
from widgets.card import Card
from widgets.secondary_button import SecondaryButton
from widgets.theme import Color

logger = get_logger("error_log")


# Map error category → (label, colour). Colours match the semantic palette.
_CATEGORY_STYLE = {
    "careless":   ("Careless",    Color.WARNING),
    "conceptual": ("Conceptual",  Color.DANGER),
    "timing":     ("Timing",      Color.ACCENT),
    "vocab_gap":  ("Vocab gap",   Color.ACCENT_DARK),
}


# Stacked-bar segment colours, keyed by category so the legend matches bars.
_CATEGORY_FILL = {k: v[1] for k, v in _CATEGORY_STYLE.items()}


_TIME_RANGES = [
    ("Last 7 days", 7),
    ("Last 30 days", 30),
    ("All time", None),
]


class ErrorLogScreen(wx.Panel):
    """List every wrong answer with auto-classified error category."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)
        self._on_ask_tutor: Optional[Callable] = None
        self._on_schedule_redo: Optional[Callable] = None
        self._rows_cache = []
        self._build_ui()

    # ── handler wiring ────────────────────────────────────────────────

    def set_handlers(self, ask_tutor=None, schedule_redo=None):
        if ask_tutor is not None:
            self._on_ask_tutor = ask_tutor
        if schedule_redo is not None:
            self._on_schedule_redo = schedule_redo

    # ── layout ────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(self, label="Error Log")
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

        col.Add(self._build_filter_card(), 0, wx.EXPAND |
                wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(5))
        col.Add(self._build_rows_card(), 1, wx.EXPAND |
                wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(5))
        col.Add(self._build_chart_card(), 0, wx.EXPAND |
                wx.ALL, ui_scale.space(5))

        self.content.SetSizer(col)
        outer.Add(self.content, 1, wx.EXPAND)
        self.SetSizer(outer)

    def _build_filter_card(self):
        card = Card(self.content, title="FILTERS")
        body = card.body

        row = wx.BoxSizer(wx.HORIZONTAL)

        # Subtype dropdown — options rebuilt in _refresh_filters.
        sub_lbl = wx.StaticText(card, label="Subtype:")
        sub_lbl.SetForegroundColour(Color.TEXT_SECONDARY)
        row.Add(sub_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                ui_scale.space(2))
        self._subtype_choice = wx.Choice(card, choices=["All"])
        self._subtype_choice.SetSelection(0)
        self._subtype_choice.Bind(wx.EVT_CHOICE, lambda _: self.refresh(rebuild_filters=False))
        row.Add(self._subtype_choice, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                ui_scale.space(4))

        # Time range dropdown.
        tr_lbl = wx.StaticText(card, label="Range:")
        tr_lbl.SetForegroundColour(Color.TEXT_SECONDARY)
        row.Add(tr_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                ui_scale.space(2))
        self._range_choice = wx.Choice(
            card, choices=[lbl for (lbl, _) in _TIME_RANGES],
        )
        self._range_choice.SetSelection(1)  # Default: last 30 days.
        self._range_choice.Bind(wx.EVT_CHOICE, lambda _: self.refresh(rebuild_filters=False))
        row.Add(self._range_choice, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                ui_scale.space(4))

        # Summary label (row count).
        self._summary = wx.StaticText(card, label="")
        self._summary.SetForegroundColour(Color.TEXT_SECONDARY)
        self._summary.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL,
        ))
        row.Add(self._summary, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                ui_scale.space(4))

        body.Add(row, 0, wx.EXPAND)
        self._filter_card = card
        return card

    def _build_rows_card(self):
        card = Card(self.content, title="WRONG ANSWERS (NEWEST FIRST)")
        self._rows_body = card.body
        self._rows_card = card
        return card

    def _build_chart_card(self):
        card = Card(self.content, title="ERROR CATEGORIES BY SUBTYPE")
        self._chart_body = card.body
        self._chart_card = card
        return card

    # ── refresh ───────────────────────────────────────────────────────

    def refresh(self, rebuild_filters: bool = True):
        try:
            if rebuild_filters:
                self._refresh_filters()
            self._refresh_rows()
            self._refresh_chart()
        except Exception:
            logger.exception("error-log refresh failed")
        self.content.Layout()
        self.content.FitInside()
        self.Layout()

    def _selected_subtype(self) -> Optional[str]:
        if self._subtype_choice.GetCount() == 0:
            return None
        sel = self._subtype_choice.GetSelection()
        if sel <= 0:  # "All"
            return None
        return self._subtype_choice.GetString(sel)

    def _selected_since_days(self) -> Optional[int]:
        sel = self._range_choice.GetSelection()
        if sel < 0 or sel >= len(_TIME_RANGES):
            return None
        return _TIME_RANGES[sel][1]

    def _refresh_filters(self):
        """Populate the subtype dropdown from distinct subtypes in errors."""
        all_rows = list_errors(limit=5000)
        subtypes = sorted({r["subtype"] for r in all_rows if r["subtype"]})
        current = self._subtype_choice.GetStringSelection()
        self._subtype_choice.Clear()
        self._subtype_choice.Append("All")
        for st in subtypes:
            self._subtype_choice.Append(st)
        # Restore selection if still present.
        idx = self._subtype_choice.FindString(current)
        self._subtype_choice.SetSelection(idx if idx >= 0 else 0)

    def _refresh_rows(self):
        self._rows_body.Clear(True)

        rows = list_errors(
            subtype=self._selected_subtype(),
            since_days=self._selected_since_days(),
            limit=200,
        )
        self._rows_cache = rows

        if not rows:
            empty = wx.StaticText(
                self._rows_card,
                label="No errors logged yet — complete a practice section "
                      "to start tracking.",
            )
            empty.SetForegroundColour(Color.TEXT_SECONDARY)
            empty.SetFont(wx.Font(
                ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            self._rows_body.Add(empty, 0, wx.ALL, ui_scale.space(3))
            self._summary.SetLabel("")
            return

        self._summary.SetLabel(
            f"{len(rows)} wrong answer{'s' if len(rows) != 1 else ''}"
        )
        for r in rows:
            self._rows_body.Add(self._render_row(r), 0,
                                wx.EXPAND | wx.BOTTOM,
                                ui_scale.space(2))

    def _render_row(self, row: dict) -> wx.BoxSizer:
        outer = wx.BoxSizer(wx.VERTICAL)

        # Top line: subtype, category badge, timestamp.
        top = wx.BoxSizer(wx.HORIZONTAL)
        subtype_lbl = wx.StaticText(
            self._rows_card,
            label=(row.get("subtype") or "unknown").upper(),
        )
        subtype_lbl.SetForegroundColour(Color.TEXT_SECONDARY)
        subtype_lbl.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        top.Add(subtype_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                ui_scale.space(3))

        badge = _CategoryBadge(self._rows_card, category=row["category"])
        top.Add(badge, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                ui_scale.space(3))

        # Pushed right: timestamp.
        ts = row.get("created_at")
        ts_s = ts.strftime("%Y-%m-%d %H:%M") if isinstance(ts, datetime) else ""
        ts_lbl = wx.StaticText(self._rows_card, label=ts_s)
        ts_lbl.SetForegroundColour(Color.TEXT_SECONDARY)
        ts_lbl.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        top.AddStretchSpacer()
        top.Add(ts_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(top, 0, wx.EXPAND | wx.BOTTOM, ui_scale.space(1))

        # Prompt preview.
        preview = wx.StaticText(self._rows_card,
                                label=row.get("prompt_preview", ""))
        preview.SetForegroundColour(Color.TEXT_PRIMARY)
        preview.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        preview.Wrap(ui_scale.font_size(700))
        outer.Add(preview, 0, wx.EXPAND | wx.BOTTOM, ui_scale.space(1))

        # Answer / time line.
        answer_s = (f"Your answer: {row['user_answer']}    ·    "
                    f"Correct: {row['correct_answer']}    ·    "
                    f"Time: {(row['time_ms'] or 0) / 1000:.1f}s")
        ans_lbl = wx.StaticText(self._rows_card, label=answer_s)
        ans_lbl.SetForegroundColour(Color.TEXT_SECONDARY)
        ans_lbl.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        outer.Add(ans_lbl, 0, wx.EXPAND | wx.BOTTOM, ui_scale.space(1))

        # Action buttons.
        btns = wx.BoxSizer(wx.HORIZONTAL)
        redo_btn = SecondaryButton(self._rows_card, label="Schedule Redo")
        redo_btn.SetMinSize((ui_scale.font_size(140), ui_scale.space(8)))
        redo_btn.Bind(wx.EVT_BUTTON,
                      lambda _e, qid=row["qid"]: self._schedule_redo(qid))
        btns.Add(redo_btn, 0, wx.RIGHT, ui_scale.space(2))

        ask_btn = SecondaryButton(self._rows_card, label="Ask Tutor")
        ask_btn.SetMinSize((ui_scale.font_size(120), ui_scale.space(8)))
        ask_btn.Bind(wx.EVT_BUTTON,
                     lambda _e, r=row: self._ask_tutor(r))
        btns.Add(ask_btn, 0)
        outer.Add(btns, 0)

        # Thin divider.
        divider = wx.Panel(self._rows_card, size=(-1, 1))
        divider.SetBackgroundColour(Color.BORDER)
        outer.Add(divider, 0, wx.EXPAND | wx.TOP, ui_scale.space(2))

        return outer

    def _refresh_chart(self):
        self._chart_body.Clear(True)

        dist = error_category_distribution(
            since_days=self._selected_since_days())
        if not dist:
            empty = wx.StaticText(
                self._chart_card,
                label="No category data yet.",
            )
            empty.SetForegroundColour(Color.TEXT_SECONDARY)
            self._chart_body.Add(empty, 0, wx.ALL, ui_scale.space(2))
            return

        # Legend row.
        legend = wx.BoxSizer(wx.HORIZONTAL)
        for cat in ERROR_CATEGORIES:
            label, color = _CATEGORY_STYLE[cat]
            swatch = wx.Panel(self._chart_card,
                              size=(ui_scale.space(3), ui_scale.space(3)))
            swatch.SetBackgroundColour(color)
            legend.Add(swatch, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                       ui_scale.space(1))
            lbl = wx.StaticText(self._chart_card, label=label)
            lbl.SetForegroundColour(Color.TEXT_SECONDARY)
            lbl.SetFont(wx.Font(
                ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            legend.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                       ui_scale.space(3))
        self._chart_body.Add(legend, 0, wx.BOTTOM, ui_scale.space(2))

        # Find the max-total so all bars share a scale.
        max_total = max(sum(counts.values()) for counts in dist.values()) or 1

        for subtype in sorted(dist.keys()):
            counts = dist[subtype]
            row = wx.BoxSizer(wx.HORIZONTAL)

            lbl = wx.StaticText(self._chart_card,
                                label=f"{subtype[:14]:14s}")
            lbl.SetForegroundColour(Color.TEXT_PRIMARY)
            lbl.SetFont(wx.Font(
                ui_scale.text_sm(), wx.FONTFAMILY_TELETYPE,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            row.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                    ui_scale.space(3))

            bar = _StackedBar(self._chart_card, counts=counts,
                              max_total=max_total)
            bar.SetMinSize((ui_scale.font_size(220), ui_scale.space(4)))
            row.Add(bar, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                    ui_scale.space(3))

            total = sum(counts.values())
            total_lbl = wx.StaticText(self._chart_card,
                                      label=f"n={total}")
            total_lbl.SetForegroundColour(Color.TEXT_SECONDARY)
            total_lbl.SetFont(wx.Font(
                ui_scale.text_sm(), wx.FONTFAMILY_TELETYPE,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            row.Add(total_lbl, 0, wx.ALIGN_CENTER_VERTICAL)

            self._chart_body.Add(row, 0, wx.EXPAND | wx.BOTTOM,
                                 ui_scale.space(1))

    # ── row action handlers ──────────────────────────────────────────

    def _schedule_redo(self, qid: int):
        """Wire into E2 FSRS when it ships; for now, log + toast."""
        logger.info("schedule_redo intent qid=%s", qid)
        if self._on_schedule_redo:
            try:
                self._on_schedule_redo(qid)
                return
            except Exception:
                logger.exception("schedule_redo handler failed")
        # Default fallback toast.
        wx.MessageBox(
            f"Question {qid} scheduled for redo. (FSRS queue wiring "
            "arrives in P2.E2.)",
            "Scheduled",
            wx.OK | wx.ICON_INFORMATION,
            parent=self,
        )

    def _ask_tutor(self, row: dict):
        if self._on_ask_tutor:
            try:
                self._on_ask_tutor(row["qid"], row["response_id"])
                return
            except Exception:
                logger.exception("ask_tutor handler failed")
        # Default fallback: open AnswerChatDialog directly.
        try:
            from screens.answer_chat_screen import AnswerChatDialog
            from services.question_bank import QuestionBankService
            qb = QuestionBankService()
            q_data = qb.get_question(row["qid"])
            if q_data is None:
                wx.MessageBox("Question not found.", "Ask Tutor",
                              wx.OK | wx.ICON_WARNING, parent=self)
                return
            dlg = AnswerChatDialog(self, q_data,
                                   user_response={"answer": row["user_answer"]})
            dlg.ShowModal()
            dlg.Destroy()
        except Exception:
            logger.exception("fallback ask_tutor failed")


# ── widgets ──────────────────────────────────────────────────────────


class _CategoryBadge(wx.Panel):
    """Small coloured pill labelled with the category name."""

    def __init__(self, parent, category: str):
        super().__init__(parent)
        label, color = _CATEGORY_STYLE.get(
            category, ("Unknown", Color.TEXT_SECONDARY))
        self._label = label
        self._color = color
        self.SetBackgroundColour(parent.GetBackgroundColour())
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        # Size from the label's rendered extent.
        dc = wx.ClientDC(self)
        dc.SetFont(wx.Font(ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
                           wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        tw, th = dc.GetTextExtent(self._label)
        pad_x = ui_scale.space(2)
        pad_y = ui_scale.space(1)
        self.SetMinSize((tw + 2 * pad_x, th + 2 * pad_y))

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        radius = ui_scale.space(1)
        gc.SetBrush(wx.Brush(self._color))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(0, 0, w, h, radius)
        gc.SetFont(
            wx.Font(ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD),
            Color.TEXT_INVERSE if hasattr(Color, "TEXT_INVERSE")
            else wx.Colour(255, 255, 255),
        )
        tw, th = gc.GetTextExtent(self._label)
        gc.DrawText(self._label, (w - tw) / 2, (h - th) / 2)


class _StackedBar(wx.Panel):
    """Horizontal stacked bar — segments per category, summing to total."""

    def __init__(self, parent, counts: dict, max_total: int):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_SURFACE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._counts = dict(counts)
        self._max_total = max_total or 1
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        radius = ui_scale.space(1)
        gc.SetBrush(wx.Brush(Color.BG_ELEVATED))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(0, 0, w, h, radius)

        total = sum(self._counts.values())
        if total == 0 or self._max_total <= 0:
            return

        bar_w = max(2, int(w * (total / self._max_total)))
        x = 0
        for cat in ERROR_CATEGORIES:
            c = self._counts.get(cat, 0)
            if c <= 0:
                continue
            seg = int(bar_w * (c / total))
            if seg <= 0:
                continue
            gc.SetBrush(wx.Brush(_CATEGORY_FILL[cat]))
            gc.DrawRectangle(x, 0, seg, h)
            x += seg
