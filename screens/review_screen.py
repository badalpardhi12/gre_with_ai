"""
Review screen — ETS GRE end-of-section "Review Your Answers" screen.

Re-skinned to the official ETS exam-mode chrome (docs/gre_ui_spec_2026_06.md §6):
a navy header (ETS·GRE lockup + Submit Section) over a white content area with a
"Review Your Answers" title, a legend line, and a re-skinned ``wx.ListCtrl``
(LC_REPORT) of one row per question. Columns are **Question Number | Status |
Marked** — there is deliberately NO correctness / "Score Status" column, because
the real test never shows correctness mid-section (simulation fidelity). A navy
footer carries a "Go to Question" numeric jump, a "Return" button, and a
prominent mauve "Submit Section" button.

Public API preserved for ``main_frame``:
    load_review(review_data)        — list of {index, question_id, answered, marked}
    set_on_goto(cb)                 — cb(question_index)  [0-based]
    set_on_return(cb)               — cb()
    set_on_end_section(cb)          — cb()
"""
import wx

from widgets import ui_scale
from widgets.exam_button import ExamButton
from widgets.theme import ExamColor


# Status label → row-background tint. Only states that warrant attention get a
# tint; "Answered" stays plain white so the table reads clean.
_STATUS_NOT_ANSWERED = "Not Answered"
_STATUS_ANSWERED = "Answered"
_STATUS_NOT_SEEN = "Not Seen"
_STATUS_INCOMPLETE = "Incomplete"

# Subtle attention tints (legible black text on a light row).
_TINT_NOT_ANSWERED = wx.Colour(0xff, 0xf0, 0xf0)   # faint pink
_TINT_NOT_SEEN = wx.Colour(0xf2, 0xf2, 0xf2)       # faint grey
_TINT_INCOMPLETE = wx.Colour(0xff, 0xf6, 0xe0)     # faint amber

_MARK_GLYPH = "⚑"   # ⚑ flag


