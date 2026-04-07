"""
Welcome screen — test type selection and mode configuration.
"""
import wx

from config import MIN_WINDOW_WIDTH


class WelcomeScreen(wx.Panel):
    """
    Landing screen: select test type, mode, and launch.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self._on_start = None
        self._on_settings = None
        self._on_progress = None
        self._build_ui()

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Title ─────────────────────────────────────────────────────
        title = wx.StaticText(self, label="GRE Mock Test Platform")
        title.SetFont(wx.Font(28, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                               wx.FONTWEIGHT_BOLD))
        main_sizer.Add(title, 0, wx.ALIGN_CENTER | wx.TOP, 40)

        subtitle = wx.StaticText(self, label="Post-September 2023 Format  •  LLM-Supervised")
        subtitle.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                  wx.FONTWEIGHT_NORMAL))
        subtitle.SetForegroundColour(wx.Colour(100, 100, 100))
        main_sizer.Add(subtitle, 0, wx.ALIGN_CENTER | wx.BOTTOM, 30)

        # ── Test Type ─────────────────────────────────────────────────
        type_box = wx.StaticBox(self, label="Select Test Type")
        type_sizer = wx.StaticBoxSizer(type_box, wx.VERTICAL)

        self.test_types = [
            ("full_mock", "Full Mock Test",
             "AWA + 2 Verbal + 2 Quant sections  •  1h 58m"),
            ("verbal", "Verbal Reasoning Only",
             "2 Verbal sections  •  41 minutes"),
            ("quant", "Quantitative Reasoning Only",
             "2 Quant sections  •  47 minutes"),
        ]

        self.type_radios = []
        for i, (key, label, desc) in enumerate(self.test_types):
            radio = wx.RadioButton(self, label=f"{label}", style=wx.RB_GROUP if i == 0 else 0)
            radio.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                   wx.FONTWEIGHT_BOLD))
            desc_text = wx.StaticText(self, label=f"    {desc}")
            desc_text.SetForegroundColour(wx.Colour(100, 100, 100))
            type_sizer.Add(radio, 0, wx.LEFT | wx.TOP, 8)
            type_sizer.Add(desc_text, 0, wx.LEFT | wx.BOTTOM, 8)
            self.type_radios.append((key, radio))

        main_sizer.Add(type_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 80)
        main_sizer.AddSpacer(16)

        # ── Mode ──────────────────────────────────────────────────────
        mode_box = wx.StaticBox(self, label="Test Mode")
        mode_sizer = wx.StaticBoxSizer(mode_box, wx.HORIZONTAL)

        self.simulation_radio = wx.RadioButton(self, label="Simulation Mode  (strict GRE rules)",
                                                style=wx.RB_GROUP)
        self.learning_radio = wx.RadioButton(self, label="Learning Mode  (pause, show answers)")
        self.simulation_radio.SetValue(True)

        mode_sizer.Add(self.simulation_radio, 0, wx.ALL, 8)
        mode_sizer.AddSpacer(20)
        mode_sizer.Add(self.learning_radio, 0, wx.ALL, 8)

        main_sizer.Add(mode_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 80)
        main_sizer.AddSpacer(30)

        # ── Buttons ───────────────────────────────────────────────────
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.start_btn = wx.Button(self, label="  Start Test  ", size=(180, 44))
        self.start_btn.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                        wx.FONTWEIGHT_BOLD))
        self.start_btn.SetBackgroundColour(wx.Colour(46, 125, 50))
        self.start_btn.SetForegroundColour(wx.WHITE)
        self.start_btn.Bind(wx.EVT_BUTTON, self._on_start_click)

        self.settings_btn = wx.Button(self, label="⚙ LLM Settings", size=(140, 44))
        self.settings_btn.Bind(wx.EVT_BUTTON, self._on_settings_click)

        self.progress_btn = wx.Button(self, label="📊 Progress", size=(140, 44))
        self.progress_btn.Bind(wx.EVT_BUTTON, self._on_progress_click)

        btn_sizer.Add(self.start_btn, 0, wx.RIGHT, 16)
        btn_sizer.Add(self.progress_btn, 0, wx.RIGHT, 16)
        btn_sizer.Add(self.settings_btn, 0)

        main_sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER)

        # ── Question count info ───────────────────────────────────────
        main_sizer.AddSpacer(24)
        self.info_label = wx.StaticText(self, label="")
        self.info_label.SetForegroundColour(wx.Colour(120, 120, 120))
        main_sizer.Add(self.info_label, 0, wx.ALIGN_CENTER)

        self.SetSizer(main_sizer)

    def set_on_start(self, callback):
        """callback(test_type, mode)"""
        self._on_start = callback

    def set_on_settings(self, callback):
        """callback()"""
        self._on_settings = callback

    def set_on_progress(self, callback):
        """callback()"""
        self._on_progress = callback

    def set_info(self, text):
        self.info_label.SetLabel(text)
        self.Layout()

    def _on_start_click(self, event):
        if self._on_start:
            test_type = "full_mock"
            for key, radio in self.type_radios:
                if radio.GetValue():
                    test_type = key
                    break
            mode = "simulation" if self.simulation_radio.GetValue() else "learning"
            self._on_start(test_type, mode)

    def _on_settings_click(self, event):
        if self._on_settings:
            self._on_settings()

    def _on_progress_click(self, event):
        if self._on_progress:
            self._on_progress()
