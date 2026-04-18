"""
Practice screen — three clearly-distinct modes.

Replaces the old dashboard's seven near-identical "Quick Action" buttons
with three mode cards that map to three real workflows: Quick Drill (smart
short session), Section Test (one timed verbal/quant section), Full Mock
Exam (the full GRE simulation). Recent practice runs at the bottom.
"""
from typing import Callable, Optional

import wx

from models.database import Session as DBSession, ScoringResult
from services.log import get_logger
from widgets import ui_scale
from widgets.card import Card
from widgets.primary_button import PrimaryButton
from widgets.secondary_button import SecondaryButton
from widgets.theme import Color

logger = get_logger("practice")


class _ModeCard(wx.Panel):
    """One of the three big mode cards."""

    def __init__(self, parent, *, icon: str, title: str, summary: str,
                 details: list, cta_label: str,
                 on_click: Callable[[], None]):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_SURFACE)
        self.SetMinSize((-1, ui_scale.font_size(280)))

        outer = wx.BoxSizer(wx.VERTICAL)

        # Icon
        icon_label = wx.StaticText(self, label=icon)
        icon_label.SetForegroundColour(Color.ACCENT)
        icon_label.SetFont(wx.Font(
            ui_scale.text_2xl(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        outer.Add(icon_label, 0, wx.LEFT | wx.RIGHT | wx.TOP,
                  ui_scale.space(4))

        # Title
        t = wx.StaticText(self, label=title)
        t.SetForegroundColour(Color.TEXT_PRIMARY)
        t.SetFont(wx.Font(
            ui_scale.text_xl(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        outer.Add(t, 0, wx.LEFT | wx.RIGHT | wx.TOP,
                  ui_scale.space(4))

        # Summary
        s = wx.StaticText(self, label=summary)
        s.SetForegroundColour(Color.TEXT_SECONDARY)
        s.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        s.Wrap(ui_scale.font_size(220))
        outer.Add(s, 0, wx.LEFT | wx.RIGHT | wx.TOP,
                  ui_scale.space(2))

        # Detail bullets
        for d in details:
            line = wx.StaticText(self, label="• " + d)
            line.SetForegroundColour(Color.TEXT_TERTIARY)
            line.SetFont(wx.Font(
                ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            outer.Add(line, 0, wx.LEFT | wx.RIGHT | wx.TOP,
                      ui_scale.space(1))

        outer.AddStretchSpacer(1)
        cta = PrimaryButton(self, label=cta_label,
                            height=ui_scale.space(11))
        cta.Bind(wx.EVT_BUTTON, lambda _: on_click())
        outer.Add(cta, 0, wx.EXPAND |
                  wx.LEFT | wx.RIGHT | wx.BOTTOM, ui_scale.space(4))

        self.SetSizer(outer)


class PracticeScreen(wx.Panel):
    """Three mode cards + recent practice list."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)
        self._on_quick_drill: Optional[Callable] = None
        self._on_section_test: Optional[Callable] = None   # callable(measure: str)
        self._on_full_mock: Optional[Callable] = None
        self._build_ui()

    def set_handlers(self, quick_drill=None, section_test=None,
                     full_mock=None):
        if quick_drill is not None:
            self._on_quick_drill = quick_drill
        if section_test is not None:
            self._on_section_test = section_test
        if full_mock is not None:
            self._on_full_mock = full_mock

    def refresh(self):
        try:
            self._refresh_recent()
        except Exception:
            logger.exception("recent practice refresh failed")

    # ── layout ────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(self, label="Practice")
        title.SetForegroundColour(Color.TEXT_PRIMARY)
        title.SetFont(wx.Font(
            ui_scale.text_2xl(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        outer.Add(title, 0, wx.ALL, ui_scale.space(5))

        # Three mode cards.
        modes = wx.BoxSizer(wx.HORIZONTAL)
        modes.Add(self._make_quick_drill(), 1, wx.EXPAND | wx.LEFT,
                  ui_scale.space(5))
        modes.Add(self._make_section_test(), 1, wx.EXPAND | wx.LEFT,
                  ui_scale.space(3))
        modes.Add(self._make_full_mock(), 1, wx.EXPAND |
                  wx.LEFT | wx.RIGHT, ui_scale.space(3))
        outer.Add(modes, 0, wx.EXPAND)

        # Recent practice card
        self.recent_card = Card(self, title="RECENT PRACTICE")
        self._recent_body = self.recent_card.body
        outer.Add(self.recent_card, 1, wx.EXPAND |
                  wx.ALL, ui_scale.space(5))

        self.SetSizer(outer)

    def _make_quick_drill(self):
        return _ModeCard(
            self, icon="⚡", title="Quick Drill",
            summary="10 smart-picked questions from your weakest topic. "
                    "Untimed, learning mode.",
            details=["10 questions", "~12-15 min",
                     "Smart-picks weak subtopics", "AI tutor available"],
            cta_label="Start →",
            on_click=lambda: self._on_quick_drill and self._on_quick_drill(),
        )

    def _make_section_test(self):
        # Section test gets a small chooser dialog before launching.
        return _ModeCard(
            self, icon="🎯", title="Section Test",
            summary="One timed Verbal or Quant section, real GRE rules.",
            details=["12-15 questions", "18-26 min", "Timed", "Adaptive S2"],
            cta_label="Choose section →",
            on_click=self._open_section_chooser,
        )

    def _make_full_mock(self):
        return _ModeCard(
            self, icon="🏁", title="Full Mock Exam",
            summary="The complete GRE: AWA + 2 verbal + 2 quant sections "
                    "with adaptive routing.",
            details=["55 questions + essay", "~1 h 58 min",
                     "Adaptive routing", "Real exam rules"],
            cta_label="Begin exam →",
            on_click=lambda: self._on_full_mock and self._on_full_mock(),
        )

    # ── recent ────────────────────────────────────────────────────────

    def _refresh_recent(self):
        self._recent_body.Clear(True)
        rows = list(DBSession
                    .select()
                    .where(DBSession.state.in_(("completed", "abandoned")))
                    .order_by(DBSession.created_at.desc())
                    .limit(8))
        if not rows:
            empty = wx.StaticText(self.recent_card,
                                  label="No practice yet — pick a mode above.")
            empty.SetForegroundColour(Color.TEXT_TERTIARY)
            empty.SetFont(wx.Font(
                ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            self._recent_body.Add(empty, 0)
            return

        for s in rows:
            line = self._format_session_row(s)
            text = wx.StaticText(self.recent_card, label=line)
            text.SetForegroundColour(Color.TEXT_SECONDARY)
            text.SetFont(wx.Font(
                ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            self._recent_body.Add(text, 0, wx.BOTTOM, ui_scale.space(1))
        self.recent_card.Layout()

    def _format_session_row(self, sess) -> str:
        date_str = sess.created_at.strftime("%a %b %d") if sess.created_at else "—"
        kind = sess.test_type.replace("_", " ")
        suffix = ""
        if sess.state == "abandoned":
            suffix = " · abandoned"
        else:
            try:
                sc = ScoringResult.get_or_none(ScoringResult.session == sess.id)
                if sc and sc.verbal_estimated_low is not None:
                    suffix = (f" · V {sc.verbal_estimated_low}–"
                              f"{sc.verbal_estimated_high}, "
                              f"Q {sc.quant_estimated_low}–"
                              f"{sc.quant_estimated_high}")
            except Exception:
                pass
        return f"{date_str}  ·  {kind}{suffix}"

    # ── handlers ──────────────────────────────────────────────────────

    def _open_section_chooser(self):
        if not self._on_section_test:
            return
        # Pre-flight: only offer measures with enough questions.
        from services.question_bank import QuestionBankService
        qb = QuestionBankService()
        v_ok = qb.get_question_count("verbal") >= 12
        q_ok = qb.get_question_count("quant") >= 12
        choices = []
        codes = []
        if v_ok:
            choices.append("Verbal Reasoning")
            codes.append("verbal")
        if q_ok:
            choices.append("Quantitative Reasoning")
            codes.append("quant")
        if not choices:
            wx.MessageBox(
                "Not enough questions in the bank to start a section test.\n"
                "Run `scripts/import_external_quant.py` (or your import "
                "script of choice) to populate it.",
                "Section Test", wx.OK | wx.ICON_INFORMATION)
            return
        dlg = wx.SingleChoiceDialog(
            self,
            "Which section would you like to take? Both run as a 2-section "
            "adaptive test (S1 → adapted S2).",
            "Choose section",
            choices,
        )
        try:
            if dlg.ShowModal() == wx.ID_OK:
                idx = dlg.GetSelection()
                self._on_section_test(codes[idx])
        finally:
            dlg.Destroy()
