"""
ExamToolButton — owner-drawn ETS "Test Preview Tool" ribbon button.

The real GRE header ribbon (top-right) shows each tool as a small raised button
with a LABEL above a tiny ICON: Exit Section, Calc, Mark, Review, Help, Back,
Next (and Continue / Return on transition screens). Gray tools (Calc/Mark/
Review/Help) have a dark label+icon on a light beveled face; the plum
Exit-Section and the blue Back/Next/Continue/Return have a white label+icon on
a colored face. Disabled nav buttons go dark/desaturated.

Owner-drawn (native wx buttons can't do the label-over-icon bevel and ignore
colors under macOS dark mode). Emits wx.EVT_BUTTON. Icons are drawn with a tiny
vector vocabulary keyed by name so we need no image assets.
"""
import wx

from widgets import ui_scale
from widgets.theme import ExamColor


# kind -> (face, face_hover, label/icon color, disabled_face, disabled_text)
_KINDS = {
    "gray": (ExamColor.TOOL_BTN_FACE, ExamColor.TOOL_BTN_FACE_HOVER,
             ExamColor.TOOL_BTN_TEXT, ExamColor.TOOL_BTN_FACE, ExamColor.BTN_DISABLED),
    "plum": (ExamColor.EXIT_PLUM, ExamColor.EXIT_PLUM_HOVER,
             ExamColor.NAV_BTN_TEXT, ExamColor.NAV_BLUE_DISABLED, ExamColor.NAV_BTN_TEXT),
    "blue": (ExamColor.NAV_BLUE, ExamColor.NAV_BLUE_HOVER,
             ExamColor.NAV_BTN_TEXT, ExamColor.NAV_BLUE_DISABLED,
             wx.Colour(0xb8, 0xc2, 0xce)),
}


