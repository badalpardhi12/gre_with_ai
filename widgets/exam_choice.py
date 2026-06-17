"""
ExamChoice — owner-drawn ETS answer-selection control.

Native wx.RadioButton / wx.CheckBox markers are invisible on macOS over a white
background and can't be ETS-colored, so the GRE oval (single-select) and square
(multi-select) markers are custom-painted (spec §1, §5). The control mimics the
wx control surface the question screen depends on:

  - ``GetValue()`` / ``SetValue(bool)``
  - emits ``wx.EVT_RADIOBUTTON`` (oval) or ``wx.EVT_CHECKBOX`` (square) on click
  - ``activate()`` for click-on-the-choice-text parity

Oval controls enforce single-select across a shared group list (set via
``set_group``); square controls toggle independently.
"""
import wx

from widgets import ui_scale
from widgets.theme import ExamColor


class ExamChoice(wx.Panel):
    """An owner-drawn ETS radio (oval) or checkbox (square) marker."""

    def __init__(self, parent, shape="oval"):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self.shape = shape if shape in ("oval", "square") else "oval"
        self._value = False
        self._group = None  # list[ExamChoice] for radio mutual-exclusion
        d = ui_scale.font_size(20)
        self._d = d
        self.SetMinSize((d, d))
        self.SetBackgroundColour(parent.GetBackgroundColour())
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, lambda _e: self.activate())

    def DoGetBestClientSize(self):  # noqa: N802 — wx idiom
        return wx.Size(self._d, self._d)

    # ── wx-compatible API ─────────────────────────────────────────────
    def GetValue(self):  # noqa: N802
        return self._value

    def SetValue(self, value):  # noqa: N802
        """Set checked state WITHOUT firing an event (matches wx semantics;
        used for restore-on-revisit). Does not clear group siblings."""
        self._value = bool(value)
        self.Refresh()

    def set_group(self, group):
        """Share a list of sibling ovals for single-select mutual exclusion."""
        self._group = group

    # ── interaction ───────────────────────────────────────────────────
    def activate(self):
        """User picked this choice (click on marker or its text)."""
        if self.shape == "oval":
            if self._group:
                for c in self._group:
                    if c is not self:
                        c.SetValue(False)
            self._value = True
            self.Refresh()
            self._emit(wx.wxEVT_RADIOBUTTON)
        else:
            self._value = not self._value
            self.Refresh()
            self._emit(wx.wxEVT_CHECKBOX)

    def _emit(self, evt_type):
        evt = wx.CommandEvent(evt_type, self.GetId())
        evt.SetEventObject(self)
        evt.SetInt(1 if self._value else 0)
        wx.PostEvent(self, evt)

    # ── painting ──────────────────────────────────────────────────────
    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        gc.SetBrush(wx.Brush(self.GetParent().GetBackgroundColour()))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRectangle(0, 0, w, h)

        m = max(2, int(w * 0.18))           # margin
        size = min(w, h) - 2 * m
        x = (w - size) // 2
        y = (h - size) // 2

        if self.shape == "oval":
            gc.SetPen(wx.Pen(ExamColor.OVAL_BORDER, 2))
            gc.SetBrush(wx.Brush(ExamColor.CONTENT_BG))
            gc.DrawEllipse(x, y, size, size)
            if self._value:
                inner = size * 0.45
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.SetBrush(wx.Brush(ExamColor.OVAL_FILL_SELECTED))
                gc.DrawEllipse(x + (size - inner) / 2, y + (size - inner) / 2,
                               inner, inner)
        else:
            r = max(1, int(size * 0.12))
            if self._value:
                gc.SetPen(wx.Pen(ExamColor.CHECK_FILL_SELECTED, 2))
                gc.SetBrush(wx.Brush(ExamColor.CHECK_FILL_SELECTED))
                gc.DrawRoundedRectangle(x, y, size, size, r)
                # white check mark
                path = gc.CreatePath()
                path.MoveToPoint(x + size * 0.24, y + size * 0.52)
                path.AddLineToPoint(x + size * 0.42, y + size * 0.70)
                path.AddLineToPoint(x + size * 0.78, y + size * 0.28)
                gc.SetPen(wx.Pen(ExamColor.TEXT_ON_NAVY, max(2, int(size * 0.12))))
                gc.StrokePath(path)
            else:
                gc.SetPen(wx.Pen(ExamColor.CHECK_BORDER, 2))
                gc.SetBrush(wx.Brush(ExamColor.CONTENT_BG))
                gc.DrawRoundedRectangle(x, y, size, size, r)
