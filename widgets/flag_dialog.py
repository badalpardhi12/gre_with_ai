"""
Small modal dialog that lets the user report a problem with a question.

Three choices map to the QuestionFlag.reason enum + a free-text note.
On OK the caller persists the row via services.question_bank.flag_question.
"""
import wx

from widgets.theme import Color


REASONS = [
    ("wrong_answer",        "The marked answer is wrong"),
    ("wrong_explanation",   "The explanation doesn't match this question"),
    ("doesnt_make_sense",   "The question is incomplete or doesn't make sense"),
    ("other",               "Other (please describe)"),
]


class FlagQuestionDialog(wx.Dialog):
    def __init__(self, parent, question_id: int):
        super().__init__(
            parent,
            title="Report a Problem",
            size=(440, 360),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        self._question_id = question_id
        self.SetBackgroundColour(Color.BG_PAGE)

        outer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=(
                f"Reporting question #{question_id}.\n"
                "What's wrong with it?"
            ),
        )
        outer.Add(intro, 0, wx.ALL, 12)

        self._radios = []
        for code, label in REASONS:
            style = wx.RB_GROUP if not self._radios else 0
            rb = wx.RadioButton(self, label=label, style=style)
            rb._reason_code = code
            outer.Add(rb, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
            self._radios.append(rb)
        # Default selection: first option
        if self._radios:
            self._radios[0].SetValue(True)

        outer.Add(
            wx.StaticText(self, label="Optional note:"),
            0, wx.LEFT | wx.RIGHT | wx.TOP, 12,
        )
        self._note = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_DONTWRAP, size=(-1, 70),
        )
        outer.Add(self._note, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)

        # Standard OK / Cancel
        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(self, wx.ID_OK, "Submit Report")
        ok_btn.SetDefault()
        cancel_btn = wx.Button(self, wx.ID_CANCEL)
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        outer.Add(btn_sizer, 0, wx.ALL | wx.ALIGN_RIGHT, 12)

        self.SetSizer(outer)

    def get_reason(self) -> str:
        for rb in self._radios:
            if rb.GetValue():
                return rb._reason_code
        return ""

    def get_note(self) -> str:
        return (self._note.GetValue() or "").strip()
