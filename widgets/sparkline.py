"""
Sparkline — a tiny inline trend chart.

Pure custom-paint, no axes, no labels — meant to live next to a number to
add directional context (e.g. forecast trend, mastery trend).
"""
from typing import Iterable, List, Optional

import wx

from widgets import ui_scale
from widgets.theme import Color


class Sparkline(wx.Panel):
    """Renders a normalized polyline of recent values."""

    def __init__(self, parent, values: Optional[Iterable[float]] = None,
                 color: Optional[wx.Colour] = None):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_SURFACE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._values: List[float] = list(values or [])
        self._color = color or Color.ACCENT
        self.SetMinSize((ui_scale.font_size(120), ui_scale.space(8)))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda _: self.Refresh())

    def set_values(self, values: Iterable[float]):
        self._values = list(values)
        self.Refresh()

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()

        if not self._values:
            # No data — render a flat dim midline so the widget reads as
            # "still empty" rather than blank.
            gc.SetPen(wx.Pen(Color.TEXT_TERTIARY, 1))
            gc.StrokeLine(0, h // 2, w, h // 2)
            return

        if len(self._values) == 1:
            # Single point — draw a dot at the midline. (Gives visual
            # confirmation of one data point without misleading trend.)
            radius = ui_scale.font_size(3)
            gc.SetBrush(wx.Brush(self._color))
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.DrawEllipse(w // 2 - radius, h // 2 - radius,
                           2 * radius, 2 * radius)
            return

        lo = min(self._values)
        hi = max(self._values)
        span = max(1.0, hi - lo)

        pad = ui_scale.space(1)
        n = len(self._values)
        path = gc.CreatePath()
        for i, v in enumerate(self._values):
            x = pad + (w - 2 * pad) * i / (n - 1)
            y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
            if i == 0:
                path.MoveToPoint(x, y)
            else:
                path.AddLineToPoint(x, y)

        gc.SetPen(wx.Pen(self._color, ui_scale.font_size(2)))
        gc.SetBrush(wx.NullBrush)
        gc.StrokePath(path)

        # Endpoint marker
        ex = pad + (w - 2 * pad)
        ey = pad + (h - 2 * pad) * (1 - (self._values[-1] - lo) / span)
        gc.SetBrush(wx.Brush(self._color))
        radius = ui_scale.font_size(3)
        gc.DrawEllipse(ex - radius, ey - radius, 2 * radius, 2 * radius)
