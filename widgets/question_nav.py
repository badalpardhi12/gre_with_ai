"""
Question navigation widget — shows question grid and status indicators.
"""
import wx


class QuestionNav(wx.Panel):
    """
    Question navigation grid showing answered/marked/current status.
    Used at the bottom of exam sections and in the review screen.
    """

    # Status colours
    CLR_DEFAULT = wx.Colour(230, 230, 230)
    CLR_CURRENT = wx.Colour(100, 149, 237)  # cornflower blue
    CLR_ANSWERED = wx.Colour(144, 238, 144)  # light green
    CLR_MARKED = wx.Colour(255, 200, 100)    # orange
    CLR_MARKED_ANSWERED = wx.Colour(255, 165, 0)  # darker orange

    def __init__(self, parent, total_questions=0):
        super().__init__(parent)
        self.total = total_questions
        self.current_index = 0
        self.answered = set()
        self.marked = set()
        self.buttons = []
        self._on_navigate = None

        self._build_ui()

    def _build_ui(self):
        # `wx.GridSizer(cols=0, ...)` raises on Windows. Guard against the
        # zero-questions edge case so an empty section doesn't blow up the UI.
        cols = max(1, min(self.total or 1, 10))
        self.grid_sizer = wx.GridSizer(cols=cols, hgap=4, vgap=4)
        self.buttons = []

        for i in range(self.total):
            btn = wx.Button(self, label=str(i + 1), size=(36, 28))
            btn.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                wx.FONTWEIGHT_NORMAL))
            btn.Bind(wx.EVT_BUTTON, lambda e, idx=i: self._on_click(idx))
            self.buttons.append(btn)
            self.grid_sizer.Add(btn, 0, wx.EXPAND)

        # Legend
        legend_sizer = wx.BoxSizer(wx.HORIZONTAL)
        for label, colour in [("Current", self.CLR_CURRENT),
                               ("Answered", self.CLR_ANSWERED),
                               ("Marked", self.CLR_MARKED),
                               ("Unanswered", self.CLR_DEFAULT)]:
            swatch = wx.Panel(self, size=(14, 14))
            swatch.SetBackgroundColour(colour)
            legend_sizer.Add(swatch, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
            legend_sizer.Add(wx.StaticText(self, label=label), 0,
                            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(self.grid_sizer, 0, wx.ALL, 4)
        main_sizer.Add(legend_sizer, 0, wx.ALL, 4)
        self.SetSizer(main_sizer)

        self._update_colours()

    def set_state(self, current_index, answered, marked):
        """Update the navigation state and refresh."""
        self.current_index = current_index
        self.answered = set(answered)
        self.marked = set(marked)
        self._update_colours()

    def set_on_navigate(self, callback):
        """Set callback for when user clicks a question number. callback(index)"""
        self._on_navigate = callback

    def _on_click(self, index):
        if self._on_navigate:
            self._on_navigate(index)

    def _update_colours(self):
        for i, btn in enumerate(self.buttons):
            if i == self.current_index:
                btn.SetBackgroundColour(self.CLR_CURRENT)
                btn.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                    wx.FONTWEIGHT_BOLD))
            elif i in self.marked and i in self.answered:
                btn.SetBackgroundColour(self.CLR_MARKED_ANSWERED)
            elif i in self.marked:
                btn.SetBackgroundColour(self.CLR_MARKED)
            elif i in self.answered:
                btn.SetBackgroundColour(self.CLR_ANSWERED)
            else:
                btn.SetBackgroundColour(self.CLR_DEFAULT)
                btn.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                    wx.FONTWEIGHT_NORMAL))
            btn.Refresh()

    def rebuild(self, total_questions):
        """Rebuild for a new question count."""
        self.total = total_questions
        self.current_index = 0
        self.answered = set()
        self.marked = set()
        # Destroy everything and rebuild
        self.DestroyChildren()
        self.buttons = []
        self._build_ui()
        self.Layout()
