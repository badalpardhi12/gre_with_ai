"""
Card surface — a titled, padded panel used for every grouped section in the
new UI (Today, Learn, Practice, Insights). Replaces the per-screen
`_make_card` helpers with a single tokenized component.

Usage:
    card = Card(parent, title="Score Forecast")
    card.body.Add(some_text, 0, wx.EXPAND)
    card.body.Add(some_button, 0, wx.TOP, ui_scale.space(2))
"""
import wx

from widgets import ui_scale
from widgets.theme import Color


class Card(wx.Panel):
    """Padded titled surface. Append children to `card.body` (a BoxSizer)."""

    def __init__(self, parent, title=None, padding=None):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_SURFACE)
        outer = wx.BoxSizer(wx.VERTICAL)

        pad = padding if padding is not None else ui_scale.space(4)

        if title:
            self.title_label = wx.StaticText(self, label=title)
            self.title_label.SetForegroundColour(Color.TEXT_SECONDARY)
            self.title_label.SetFont(wx.Font(
                ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
            ))
            outer.Add(self.title_label, 0,
                      wx.LEFT | wx.RIGHT | wx.TOP, pad)
            outer.Add(self._divider(), 0,
                      wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP,
                      ui_scale.space(2))

        self.body = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self.body, 1, wx.EXPAND | wx.ALL, pad)
        self.SetSizer(outer)

    def _divider(self) -> wx.Panel:
        line = wx.Panel(self, size=(-1, 1))
        line.SetBackgroundColour(Color.BORDER)
        return line

    def set_title(self, label: str):
        if hasattr(self, "title_label"):
            self.title_label.SetLabel(label)