class ReviewScreen(wx.Panel):
    """Section review: ETS "Review Your Answers" table with status + marked."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(ExamColor.CONTENT_BG)
        self._on_goto = None
        self._on_return = None
        self._on_end_section = None
        self._row_count = 0
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Navy header: ETS·GRE lockup (left) + Submit Section (right) ─
        self.header = wx.Panel(self)
        self.header.SetBackgroundColour(ExamColor.HEADER_NAVY)
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)

        logo = wx.StaticText(self.header, label="ETS")
        logo.SetForegroundColour(ExamColor.HEADER_NAVY)
        logo.SetBackgroundColour(ExamColor.TEXT_ON_NAVY)
        logo.SetFont(ui_scale.exam_sans(ui_scale.EXAM_COUNTER_PT, wx.FONTWEIGHT_BOLD))
        header_sizer.Add(logo, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(3))

        gre = wx.StaticText(self.header, label="GRE")
        gre.SetForegroundColour(ExamColor.TEXT_ON_NAVY)
        gre.SetFont(ui_scale.exam_sans(ui_scale.EXAM_STEM_PT, wx.FONTWEIGHT_BOLD,
                                       wx.FONTSTYLE_ITALIC))
        header_sizer.Add(gre, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, ui_scale.space(3))

        header_sizer.AddStretchSpacer()

        # Header "Submit Section" mirrors the question screen (ends the section).
        self.header_submit_btn = ExamButton(self.header, "Submit Section",
                                            kind="mauve", icon="⬆",
                                            icon_after=True)
        self.header_submit_btn.Bind(wx.EVT_BUTTON, self._on_end_click)
        header_sizer.Add(self.header_submit_btn, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(2))
        self.header.SetSizer(header_sizer)
        main_sizer.Add(self.header, 0, wx.EXPAND)

        # ── Title (sans bold) on white ─────────────────────────────────
        self.title_label = wx.StaticText(self, label="Review Your Answers")
        self.title_label.SetForegroundColour(ExamColor.TEXT)
        self.title_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_STEM_PT,
                                                    wx.FONTWEIGHT_BOLD))
        main_sizer.Add(self.title_label, 0,
                       wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(4))

        # ── Legend / instructions line ─────────────────────────────────
        self.legend_label = wx.StaticText(
            self,
            label=("Click a question number to return to it, or type a number "
                   "below and choose Go to Question.  " + _MARK_GLYPH +
                   " indicates a question marked for review."),
        )
        self.legend_label.SetForegroundColour(ExamColor.TEXT_MUTED)
        self.legend_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT))
        main_sizer.Add(self.legend_label, 0,
                       wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(2))

        # Summary counts (kept for at-a-glance answered/marked totals).
        self.summary_label = wx.StaticText(self, label="")
        self.summary_label.SetForegroundColour(ExamColor.TEXT_MUTED)
        self.summary_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT))
        main_sizer.Add(self.summary_label, 0,
                       wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, ui_scale.space(2))

        # ── Re-skinned table (LC_REPORT) ───────────────────────────────
        # Columns: Question Number | Status | Marked. No correctness column.
        self.list_ctrl = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_ctrl.SetBackgroundColour(ExamColor.CONTENT_BG)
        self.list_ctrl.SetForegroundColour(ExamColor.TEXT)
        self.list_ctrl.SetFont(ui_scale.exam_sans(ui_scale.EXAM_COUNTER_PT))
        self.list_ctrl.InsertColumn(0, "Question Number",
                                    width=ui_scale.font_size(200))
        self.list_ctrl.InsertColumn(1, "Status", width=ui_scale.font_size(200))
        self.list_ctrl.InsertColumn(2, "Marked", width=ui_scale.font_size(120))
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_item_activated)
        main_sizer.Add(self.list_ctrl, 1,
                       wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, ui_scale.space(4))

        # ── Navy footer: Go to Question N · Return · Submit Section ─────
        self.footer = wx.Panel(self)
        self.footer.SetBackgroundColour(ExamColor.HEADER_NAVY)
        footer_sizer = wx.BoxSizer(wx.HORIZONTAL)

        goto_lbl = wx.StaticText(self.footer, label="Go to Question:")
        goto_lbl.SetForegroundColour(ExamColor.TEXT_ON_NAVY)
        goto_lbl.SetFont(ui_scale.exam_sans(ui_scale.EXAM_BTN_PT))
        footer_sizer.Add(goto_lbl, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT,
                         ui_scale.space(2))

        # 1-based numeric input. min=1; max is bumped per-load_review.
        self.goto_spin = wx.SpinCtrl(self.footer, min=1, max=1, initial=1,
                                     size=(ui_scale.font_size(80), -1))
        self.goto_spin.Bind(wx.EVT_TEXT_ENTER, self._on_goto_number)
        footer_sizer.Add(self.goto_spin, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(1))

        self.goto_btn = ExamButton(self.footer, "Go to Question", kind="grey")
        self.goto_btn.Bind(wx.EVT_BUTTON, self._on_goto_number)
        footer_sizer.Add(self.goto_btn, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(1))

        footer_sizer.AddStretchSpacer()

        self.return_btn = ExamButton(self.footer, "Return", kind="grey")
        self.return_btn.Bind(wx.EVT_BUTTON, self._on_return_click)
        footer_sizer.Add(self.return_btn, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(1))

        self.end_btn = ExamButton(self.footer, "Submit Section", kind="mauve",
                                  icon="⬆", icon_after=True)
        self.end_btn.Bind(wx.EVT_BUTTON, self._on_end_click)
        footer_sizer.Add(self.end_btn, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(1))

        self.footer.SetSizer(footer_sizer)
        main_sizer.Add(self.footer, 0, wx.EXPAND)

        self.SetSizer(main_sizer)

    # ── Public API (preserved) ────────────────────────────────────────

    def load_review(self, review_data):
        """Load review data from ``SectionState.get_review_data()``.

        ``review_data``: list of ``{"index", "question_id", "answered",
        "marked"}``. An optional ``"status"`` key (one of "Answered",
        "Not Answered", "Not Seen", "Incomplete") overrides the
        answered→status mapping so a future caller can pass richer
        statuses without breaking the current 4-key contract.
        """
        self.list_ctrl.DeleteAllItems()
        answered_count = 0
        marked_count = 0

        for item in review_data:
            row = self.list_ctrl.InsertItem(self.list_ctrl.GetItemCount(),
                                             str(item["index"] + 1))
            status = self._status_for(item)
            self.list_ctrl.SetItem(row, 1, status)

            tint = self._tint_for(status)
            if tint is not None:
                self.list_ctrl.SetItemBackgroundColour(row, tint)

            marked = _MARK_GLYPH if item.get("marked") else ""
            self.list_ctrl.SetItem(row, 2, marked)

            if status == _STATUS_ANSWERED:
                answered_count += 1
            if item.get("marked"):
                marked_count += 1

        total = len(review_data)
        self._row_count = total

        # Keep the numeric jump in range (min stays 1; SpinCtrl needs max>=min).
        self.goto_spin.SetRange(1, max(1, total))

        self.summary_label.SetLabel(
            "Answered: {a}/{t}    Not answered: {n}    Marked: {m}".format(
                a=answered_count, t=total,
                n=total - answered_count, m=marked_count)
        )
        self.Layout()

    def set_on_goto(self, callback):
        """callback(question_index)  — 0-based question index."""
        self._on_goto = callback

    def set_on_return(self, callback):
        """callback()"""
        self._on_return = callback

    def set_on_end_section(self, callback):
        """callback()"""
        self._on_end_section = callback

    # ── Status helpers ────────────────────────────────────────────────

    @staticmethod
    def _status_for(item):
        """Resolve a row's status label.

        Honors an explicit ``item["status"]`` override when present;
        otherwise falls back to the current answered→{Answered, Not
        Answered} contract.
        """
        override = item.get("status")
        if override in (_STATUS_ANSWERED, _STATUS_NOT_ANSWERED,
                        _STATUS_NOT_SEEN, _STATUS_INCOMPLETE):
            return override
        return _STATUS_ANSWERED if item.get("answered") else _STATUS_NOT_ANSWERED

    @staticmethod
    def _tint_for(status):
        if status == _STATUS_NOT_ANSWERED:
            return _TINT_NOT_ANSWERED
        if status == _STATUS_NOT_SEEN:
            return _TINT_NOT_SEEN
        if status == _STATUS_INCOMPLETE:
            return _TINT_INCOMPLETE
        return None

    # ── Event handlers ────────────────────────────────────────────────

    def _on_goto_number(self, event):
        """Numeric "Go to Question N" — 1-based input → 0-based callback."""
        if not self._on_goto or self._row_count == 0:
            return
        n = self.goto_spin.GetValue()
        if 1 <= n <= self._row_count:
            self._on_goto(n - 1)

    def _on_item_activated(self, event):
        """Clicking/activating a row jumps to that question (0-based)."""
        if self._on_goto:
            self._on_goto(event.GetIndex())

    def _on_return_click(self, event):
        if self._on_return:
            self._on_return()

    def _on_end_click(self, event):
        dlg = wx.MessageDialog(
            self,
            "Are you sure you want to submit this section? "
            "You will not be able to return.",
            "Submit Section",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        confirmed = dlg.ShowModal() == wx.ID_YES
        dlg.Destroy()
        if confirmed and self._on_end_section:
            self._on_end_section()
