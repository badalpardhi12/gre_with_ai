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

        # Theme adaptation — match macOS appearance
        appearance = wx.SystemSettings.GetAppearance()
        is_dark = appearance.IsDark()
        if is_dark:
            bg = wx.Colour(45, 45, 45)
            display_bg = wx.Colour(25, 25, 25)
            display_fg = wx.Colour(245, 245, 245)
            btn_bg = wx.Colour(70, 70, 70)
            btn_fg = wx.Colour(240, 240, 240)
            op_btn_bg = wx.Colour(95, 95, 95)
        else:
            bg = wx.Colour(240, 240, 240)
            display_bg = wx.Colour(255, 255, 255)
            display_fg = wx.Colour(20, 20, 20)
            btn_bg = wx.Colour(255, 255, 255)
            btn_fg = wx.Colour(30, 30, 30)
            op_btn_bg = wx.Colour(220, 220, 220)

        self.SetBackgroundColour(bg)

        self._expression = ""
        self._memory = 0.0

        # Display
        self.display = wx.TextCtrl(self, style=wx.TE_RIGHT | wx.TE_READONLY)
        self.display.SetFont(wx.Font(14, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL,
                                      wx.FONTWEIGHT_NORMAL))
        self.display.SetValue("0")
        self.display.SetBackgroundColour(display_bg)
        self.display.SetForegroundColour(display_fg)

        # Operator buttons get distinct color
        op_labels = {"÷", "×", "−", "+", "=", "√", "(", ")", ".", "±"}
        memory_labels = {"MR", "MC", "M+", "C"}

        # Button grid
        grid = wx.GridSizer(rows=len(self.BUTTONS), cols=4, hgap=2, vgap=2)

        for row in self.BUTTONS:
            for label in row:
                btn = wx.Button(self, label=label, size=(48, 36))
                btn.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                     wx.FONTWEIGHT_NORMAL))
                # Color: operators and memory get distinct background
                if label in op_labels or label in memory_labels:
                    btn.SetBackgroundColour(op_btn_bg)
                else:
                    btn.SetBackgroundColour(btn_bg)
                btn.SetForegroundColour(btn_fg)
                btn.Bind(wx.EVT_BUTTON, lambda e, l=label: self._on_button(l))
                grid.Add(btn, 0, wx.EXPAND)

        # Transfer button — allows copying result to numeric entry
        self.transfer_btn = wx.Button(self, label="Transfer Display")
        self.transfer_btn.SetBackgroundColour(op_btn_bg)
        self.transfer_btn.SetForegroundColour(btn_fg)
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
            # Safe evaluation of arithmetic expressions only.
            # Reject ** outright — even with __builtins__ scrubbed, an
            # expression like 9**9**9 would block the UI on a huge BigInt
            # computation.
            if "**" in self._expression:
                self.display.SetValue("Error")
                self._expression = ""
                return
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
