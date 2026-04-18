"""
Numeric entry widget — allows decimal or fraction input for GRE quant questions.
"""
import wx


class NumericEntry(wx.Panel):
    """
    Numeric Entry input field(s) for GRE quantitative questions.
    Supports decimal entry OR fraction entry (numerator / denominator).
    """

    def __init__(self, parent, fraction_mode=False):
        super().__init__(parent)
        self.fraction_mode = fraction_mode
        self._on_change = None

        if fraction_mode:
            self._build_fraction_ui()
        else:
            self._build_decimal_ui()

    def _build_decimal_ui(self):
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        label = wx.StaticText(self, label="Your answer: ")
        self.value_ctrl = wx.TextCtrl(self, size=(120, -1),
                                        style=wx.TE_PROCESS_ENTER)
        self.value_ctrl.Bind(wx.EVT_TEXT, self._fire_change)

        sizer.Add(label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        sizer.Add(self.value_ctrl, 0, wx.ALIGN_CENTER_VERTICAL)
        self.SetSizer(sizer)

    def _build_fraction_ui(self):
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        label = wx.StaticText(self, label="Your answer: ")
        self.num_ctrl = wx.TextCtrl(self, size=(60, -1))
        slash = wx.StaticText(self, label=" / ")
        self.den_ctrl = wx.TextCtrl(self, size=(60, -1))

        self.num_ctrl.Bind(wx.EVT_TEXT, self._fire_change)
        self.den_ctrl.Bind(wx.EVT_TEXT, self._fire_change)

        sizer.Add(label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        sizer.Add(self.num_ctrl, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(slash, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(self.den_ctrl, 0, wx.ALIGN_CENTER_VERTICAL)
        self.SetSizer(sizer)

    def get_response(self):
        """Return response dict for scoring."""
        if self.fraction_mode:
            num = self.num_ctrl.GetValue().strip()
            den = self.den_ctrl.GetValue().strip()
            if num and den:
                try:
                    n, d = int(num), int(den)
                    if d == 0:
                        return {}
                    return {"numerator": n, "denominator": d}
                except ValueError:
                    pass
            return {}
        else:
            val = self.value_ctrl.GetValue().strip()
            if val:
                try:
                    float(val)  # validate
                    return {"value": val}
                except ValueError:
                    pass
            return {}

    def set_response(self, payload):
        """Restore a saved response."""
        if not isinstance(payload, dict):
            return
        if self.fraction_mode:
            self.num_ctrl.SetValue(str(payload.get("numerator", "")))
            self.den_ctrl.SetValue(str(payload.get("denominator", "")))
        else:
            self.value_ctrl.SetValue(str(payload.get("value", "")))

    def clear(self):
        if self.fraction_mode:
            self.num_ctrl.SetValue("")
            self.den_ctrl.SetValue("")
        else:
            self.value_ctrl.SetValue("")

    def set_on_change(self, callback):
        self._on_change = callback

    def _fire_change(self, event):
        if self._on_change:
            self._on_change(self.get_response())
