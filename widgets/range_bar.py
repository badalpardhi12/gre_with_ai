"""
Score-forecast range bar.

A horizontal bar showing the predicted score range against the full
130–170 GRE scale. Lightweight custom-painted widget — uses no charts
library, just `wx.GraphicsContext`.

Usage:
    bar = RangeBar(parent, low=130, high=170, current_low=155, current_high=167)
    bar.SetMinSize((-1, ui_scale.space(8)))
    bar.update(155, 167)
"""
import wx

from widgets import ui_scale
from widgets.theme import Color


class RangeBar(wx.Panel):
    """Min-max range bar; draws a track + a filled segment for the
    forecast range + numeric labels at each end of the segment."""

    def __init__(self, parent,
                 lo: int = 130, hi: int = 170,
                 current_low: int = None, current_high: int = None,
                 label: str = ""):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_SURFACE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._lo = lo
        self._hi = hi
        self._current_low = current_low
        self._current_high = current_high
        self._label = label
        self.SetMinSize((-1, ui_scale.space(11)))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda _: self.Refresh())

    def update(self, current_low, current_high, label: str = None):
        self._current_low = current_low
        self._current_high = current_high
        if label is not None:
            self._label = label
        self.Refresh()

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()

        # Layout: optional label strip on top, track in the middle, axis
        # labels at the ends.
        font = wx.Font(ui_scale.text_xs(), wx.FONTFAMILY_DEFAULT,
                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        gc.SetFont(font, Color.TEXT_TERTIARY)

        margin_x = ui_scale.space(2)
        track_top = h // 2 - ui_scale.space(1)
        track_h = ui_scale.space(2)
        track_x = margin_x + ui_scale.font_size(28)   # leave room for "130"
        track_w = w - 2 * margin_x - 2 * ui_scale.font_size(28)

        # End-axis labels
        gc.DrawText(str(self._lo), margin_x, track_top - ui_scale.space(1))
        end_text = str(self._hi)
        ew, _ = gc.GetTextExtent(end_text)
        gc.DrawText(end_text, w - margin_x - ew, track_top - ui_scale.space(1))

        # Background track
        gc.SetBrush(wx.Brush(Color.BG_ELEVATED))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(track_x, track_top, track_w, track_h,
                                ui_scale.space(1))

        # Filled forecast segment
        if self._current_low is not None and self._current_high is not None:
            span = max(1, self._hi - self._lo)
            x0 = track_x + (max(self._lo, self._current_low) - self._lo) / span * track_w
            x1 = track_x + (min(self._hi, self._current_high) - self._lo) / span * track_w
            seg_w = max(2, x1 - x0)
            gc.SetBrush(wx.Brush(Color.ACCENT))
            gc.DrawRoundedRectangle(x0, track_top, seg_w, track_h,
                                    ui_scale.space(1))

            # Numeric label above the segment
            seg_text = f"{self._current_low}–{self._current_high}"
            sw, sh = gc.GetTextExtent(seg_text)
            label_x = max(track_x, min(track_x + track_w - sw,
                                       (x0 + x1) / 2 - sw / 2))
            label_y = track_top - sh - ui_scale.space(1)
            gc.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                               wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD),
                       Color.TEXT_PRIMARY)
            gc.DrawText(seg_text, label_x, label_y)

        if self._label:
            gc.SetFont(wx.Font(ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
                               wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL),
                       Color.TEXT_SECONDARY)
            gc.DrawText(self._label, margin_x,
                        track_top + track_h + ui_scale.space(1))