class ExamToolButton(wx.Panel):
    """A label-over-icon ribbon button. ``icon`` is a glyph name (see _draw_icon).

    kind: 'gray' | 'plum' | 'blue'. Emits wx.EVT_BUTTON when clicked/activated.
    The host header is charcoal, so the gray face reads as a raised key and the
    label is drawn IN the button face (matching the ETS look where label+icon
    sit inside one beveled button).
    """

    def __init__(self, parent, label, icon, kind="gray", min_width=None):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self._label = label
        self._icon = icon
        self._kind = kind if kind in _KINDS else "gray"
        self._hover = False
        self._pressed = False
        self._enabled = True
        self._h = ui_scale.font_size(50)
        self._min_w = min_width if min_width is not None else ui_scale.font_size(62)
        self.SetMinSize(self._compute_size())
        self.SetBackgroundColour(ExamColor.HEADER_CHARCOAL)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ENTER_WINDOW, self._enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._leave)
        self.Bind(wx.EVT_LEFT_DOWN, self._down)
        self.Bind(wx.EVT_LEFT_UP, self._up)
        self.Bind(wx.EVT_KEY_DOWN, self._key)

    def _compute_size(self):
        dc = wx.MemoryDC(); dc.SelectObject(wx.Bitmap(1, 1))
        dc.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT - 1, wx.FONTWEIGHT_BOLD))
        tw, _th = dc.GetTextExtent(self._label)
        dc.SelectObject(wx.NullBitmap)
        return wx.Size(max(self._min_w, tw + ui_scale.space(4)), self._h)

    def DoGetBestClientSize(self):  # noqa: N802
        return self._compute_size()

    # ── API ───────────────────────────────────────────────────────────
    def set_label(self, label, icon=None):
        self._label = label
        if icon is not None:
            self._icon = icon
        self.SetMinSize(self._compute_size())
        self.InvalidateBestSize()
        self.Refresh()

    def Enable(self, enable=True):  # noqa: N802
        self._enabled = bool(enable)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND if enable else wx.CURSOR_ARROW))
        self.Refresh()
        return super().Enable(enable)

    def Disable(self):  # noqa: N802
        return self.Enable(False)

    # ── events ────────────────────────────────────────────────────────
    def _emit(self):
        evt = wx.CommandEvent(wx.wxEVT_BUTTON, self.GetId())
        evt.SetEventObject(self)
        wx.PostEvent(self, evt)

    def _enter(self, _):
        if self._enabled:
            self._hover = True; self.Refresh()

    def _leave(self, _):
        self._hover = False; self._pressed = False; self.Refresh()

    def _down(self, _):
        if self._enabled:
            self.SetFocus(); self._pressed = True
            if not self.HasCapture():
                self.CaptureMouse()
            self.Refresh()

    def _up(self, evt):
        if not self._enabled:
            return
        if self.HasCapture():
            self.ReleaseMouse()
        was = self._pressed
        self._pressed = False
        self.Refresh()
        if was and self.GetClientRect().Contains(evt.GetPosition()):
            self._emit()

    def _key(self, evt):
        if self._enabled and evt.GetKeyCode() in (
                wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
            self._emit(); return
        evt.Skip()

    # ── painting ──────────────────────────────────────────────────────
    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        gc.SetBrush(wx.Brush(ExamColor.HEADER_CHARCOAL))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRectangle(0, 0, w, h)

        face, hover, fg, dface, dtext = _KINDS[self._kind]
        if not self._enabled:
            bg, ink = dface, dtext
        elif self._pressed or self._hover:
            bg, ink = hover, fg
        else:
            bg, ink = face, fg

        pad = ui_scale.space(1)
        bx, by, bw, bh = pad, pad, w - 2 * pad, h - 2 * pad
        # bevel: light top/left, dark bottom/right
        gc.SetBrush(wx.Brush(bg)); gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(bx, by, bw, bh, 3)
        if self._kind == "gray" and self._enabled:
            gc.SetPen(wx.Pen(ExamColor.TOOL_BTN_BEVEL_HI, 1))
            gc.StrokeLine(bx + 1, by + 1, bx + bw - 1, by + 1)
            gc.StrokeLine(bx + 1, by + 1, bx + 1, by + bh - 1)
            gc.SetPen(wx.Pen(ExamColor.TOOL_BTN_BEVEL_LO, 1))
            gc.StrokeLine(bx + 1, by + bh - 1, bx + bw - 1, by + bh - 1)
            gc.StrokeLine(bx + bw - 1, by + 1, bx + bw - 1, by + bh - 1)

        # label (top) + icon (below), as one block vertically centered in the
        # button face so the glyph never spills past the bevel.
        font = ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT - 1, wx.FONTWEIGHT_BOLD)
        gc.SetFont(font, ink)
        tw, th = gc.GetTextExtent(self._label)
        icon_s = ui_scale.font_size(13)
        gap = ui_scale.space(1)
        block_h = th + gap + icon_s
        inner_top = by + ui_scale.space(1)
        inner_bot = by + bh - ui_scale.space(1)
        start_y = inner_top + max(0, ((inner_bot - inner_top) - block_h) / 2)
        gc.DrawText(self._label, bx + (bw - tw) / 2, start_y)
        icon_top = start_y + th + gap
        # Clamp so the glyph's bottom stays inside the button face.
        icon_top = min(icon_top, inner_bot - icon_s)
        # ``bg`` is the button face; ``ink`` the label/glyph color. The help
        # mark needs a glyph drawn IN the contrasting face color, so pass both.
        self._draw_icon(gc, bx + bw / 2, icon_top, icon_s, ink, bg)

    def _draw_icon(self, gc, cx, top, size, color, face=None):
        """Draw a tiny vector glyph named ``self._icon`` centered at x=cx,
        starting at y=top, fitting in ``size`` px. ``color`` is the ink;
        ``face`` (the button background) is used where a glyph needs a
        contrasting mark cut out of a filled shape (the help ``?``)."""
        if face is None:
            face = ExamColor.CONTENT_BG
        s = size
        x0 = cx - s / 2
        pen = wx.Pen(color, max(2, int(s * 0.14)))
        gc.SetPen(pen)
        gc.SetBrush(wx.Brush(color))
        name = self._icon
        if name == "next":      # right arrow
            mid = top + s / 2
            p = gc.CreatePath()
            p.MoveToPoint(x0, mid); p.AddLineToPoint(x0 + s, mid)
            p.MoveToPoint(x0 + s * 0.6, mid - s * 0.28)
            p.AddLineToPoint(x0 + s, mid); p.AddLineToPoint(x0 + s * 0.6, mid + s * 0.28)
            gc.StrokePath(p)
        elif name == "back":    # left arrow
            mid = top + s / 2
            p = gc.CreatePath()
            p.MoveToPoint(x0 + s, mid); p.AddLineToPoint(x0, mid)
            p.MoveToPoint(x0 + s * 0.4, mid - s * 0.28)
            p.AddLineToPoint(x0, mid); p.AddLineToPoint(x0 + s * 0.4, mid + s * 0.28)
            gc.StrokePath(p)
        elif name == "calc":    # calculator: rect + grid dots
            gc.SetBrush(wx.TRANSPARENT_BRUSH)
            gc.SetPen(wx.Pen(color, max(1, int(s * 0.1))))
            gc.DrawRoundedRectangle(x0, top, s, s, 2)
            gc.SetBrush(wx.Brush(color)); gc.SetPen(wx.TRANSPARENT_PEN)
            r = max(1, s * 0.08)
            for ix in range(3):
                for iy in range(3):
                    gc.DrawEllipse(x0 + s * (0.22 + ix * 0.28) - r,
                                   top + s * (0.30 + iy * 0.24) - r, 2 * r, 2 * r)
        elif name == "mark":    # empty square (mark = bookmark/flag box)
            gc.SetBrush(wx.TRANSPARENT_BRUSH)
            gc.SetPen(wx.Pen(color, max(1, int(s * 0.12))))
            gc.DrawRectangle(x0 + s * 0.15, top + s * 0.1, s * 0.7, s * 0.7)
        elif name == "review":  # bookmark/ribbon
            p = gc.CreatePath()
            p.MoveToPoint(x0 + s * 0.25, top)
            p.AddLineToPoint(x0 + s * 0.25, top + s)
            p.AddLineToPoint(x0 + s * 0.5, top + s * 0.72)
            p.AddLineToPoint(x0 + s * 0.75, top + s)
            p.AddLineToPoint(x0 + s * 0.75, top)
            p.CloseSubpath()
            gc.SetBrush(wx.Brush(color)); gc.SetPen(wx.TRANSPARENT_PEN)
            gc.FillPath(p)
        elif name == "help":    # filled circle with a contrasting "?"
            gc.SetBrush(wx.Brush(color)); gc.SetPen(wx.TRANSPARENT_PEN)
            gc.DrawEllipse(x0, top, s, s)
            # Draw the "?" in the button-face color so it reads as cut out of
            # the disc (the previous charcoal-on-dark glyph was invisible).
            f = ui_scale.exam_sans(max(9, int(s * 0.78)), wx.FONTWEIGHT_BOLD)
            gc.SetFont(f, face)
            qw, qh = gc.GetTextExtent("?")
            gc.DrawText("?", cx - qw / 2, top + (s - qh) / 2)
        elif name == "exit":    # door with arrow
            gc.SetBrush(wx.TRANSPARENT_BRUSH)
            gc.SetPen(wx.Pen(color, max(1, int(s * 0.1))))
            gc.DrawRectangle(x0 + s * 0.1, top, s * 0.55, s)
            gc.SetBrush(wx.Brush(color)); gc.SetPen(wx.TRANSPARENT_PEN)
            gc.DrawEllipse(x0 + s * 0.45, top + s * 0.42, s * 0.12, s * 0.12)
        # unknown icon name → just the label (no glyph)
