"""
Learn screen — heatmap of all subtopics on the left, detail pane on the right.

Replaces both the old TopicBrowserScreen (a tree) and LessonScreen (a
separate page). Selecting a heatmap cell renders the subtopic's lesson +
practice CTA + recent-attempts sparkline in the right pane — no extra
navigation step.
"""
from typing import Callable, Optional

import wx

from models.database import Lesson, Response, Question
from models.taxonomy import subtopic_display_name
from services.log import get_logger
from services.question_bank import QuestionBankService
from widgets import ui_scale
from widgets.card import Card
from widgets.heatmap import (
    MasteryHeatmap, EVT_HEATMAP_SELECT,
    FILTER_ALL, FILTER_WEAK, FILTER_MASTERED, FILTER_NEW,
)
from widgets.math_view import MathView
from widgets.primary_button import PrimaryButton
from widgets.secondary_button import SecondaryButton
from widgets.sparkline import Sparkline
from widgets.theme import Color

logger = get_logger("learn")


class LearnScreen(wx.Panel):
    """Mastery heatmap + integrated subtopic detail."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)
        self._on_start_drill: Optional[Callable[[str], None]] = None
        self._qb = QuestionBankService()
        self._build_ui()

    # ── public API ────────────────────────────────────────────────────

    def set_on_start_drill(self, cb: Callable[[str], None]):
        self._on_start_drill = cb

    def refresh(self):
        try:
            data = self._qb.subtopic_summary()
            self.heatmap.set_data(data)
            # Auto-select the first weak subtopic so the detail pane isn't
            # empty on first open.
            current = self.heatmap.selected
            if current is None:
                weak = sorted(
                    [(s, d) for s, d in data.items()
                     if d.get("attempts", 0) > 0 and (d.get("mastery") or 0) < 0.6],
                    key=lambda kv: kv[1].get("mastery") or 0,
                )
                if weak:
                    self._select(weak[0][0])
                elif data:
                    self._select(next(iter(data)))
        except Exception:
            logger.exception("learn refresh failed")

    # ── layout ────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # Page title — left-aligned, generous top padding (Apple HIG).
        title = wx.StaticText(self, label="Learn")
        title.SetForegroundColour(Color.TEXT_PRIMARY)
        title.SetFont(wx.Font(
            ui_scale.text_2xl(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        outer.Add(title, 0, wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(6))

        subtitle = wx.StaticText(
            self,
            label="Mastery across every subtopic. Pick a tile to see the "
                  "lesson and start a focused drill.",
        )
        subtitle.SetForegroundColour(Color.TEXT_SECONDARY)
        subtitle.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        outer.Add(subtitle, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, ui_scale.space(6))

        # Body: heatmap card (left, 7/12) + detail card (right, 5/12).
        body = wx.BoxSizer(wx.HORIZONTAL)
        body.Add(self._build_heatmap_card(), 7, wx.EXPAND |
                 wx.LEFT | wx.BOTTOM, ui_scale.space(6))
        body.Add(self._build_detail_card(), 5, wx.EXPAND |
                 wx.LEFT | wx.RIGHT | wx.BOTTOM, ui_scale.space(6))
        outer.Add(body, 1, wx.EXPAND)

        self.SetSizer(outer)

    def _build_heatmap_card(self) -> wx.Panel:
        card = Card(self, title=None, padding=ui_scale.space(4))

        # Filter chips row — sits inside the card it filters (Apple HIG:
        # controls live next to the content they affect).
        filter_row = wx.BoxSizer(wx.HORIZONTAL)
        filter_label = wx.StaticText(card, label="Show:")
        filter_label.SetForegroundColour(Color.TEXT_TERTIARY)
        filter_label.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        filter_row.Add(filter_label, 0, wx.ALIGN_CENTER_VERTICAL |
                       wx.RIGHT, ui_scale.space(2))

        self._filter_buttons = {}
        for fid, label in (
            (FILTER_ALL, "All"),
            (FILTER_WEAK, "Weak"),
            (FILTER_MASTERED, "Mastered"),
            (FILTER_NEW, "Not started"),
        ):
            btn = SecondaryButton(card, label=label,
                                  height=ui_scale.space(8))
            # Width: let the button autosize to its text content via
            # DoGetBestClientSize. Don't call SetMinSize((w, -1)) — that
            # would clobber the height we just set.
            btn.Bind(wx.EVT_BUTTON,
                     lambda _, f=fid: self._on_filter_chip(f))
            self._filter_buttons[fid] = btn
            filter_row.Add(btn, 0, wx.LEFT, ui_scale.space(2))
        card.body.Add(filter_row, 0, wx.EXPAND | wx.BOTTOM, ui_scale.space(4))

        # Subtle divider between chips and the heatmap.
        divider = wx.Panel(card, size=(-1, 1))
        divider.SetBackgroundColour(Color.BORDER)
        card.body.Add(divider, 0, wx.EXPAND | wx.BOTTOM, ui_scale.space(3))

        self.heatmap = MasteryHeatmap(card)
        self.heatmap.Bind(EVT_HEATMAP_SELECT, self._on_heatmap_select)
        card.body.Add(self.heatmap, 1, wx.EXPAND)
        return card

    def _build_detail_card(self) -> wx.Panel:
        card = Card(self, title=None, padding=ui_scale.space(5))
        body = card.body

        # Header row: subtopic title + mastery badge on the right.
        header = wx.BoxSizer(wx.HORIZONTAL)
        self._detail_title = wx.StaticText(card, label="Pick a topic")
        self._detail_title.SetForegroundColour(Color.TEXT_PRIMARY)
        self._detail_title.SetFont(wx.Font(
            ui_scale.text_xl(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        header.Add(self._detail_title, 1, wx.ALIGN_CENTER_VERTICAL)

        self._detail_badge = wx.StaticText(card, label="")
        self._detail_badge.SetForegroundColour(Color.TEXT_TERTIARY)
        self._detail_badge.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        header.Add(self._detail_badge, 0, wx.ALIGN_CENTER_VERTICAL |
                   wx.LEFT, ui_scale.space(2))
        body.Add(header, 0, wx.EXPAND | wx.BOTTOM, ui_scale.space(1))

        # Meta line: attempts, questions in bank.
        self._detail_meta = wx.StaticText(card, label="Select a tile on the left.")
        self._detail_meta.SetForegroundColour(Color.TEXT_SECONDARY)
        self._detail_meta.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        body.Add(self._detail_meta, 0, wx.BOTTOM, ui_scale.space(3))

        # Recent attempts sparkline (subtle — labeled below).
        spark_label = wx.StaticText(card, label="LAST 10 ATTEMPTS")
        spark_label.SetForegroundColour(Color.TEXT_TERTIARY)
        spark_label.SetFont(wx.Font(
            ui_scale.text_xs(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        body.Add(spark_label, 0, wx.BOTTOM, ui_scale.space(1))
        self._detail_history = Sparkline(card)
        self._detail_history.SetMinSize((-1, ui_scale.space(7)))
        body.Add(self._detail_history, 0, wx.EXPAND | wx.BOTTOM,
                 ui_scale.space(4))

        # Section label for the lesson body.
        lesson_label = wx.StaticText(card, label="LESSON")
        lesson_label.SetForegroundColour(Color.TEXT_TERTIARY)
        lesson_label.SetFont(wx.Font(
            ui_scale.text_xs(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        body.Add(lesson_label, 0, wx.BOTTOM, ui_scale.space(1))

        self._detail_lesson = MathView(card,
                                       size=(-1, ui_scale.font_size(280)))
        body.Add(self._detail_lesson, 1, wx.EXPAND | wx.BOTTOM,
                 ui_scale.space(4))

        # Action row — primary CTA fills the row.
        self._practice_btn = PrimaryButton(
            card,
            label="▶  Practice 10 questions",
            subtitle="Smart-picks from this subtopic",
            height=ui_scale.space(13),
        )
        self._practice_btn.Bind(wx.EVT_BUTTON, self._on_practice_clicked)
        body.Add(self._practice_btn, 0, wx.EXPAND)
        return card

    # ── events ────────────────────────────────────────────────────────

    def _on_filter_chip(self, fid: str):
        self.heatmap.set_filter(fid)

    def _on_heatmap_select(self, evt):
        self._select(evt.GetString())

    def _on_practice_clicked(self, _):
        sub = self.heatmap.selected
        if sub and self._on_start_drill:
            self._on_start_drill(sub)

    # ── detail population ─────────────────────────────────────────────

    def _select(self, sub_id: str):
        self.heatmap.set_selected(sub_id)
        info = self._qb.subtopic_summary().get(sub_id, {})

        # Title (left) — use the taxonomy display name.
        display = subtopic_display_name(sub_id)
        self._detail_title.SetLabel(display)

        # Mastery badge (right) — short and glanceable.
        attempts = info.get("attempts", 0)
        if attempts:
            pct = int((info.get("mastery") or 0) * 100)
            self._detail_badge.SetLabel(f"{pct}%")
        else:
            self._detail_badge.SetLabel("—")

        # Meta line — combine attempts + bank size.
        if attempts:
            self._detail_meta.SetLabel(
                f"{attempts} attempts · "
                f"{info.get('question_count', 0)} questions in bank")
        else:
            self._detail_meta.SetLabel(
                f"Not started yet · "
                f"{info.get('question_count', 0)} questions in bank")

        # Recent-attempts sparkline (last 10 binary correct/incorrect)
        try:
            last = list(Response
                        .select(Response.is_correct)
                        .join(Question)
                        .where((Question.subtopic == sub_id) &
                               (Response.is_correct.is_null(False)))
                        .order_by(Response.created_at.desc())
                        .limit(10))
            values = [1.0 if r.is_correct else 0.0 for r in reversed(last)]
            self._detail_history.set_values(values)
        except Exception:
            self._detail_history.set_values([])

        # Lesson HTML — pull on-demand
        try:
            lesson = Lesson.get_or_none(Lesson.subtopic == sub_id)
        except Exception:
            lesson = None
        if lesson and lesson.body_html:
            self._detail_lesson.set_content(lesson.body_html)
        else:
            self._detail_lesson.set_content(
                "<p style='color:#999'><em>No lesson written for this subtopic "
                "yet. You can still practice questions below — the AI tutor "
                "is one click away on every question.</em></p>"
            )

        self.Layout()
