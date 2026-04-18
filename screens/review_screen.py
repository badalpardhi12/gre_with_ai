"""
Review screen — shows all questions in the current section with status.
"""
import wx

from widgets.theme import Color


class ReviewScreen(wx.Panel):
    """Section review: list all questions with answered/marked status."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)
        self._on_goto = None
        self._on_return = None
        self._on_end_section = None
        self._build_ui()

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(self, label="Review Section")
        title.SetFont(wx.Font(18, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                               wx.FONTWEIGHT_BOLD))
        main_sizer.Add(title, 0, wx.ALIGN_CENTER | wx.ALL, 16)

        # Summary
        self.summary_label = wx.StaticText(self, label="")
        self.summary_label.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                            wx.FONTWEIGHT_NORMAL))
        main_sizer.Add(self.summary_label, 0, wx.LEFT | wx.BOTTOM, 20)

        # List
        self.list_ctrl = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_ctrl.InsertColumn(0, "#", width=50)
        self.list_ctrl.InsertColumn(1, "Status", width=150)
        self.list_ctrl.InsertColumn(2, "Marked", width=100)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_item_double_click)
        main_sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 20)

        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.goto_btn = wx.Button(self, label="Go to Selected Question")
        self.goto_btn.Bind(wx.EVT_BUTTON, self._on_goto_click)
        btn_sizer.Add(self.goto_btn, 0, wx.ALL, 8)

        btn_sizer.AddStretchSpacer()

        self.return_btn = wx.Button(self, label="Return to Questions")
        self.return_btn.Bind(wx.EVT_BUTTON, self._on_return_click)
        btn_sizer.Add(self.return_btn, 0, wx.ALL, 8)

        self.end_btn = wx.Button(self, label="End Section")
        self.end_btn.SetBackgroundColour(Color.DANGER)
        self.end_btn.SetForegroundColour(wx.WHITE)
        self.end_btn.Bind(wx.EVT_BUTTON, self._on_end_click)
        btn_sizer.Add(self.end_btn, 0, wx.ALL, 8)

        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(main_sizer)

    def load_review(self, review_data):
        """
        Load review data from SectionState.get_review_data().
        review_data: list of {"index", "question_id", "answered", "marked"}
        """
        self.list_ctrl.DeleteAllItems()
        answered_count = 0
        marked_count = 0

        for item in review_data:
            idx = self.list_ctrl.InsertItem(self.list_ctrl.GetItemCount(),
                                             str(item["index"] + 1))
            status = "Answered" if item["answered"] else "Not Answered"
            self.list_ctrl.SetItem(idx, 1, status)
            if not item["answered"]:
                self.list_ctrl.SetItemBackgroundColour(idx, Color.MASTERY[1])

            marked = "★ Marked" if item["marked"] else ""
            self.list_ctrl.SetItem(idx, 2, marked)

            if item["answered"]:
                answered_count += 1
            if item["marked"]:
                marked_count += 1

        total = len(review_data)
        self.summary_label.SetLabel(
            f"Answered: {answered_count}/{total}  •  "
            f"Not answered: {total - answered_count}  •  "
            f"Marked: {marked_count}"
        )
        self.Layout()

    def set_on_goto(self, callback):
        """callback(question_index)"""
        self._on_goto = callback

    def set_on_return(self, callback):
        """callback()"""
        self._on_return = callback

    def set_on_end_section(self, callback):
        """callback()"""
        self._on_end_section = callback

    def _on_goto_click(self, event):
        sel = self.list_ctrl.GetFirstSelected()
        if sel >= 0 and self._on_goto:
            self._on_goto(sel)

    def _on_item_double_click(self, event):
        idx = event.GetIndex()
        if self._on_goto:
            self._on_goto(idx)

    def _on_return_click(self, event):
        if self._on_return:
            self._on_return()

    def _on_end_click(self, event):
        dlg = wx.MessageDialog(
            self,
            "Are you sure you want to end this section? "
            "You will not be able to return.",
            "End Section",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        if dlg.ShowModal() == wx.ID_YES:
            if self._on_end_section:
                self._on_end_section()
        dlg.Destroy()
