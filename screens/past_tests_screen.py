"""
Past Tests screen — browse every completed session and re-open the
post-test review dialog for any of them.

Sits as a top-level sidebar tab between Insights and Error Log. The
content is a single `wx.ListCtrl` (LC_REPORT) with one row per
completed session, sorted newest-first. Double-clicking a row (or
selecting one and pressing the "Review answers" button) opens the
shared `AnswerReviewDialog` populated from the session's stored
Response rows joined to current Question data.

The screen is intentionally read-only — every interactive surface
(report-issue, etc.) lives inside the review dialog. This keeps the
list lightweight and lets the existing dialog continue to be the
single source of truth for per-question affordances (LaTeX rendering,
report button, explanation text, correct vs user's answer).
"""
from typing import List, Optional

import wx

from services.log import get_logger
from widgets import ui_scale
from widgets.theme import Color

logger = get_logger("past_tests")


_TEST_TYPE_LABEL = {
    "full_mock": "Full mock",
    "section": "Section test",
    "drill": "Topic drill",
    "custom": "Custom test",
}


def _format_started_at(started_at) -> str:
    if started_at is None:
        return "—"
    try:
        return started_at.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(started_at)


def _format_test_type(test_type: str, mode: str) -> str:
    label = _TEST_TYPE_LABEL.get(test_type, test_type.replace("_", " ").title())
    if mode and mode != "simulation":
        label = f"{label} ({mode})"
    return label


def _format_score(scores: Optional[dict]) -> str:
    if not scores:
        return "—"
    parts: List[str] = []
    if scores.get("verbal_low") is not None and scores.get("verbal_high") is not None:
        parts.append(f"V {scores['verbal_low']}-{scores['verbal_high']}")
    if scores.get("quant_low") is not None and scores.get("quant_high") is not None:
        parts.append(f"Q {scores['quant_low']}-{scores['quant_high']}")
    if scores.get("awa") is not None:
        parts.append(f"AWA {scores['awa']:.1f}")
    return "  ·  ".join(parts) if parts else "—"


def _format_accuracy(accuracy: Optional[float], n_correct: int, n_questions: int) -> str:
    if n_questions == 0:
        return "—"
    if accuracy is None:
        return f"{n_correct}/{n_questions}"
    return f"{n_correct}/{n_questions} ({accuracy * 100:.0f}%)"


