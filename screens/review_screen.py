"""
Review screen — the ETS "Test Preview Tool" end-of-section "Review" table.

Re-skinned onto the shared ExamChrome (charcoal header + maroon rule + tool
ribbon + pink section bar) so its header/section-bar match the rest of the
in-test screens. Reachable from the End-of-Section page; shows a black-bordered
white content box with a "Review" title, a legend line, and a ``wx.ListCtrl``
(LC_REPORT) of one row per question. Columns are **Question Number | Status |
Marked** — there is deliberately NO correctness / score column, because the
real test never shows correctness mid-section (simulation fidelity).

A content-area footer carries a "Go to Question" numeric jump, a "Return"
button, and a "Submit Section" button; the chrome ribbon is the minimal
``[exit, return, continue]`` (Exit Section + Return + Continue), with Exit and
Continue both ending the section through the confirm dialog.

Public API preserved for ``main_frame``:
    load_review(review_data)        — list of {index, question_id, answered, marked}
    set_on_goto(cb)                 — cb(question_index)  [0-based]
    set_on_return(cb)               — cb()
    set_on_end_section(cb)          — cb()
"""
import wx

from widgets import ui_scale
from widgets.exam_button import ExamButton
from widgets.exam_chrome import ExamChrome
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
    """Section review: ETS "Review" table with status + marked, on ExamChrome."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(ExamColor.PAGE_GRAY)
        self._on_goto = None
        self._on_return = None
        self._on_end_section = None
        self._row_count = 0
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # ── Shared chrome: ribbon = [Exit, Return, Continue] ────────────
        self.chrome = ExamChrome(self, with_timer=True)
        self.chrome.set_buttons(["exit", "return", "continue"])
        self.chrome.set_section_label("Review")
        self.chrome.set_on("exit", self._on_end_click)
        self.chrome.set_on("return", self._on_return_click)
        self.chrome.set_on("continue", self._on_end_click)
        outer.Add(self.chrome, 0, wx.EXPAND)

        # ── Black-bordered white content box on the gray page ───────────
        self.content_border = wx.Panel(self)
        self.content_border.SetBackgroundColour(ExamColor.CONTENT_BORDER)
        border_sizer = wx.BoxSizer(wx.VERTICAL)

        self.content_box = wx.Panel(self.content_border)
        self.content_box.SetBackgroundColour(ExamColor.CONTENT_BG)
        box_sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Title (serif bold) ─────────────────────────────────────────
        self.title_label = wx.StaticText(self.content_box, label="Review")
        self.title_label.SetForegroundColour(ExamColor.TEXT)
        self.title_label.SetFont(ui_scale.exam_serif(ui_scale.BASE_TITLE,
                                                     wx.FONTWEIGHT_BOLD))
        box_sizer.Add(self.title_label, 0,
                      wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(4))

        # ── Legend / instructions line ─────────────────────────────────
        self.legend_label = wx.StaticText(
            self.content_box,
            label=("Click a question number to return to it, or type a number "
                   "below and choose Go to Question.  " + _MARK_GLYPH +
                   " indicates a question marked for review."),
        )
        self.legend_label.SetForegroundColour(ExamColor.TEXT_MUTED)
        self.legend_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT))
        box_sizer.Add(self.legend_label, 0,
                      wx.LEFT | wx.RIGHT | wx.TOP, ui_scale.space(2))

        # Summary counts (at-a-glance answered/marked totals).
        self.summary_label = wx.StaticText(self.content_box, label="")
        self.summary_label.SetForegroundColour(ExamColor.TEXT_MUTED)
        self.summary_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT))
        box_sizer.Add(self.summary_label, 0,
                      wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, ui_scale.space(2))

        # ── Re-skinned table (LC_REPORT) ───────────────────────────────
        # Columns: Question Number | Status | Marked. No correctness column.
        self.list_ctrl = wx.ListCtrl(self.content_box,
                                     style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_ctrl.SetBackgroundColour(ExamColor.CONTENT_BG)
        self.list_ctrl.SetForegroundColour(ExamColor.TEXT)
        self.list_ctrl.SetFont(ui_scale.exam_sans(ui_scale.EXAM_COUNTER_PT))
        self.list_ctrl.InsertColumn(0, "Question Number",
                                    width=ui_scale.font_size(200))
        self.list_ctrl.InsertColumn(1, "Status", width=ui_scale.font_size(200))
        self.list_ctrl.InsertColumn(2, "Marked", width=ui_scale.font_size(120))
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_item_activated)
        box_sizer.Add(self.list_ctrl, 1,
                      wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, ui_scale.space(4))

        # ── Content footer: Go to Question N · Return · Submit Section ──
        footer_sizer = wx.BoxSizer(wx.HORIZONTAL)

        goto_lbl = wx.StaticText(self.content_box, label="Go to Question:")
        goto_lbl.SetForegroundColour(ExamColor.TEXT)
        goto_lbl.SetFont(ui_scale.exam_sans(ui_scale.EXAM_BTN_PT))
        footer_sizer.Add(goto_lbl, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT,
                         ui_scale.space(2))

        # 1-based numeric input. min=1; max is bumped per-load_review.
        self.goto_spin = wx.SpinCtrl(self.content_box, min=1, max=1, initial=1,
                                     size=(ui_scale.font_size(80), -1))
        self.goto_spin.Bind(wx.EVT_TEXT_ENTER, self._on_goto_number)
        footer_sizer.Add(self.goto_spin, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(1))

        self.goto_btn = ExamButton(self.content_box, "Go to Question", kind="grey")
        self.goto_btn.Bind(wx.EVT_BUTTON, self._on_goto_number)
        footer_sizer.Add(self.goto_btn, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(1))

        footer_sizer.AddStretchSpacer()

        self.return_btn = ExamButton(self.content_box, "Return", kind="grey")
        self.return_btn.Bind(wx.EVT_BUTTON, self._on_return_click)
        footer_sizer.Add(self.return_btn, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(1))

        self.end_btn = ExamButton(self.content_box, "Submit Section", kind="mauve",
                                  icon="⬆", icon_after=True)
        self.end_btn.Bind(wx.EVT_BUTTON, self._on_end_click)
        footer_sizer.Add(self.end_btn, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(1))

        box_sizer.Add(footer_sizer, 0,
                      wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, ui_scale.space(3))

        self.content_box.SetSizer(box_sizer)
        border_sizer.Add(self.content_box, 1, wx.EXPAND | wx.ALL,
                         max(1, ui_scale.font_size(2)))
        self.content_border.SetSizer(border_sizer)
        outer.Add(self.content_border, 1, wx.EXPAND | wx.ALL, ui_scale.space(4))

        self.SetSizer(outer)

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

    def set_section_label(self, text):
        """Convenience passthrough to the chrome's pink section bar."""
        self.chrome.set_section_label(text)

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

    def _on_return_click(self, event=None):
        if self._on_return:
            self._on_return()

    def _on_end_click(self, event=None):
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
