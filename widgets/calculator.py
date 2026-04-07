"""
On-screen calculator widget for Quantitative Reasoning sections.
Mimics the basic ETS on-screen calculator.
"""
import wx


class CalculatorWidget(wx.Panel):
    """
    Basic on-screen calculator (ETS style).
    Supports: +, −, ×, ÷, √, ±, %, memory (MR, MC, M+).
    """

    BUTTONS = [
        ["MR", "MC", "M+", "C"],
        ["7", "8", "9", "÷"],
        ["4", "5", "6", "×"],
        ["1", "2", "3", "−"],
        ["0", ".", "±", "+"],
        ["√", "(", ")", "="],
    ]

    def __init__(self, parent):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.SetBackgroundColour(wx.Colour(240, 240, 240))

        self._expression = ""
        self._memory = 0.0

        # Display
        self.display = wx.TextCtrl(self, style=wx.TE_RIGHT | wx.TE_READONLY)
        self.display.SetFont(wx.Font(14, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL,
                                      wx.FONTWEIGHT_NORMAL))
        self.display.SetValue("0")

        # Button grid
        grid = wx.GridSizer(rows=len(self.BUTTONS), cols=4, hgap=2, vgap=2)

        for row in self.BUTTONS:
            for label in row:
                btn = wx.Button(self, label=label, size=(48, 36))
                btn.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                     wx.FONTWEIGHT_NORMAL))
                btn.Bind(wx.EVT_BUTTON, lambda e, l=label: self._on_button(l))
                grid.Add(btn, 0, wx.EXPAND)

        # Transfer button — allows copying result to numeric entry
        self.transfer_btn = wx.Button(self, label="Transfer Display")
        self.transfer_btn.Bind(wx.EVT_BUTTON, self._on_transfer)
        self._on_transfer_callback = None

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.display, 0, wx.EXPAND | wx.ALL, 4)
        sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 4)
        sizer.Add(self.transfer_btn, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)

    def _on_button(self, label):
        if label == "C":
            self._expression = ""
            self.display.SetValue("0")
        elif label == "=":
            self._evaluate()
        elif label == "±":
            self._toggle_sign()
        elif label == "√":
            self._sqrt()
        elif label == "MR":
            self._expression = str(self._memory)
            self.display.SetValue(self._expression)
        elif label == "MC":
            self._memory = 0.0
        elif label == "M+":
            self._evaluate()
            try:
                self._memory += float(self.display.GetValue())
            except ValueError:
                pass
        elif label in "÷×−+.()":
            op_map = {"÷": "/", "×": "*", "−": "-"}
            self._expression += op_map.get(label, label)
            self.display.SetValue(self._expression)
        else:
            # Digit
            if self._expression == "0":
                self._expression = label
            else:
                self._expression += label
            self.display.SetValue(self._expression)

    def _evaluate(self):
        try:
            # Safe evaluation of arithmetic expressions only
            allowed = set("0123456789.+-*/() ")
            expr = self._expression
            if not all(c in allowed for c in expr):
                self.display.SetValue("Error")
                return
            result = eval(expr, {"__builtins__": {}}, {})  # noqa: S307
            if isinstance(result, float) and result == int(result):
                result = int(result)
            self._expression = str(result)
            self.display.SetValue(self._expression)
        except Exception:
            self.display.SetValue("Error")
            self._expression = ""

    def _toggle_sign(self):
        try:
            val = float(self._expression or "0")
            val = -val
            self._expression = str(int(val) if val == int(val) else val)
            self.display.SetValue(self._expression)
        except ValueError:
            pass

    def _sqrt(self):
        try:
            val = float(self._expression or "0")
            if val < 0:
                self.display.SetValue("Error")
                self._expression = ""
                return
            result = val ** 0.5
            if result == int(result):
                result = int(result)
            self._expression = str(result)
            self.display.SetValue(self._expression)
        except ValueError:
            self.display.SetValue("Error")
            self._expression = ""

    def set_on_transfer(self, callback):
        """Set callback: callback(value_string) when Transfer is clicked."""
        self._on_transfer_callback = callback

    def _on_transfer(self, event):
        if self._on_transfer_callback:
            self._on_transfer_callback(self.display.GetValue())

    def get_value(self):
        return self.display.GetValue()