class PastTestsScreen(wx.Panel):
    """List + review-launcher for every completed test session."""

    # Column indices — match the order used in _build_list().
    COL_DATE = 0
    COL_TYPE = 1
    COL_SCORE = 2
    COL_QUESTIONS = 3
    COL_ACCURACY = 4

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)
        # Cache of summary dicts in the same order as ListCtrl rows so we
        # can map a row index → session_id without parsing the visible
        # cell text.
        self._rows_cache: List[dict] = []
        self._build_ui()

    # ── public API ────────────────────────────────────────────────────

    def refresh(self):
        """Reload summaries from the DB and repopulate the list."""
        # Imported lazily so the unit-test temp_db fixture (which
        # evicts and re-imports `services` between tests) hands us a
        # function bound to the patched DB, not the previous test's
        # stale module.
        from services.analytics import get_past_session_summaries
        try:
            self._rows_cache = get_past_session_summaries(limit=100)
        except Exception:
            logger.exception("failed to load past-session summaries")
            self._rows_cache = []
        self._render_rows()

    # ── layout ────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(self, label="Past Tests")
        title.SetForegroundColour(Color.TEXT_PRIMARY)
        title.SetFont(wx.Font(
            ui_scale.text_2xl(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        outer.Add(title, 0, wx.ALL, ui_scale.space(5))

        subtitle = wx.StaticText(
            self,
            label=(
                "Every completed mock, section test, and drill — "
                "click a row to review questions, answers, and explanations."
            ),
        )
        subtitle.SetForegroundColour(Color.TEXT_SECONDARY)
        subtitle.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        outer.Add(subtitle, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM,
                  ui_scale.space(5))

        # The list lives in a wx.Panel container so we can swap the
        # empty-state placeholder in/out without disturbing the rest of
        # the layout.
        self._list_panel = wx.Panel(self)
        self._list_panel.SetBackgroundColour(Color.BG_PAGE)
        self._list_sizer = wx.BoxSizer(wx.VERTICAL)
        self._list_panel.SetSizer(self._list_sizer)

        self._list_ctrl = self._build_list(self._list_panel)
        self._list_sizer.Add(self._list_ctrl, 1, wx.EXPAND)

        self._empty_state = self._build_empty_state(self._list_panel)
        self._list_sizer.Add(self._empty_state, 1, wx.EXPAND)
        self._empty_state.Hide()

        outer.Add(self._list_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT,
                  ui_scale.space(5))

        # Action row: Review button, sits below the list.
        action_row = wx.BoxSizer(wx.HORIZONTAL)
        self._review_btn = wx.Button(self, label="Review answers")
        self._review_btn.Disable()
        self._review_btn.Bind(wx.EVT_BUTTON, self._on_review_clicked)
        action_row.AddStretchSpacer(1)
        action_row.Add(self._review_btn, 0)
        outer.Add(action_row, 0, wx.EXPAND | wx.ALL, ui_scale.space(5))

        self.SetSizer(outer)

    def _build_list(self, parent) -> wx.ListCtrl:
        ctrl = wx.ListCtrl(
            parent,
            style=wx.LC_REPORT | wx.LC_HRULES | wx.LC_SINGLE_SEL,
        )
        ctrl.SetBackgroundColour(Color.BG_SURFACE)
        ctrl.SetForegroundColour(Color.TEXT_PRIMARY)
        ctrl.InsertColumn(self.COL_DATE, "Date",
                          width=ui_scale.font_size(160))
        ctrl.InsertColumn(self.COL_TYPE, "Type",
                          width=ui_scale.font_size(160))
        ctrl.InsertColumn(self.COL_SCORE, "Score",
                          width=ui_scale.font_size(220))
        ctrl.InsertColumn(self.COL_QUESTIONS, "Questions",
                          width=ui_scale.font_size(100))
        ctrl.InsertColumn(self.COL_ACCURACY, "Correct",
                          width=ui_scale.font_size(140))

        ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_row_activated)
        ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_row_selected)
        ctrl.Bind(wx.EVT_LIST_ITEM_DESELECTED, self._on_row_deselected)
        return ctrl

    def _build_empty_state(self, parent) -> wx.Panel:
        panel = wx.Panel(parent)
        panel.SetBackgroundColour(Color.BG_SURFACE)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.AddStretchSpacer(1)

        head = wx.StaticText(panel, label="No completed tests yet")
        head.SetForegroundColour(Color.TEXT_PRIMARY)
        head.SetFont(wx.Font(
            ui_scale.text_xl(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        sizer.Add(head, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM,
                  ui_scale.space(2))

        body = wx.StaticText(
            panel,
            label=(
                "Finish a mock or section test from the Practice tab "
                "to see it here."
            ),
        )
        body.SetForegroundColour(Color.TEXT_SECONDARY)
        body.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        sizer.Add(body, 0, wx.ALIGN_CENTER_HORIZONTAL)

        sizer.AddStretchSpacer(1)
        panel.SetSizer(sizer)
        return panel

    # ── render ────────────────────────────────────────────────────────

    def _render_rows(self):
        self._list_ctrl.DeleteAllItems()
        if not self._rows_cache:
            self._list_ctrl.Hide()
            self._empty_state.Show()
            self._review_btn.Disable()
            self._list_panel.Layout()
            return

        self._empty_state.Hide()
        self._list_ctrl.Show()

        for summary in self._rows_cache:
            idx = self._list_ctrl.InsertItem(
                self._list_ctrl.GetItemCount(),
                _format_started_at(summary.get("started_at")),
            )
            self._list_ctrl.SetItem(
                idx, self.COL_TYPE,
                _format_test_type(summary.get("test_type", ""),
                                  summary.get("mode", "")),
            )
            self._list_ctrl.SetItem(
                idx, self.COL_SCORE,
                _format_score(summary.get("scores")),
            )
            self._list_ctrl.SetItem(
                idx, self.COL_QUESTIONS,
                str(summary.get("n_questions", 0)),
            )
            self._list_ctrl.SetItem(
                idx, self.COL_ACCURACY,
                _format_accuracy(
                    summary.get("accuracy"),
                    summary.get("n_correct", 0),
                    summary.get("n_questions", 0),
                ),
            )
        self._list_panel.Layout()

    # ── event handlers ────────────────────────────────────────────────

    def _on_row_selected(self, evt):
        self._review_btn.Enable()
        evt.Skip()

    def _on_row_deselected(self, evt):
        if self._list_ctrl.GetFirstSelected() == -1:
            self._review_btn.Disable()
        evt.Skip()

    def _on_row_activated(self, evt):
        idx = evt.GetIndex()
        self._open_review_for_row(idx)

    def _on_review_clicked(self, _evt):
        idx = self._list_ctrl.GetFirstSelected()
        if idx == -1:
            return
        self._open_review_for_row(idx)

    def _open_review_for_row(self, row_idx: int) -> None:
        """Resolve row index → session_id, build details, show dialog."""
        if row_idx < 0 or row_idx >= len(self._rows_cache):
            return
        summary = self._rows_cache[row_idx]
        session_id = summary.get("session_id")
        if session_id is None:
            return
        # Imported lazily — see comment in refresh() for the same
        # rebind-on-test pattern.
        from services.analytics import build_session_question_details
        try:
            details = build_session_question_details(session_id)
        except Exception:
            logger.exception(
                "failed to build review details for session %s", session_id,
            )
            wx.MessageBox(
                "Sorry — couldn't load this session's questions.\n"
                "See data/gre_app.log for details.",
                "Review failed",
                wx.OK | wx.ICON_ERROR,
                parent=self,
            )
            return

        if not details:
            wx.MessageBox(
                "This session has no recorded responses to review.",
                "Nothing to review",
                wx.OK | wx.ICON_INFORMATION,
                parent=self,
            )
            return

        # Imported lazily so a wx-less unit test can still touch the
        # row-activation plumbing without dragging the dialog in.
        from screens.answer_review_dialog import AnswerReviewDialog

        dlg = AnswerReviewDialog(self, details)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
