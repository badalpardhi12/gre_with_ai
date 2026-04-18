"""
Empty-state component — used everywhere a screen might have no data yet.

A "no data" screen is a first impression; it should guide the user to the
single most useful next action, not say "no data".

Usage:
    es = EmptyState(parent, icon="🎯",
                    headline="No mastery data yet",
                    body="Take a 30-question diagnostic to see your strengths.",
                    cta_label="Take diagnostic →",
                    on_cta=self._open_diagnostic)
"""
import wx

from widgets import ui_scale
from widgets.primary_button import PrimaryButton
from widgets.theme import Color


class EmptyState(wx.Panel):
    """Centered icon + headline + body + optional CTA button."""

    def __init__(self, parent, icon: str = "📭",
                 headline: str = "Nothing here yet",
                 body: str = "",
                 cta_label: str = "",
                 on_cta=None,
                 max_width: int = 480):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)
        self._on_cta = on_cta

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.AddStretchSpacer(1)

        # Icon (large emoji)
        icon_label = wx.StaticText(self, label=icon)
        icon_label.SetForegroundColour(Color.TEXT_TERTIARY)
        icon_label.SetFont(wx.Font(
            ui_scale.text_display(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        outer.Add(icon_label, 0, wx.ALIGN_CENTER | wx.BOTTOM, ui_scale.space(3))

        # Headline
        h = wx.StaticText(self, label=headline)
        h.SetForegroundColour(Color.TEXT_PRIMARY)
        h.SetFont(wx.Font(
            ui_scale.text_xl(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        outer.Add(h, 0, wx.ALIGN_CENTER | wx.BOTTOM, ui_scale.space(2))

        # Body (wrapped)
        if body:
            b = wx.StaticText(self, label=body)
            b.SetForegroundColour(Color.TEXT_SECONDARY)
            b.SetFont(wx.Font(
                ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
            ))
            b.Wrap(ui_scale.font_size(max_width))
            outer.Add(b, 0, wx.ALIGN_CENTER | wx.BOTTOM, ui_scale.space(4))

        # CTA
        if cta_label and on_cta:
            btn = PrimaryButton(self, label=cta_label,
                                height=ui_scale.space(11))
            btn.SetMaxSize((ui_scale.font_size(280), -1))
            btn.Bind(wx.EVT_BUTTON, lambda _: on_cta())
            outer.Add(btn, 0, wx.ALIGN_CENTER)

        outer.AddStretchSpacer(1)
        self.SetSizer(outer)
