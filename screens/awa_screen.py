"""
AWA screen — essay writing with prompt display, rich text editor, timer, and word count.
"""
import wx
import wx.richtext

from widgets.timer import TimerWidget
from config import AWA_TIME


class AWAScreen(wx.Panel):
    """Analytical Writing Assessment screen."""

    def __init__(self, parent):
        super().__init__(parent)
        self._on_submit = None
        self._prompt_data = None
        self._build_ui()

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Top bar: section label + timer ────────────────────────────
        top_bar = wx.BoxSizer(wx.HORIZONTAL)
        section_label = wx.StaticText(self, label="Analytical Writing — Analyze an Issue")
        section_label.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                       wx.FONTWEIGHT_BOLD))
        top_bar.Add(section_label, 1, wx.ALIGN_CENTER_VERTICAL)

        self.timer = TimerWidget(self, AWA_TIME)
        top_bar.Add(self.timer, 0, wx.ALIGN_CENTER_VERTICAL)

        main_sizer.Add(top_bar, 0, wx.EXPAND | wx.ALL, 10)

        # ── Horizontal split: prompt (left) + editor (right) ─────────
        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)

        # Left: Prompt
        prompt_panel = wx.Panel(splitter)
        prompt_sizer = wx.BoxSizer(wx.VERTICAL)
        prompt_header = wx.StaticText(prompt_panel, label="Issue Prompt:")
        prompt_header.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                       wx.FONTWEIGHT_BOLD))
        prompt_sizer.Add(prompt_header, 0, wx.ALL, 8)

        self.prompt_text = wx.TextCtrl(prompt_panel, style=wx.TE_MULTILINE | wx.TE_READONLY |
                                        wx.TE_WORDWRAP | wx.TE_RICH2)
        self.prompt_text.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                          wx.FONTWEIGHT_NORMAL))
        prompt_sizer.Add(self.prompt_text, 1, wx.EXPAND | wx.ALL, 8)
        prompt_panel.SetSizer(prompt_sizer)

        # Right: Essay editor
        editor_panel = wx.Panel(splitter)
        editor_sizer = wx.BoxSizer(wx.VERTICAL)

        editor_header = wx.StaticText(editor_panel, label="Your Essay:")
        editor_header.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                       wx.FONTWEIGHT_BOLD))
        editor_sizer.Add(editor_header, 0, wx.ALL, 8)

        self.editor = wx.richtext.RichTextCtrl(editor_panel,
                                                style=wx.VSCROLL | wx.HSCROLL |
                                                wx.TE_MULTILINE | wx.TE_WORDWRAP)
        self.editor.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                     wx.FONTWEIGHT_NORMAL))
        self.editor.Bind(wx.EVT_TEXT, self._on_text_change)
        editor_sizer.Add(self.editor, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # Word count
        self.word_count_label = wx.StaticText(editor_panel, label="Words: 0")
        self.word_count_label.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                               wx.FONTWEIGHT_NORMAL))
        editor_sizer.Add(self.word_count_label, 0, wx.ALL, 8)

        editor_panel.SetSizer(editor_sizer)

        splitter.SplitVertically(prompt_panel, editor_panel, 400)
        splitter.SetMinimumPaneSize(250)
        main_sizer.Add(splitter, 1, wx.EXPAND)

        # ── Bottom bar: submit button ─────────────────────────────────
        bottom_bar = wx.BoxSizer(wx.HORIZONTAL)

        self.exit_btn = wx.Button(self, label="Exit to Dashboard", size=(170, 38))
        self.exit_btn.Bind(wx.EVT_BUTTON, self._on_exit_clicked)
        bottom_bar.Add(self.exit_btn, 0, wx.ALL, 8)

        bottom_bar.AddStretchSpacer()

        self.submit_btn = wx.Button(self, label="  Submit Essay  ", size=(160, 38))
        self.submit_btn.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                         wx.FONTWEIGHT_BOLD))
        self.submit_btn.Bind(wx.EVT_BUTTON, self._on_submit_click)
        bottom_bar.Add(self.submit_btn, 0, wx.ALL, 8)

        main_sizer.Add(bottom_bar, 0, wx.EXPAND)
        self.SetSizer(main_sizer)

    def load_prompt(self, prompt_data):
        """Load an AWA prompt. prompt_data = {"prompt_text": ..., "instructions": ...}"""
        self._prompt_data = prompt_data
        text = prompt_data.get("prompt_text", "")
        instructions = prompt_data.get("instructions", "")
        if instructions:
            text += f"\n\n{instructions}"
        self.prompt_text.SetValue(text)
        self.editor.Clear()
        self.word_count_label.SetLabel("Words: 0")

    def start_timer(self):
        self.timer.set_time(AWA_TIME)
        self.timer.start()

    def set_on_submit(self, callback):
        """callback(essay_text, word_count)"""
        self._on_submit = callback

    def set_on_time_expire(self, callback):
        self.timer.set_on_expire(callback)

    def set_on_exit(self, callback):
        """callback() — exit AWA back to dashboard"""
        self._on_exit = callback

    def _on_exit_clicked(self, event):
        dlg = wx.MessageDialog(
            self,
            "Exit to dashboard? Your essay will be lost.",
            "Exit AWA?",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        if dlg.ShowModal() == wx.ID_YES:
            dlg.Destroy()
            if hasattr(self, '_on_exit') and self._on_exit:
                self._on_exit()
        else:
            dlg.Destroy()

    def get_essay(self):
        return self.editor.GetValue()

    def get_word_count(self):
        text = self.editor.GetValue().strip()
        if not text:
            return 0
        return len(text.split())

    def _on_text_change(self, event):
        count = self.get_word_count()
        self.word_count_label.SetLabel(f"Words: {count}")

    def _on_submit_click(self, event):
        essay = self.get_essay()
        wc = self.get_word_count()
        if wc < 10:
            dlg = wx.MessageDialog(self,
                                    "Your essay is very short. Are you sure you want to submit?",
                                    "Confirm Submit",
                                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
            if dlg.ShowModal() != wx.ID_YES:
                dlg.Destroy()
                return
            dlg.Destroy()
        if self._on_submit:
            self._on_submit(essay, wc)
