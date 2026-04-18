"""
Secondary button: outlined surface, accent text. Use for non-primary actions
on a card (e.g. "View progress", "Update plan", "Skip").

Custom-painted to match `PrimaryButton` cross-platform.
"""
import wx

from widgets import ui_scale
from widgets.theme import Color


class SecondaryButton(wx.Panel):
    """Outlined button. Emits wx.EVT_BUTTON when activated."""

    def __init__(self, parent, label: str, height=None, accent=None):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self._label = label
        self._accent = accent if accent is not None else Color.ACCENT
        self._hover = False
        self._pressed = False
        self._enabled = True

        # Desired height — kept as a private attr so DoGetBestClientSize can
        # report it back to the sizer even after callers later override the
        # min size (a common gotcha: `SetMinSize((w, -1))` accidentally
        # clobbers the height the constructor set).
        self._desired_h = height if height is not None else ui_scale.space(10)
        self.SetMinSize((-1, self._desired_h))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_up)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key)

    def DoGetBestClientSize(self):  # noqa: N802 — wx idiom
        # Authoritative height; lets the sizer place the button at its
        # intended size even if SetMinSize was overridden after construction.
        # Width: text width + side padding so chips never squash to zero.
        try:
            dc = wx.ClientDC(self)
            dc.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                               wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            tw, _ = dc.GetTextExtent(self._label)
        except Exception:
            tw = ui_scale.font_size(60)
        return wx.Size(tw + ui_scale.space(6), self._desired_h)

    def set_label(self, label: str):
        self._label = label
        self.Refresh()

    def Enable(self, enable: bool = True):  # noqa: N802
        self._enabled = bool(enable)
        self.Refresh()
        return super().Enable(enable)

    def _emit(self):
        evt = wx.CommandEvent(wx.wxEVT_BUTTON, self.GetId())
        evt.SetEventObject(self)
        wx.PostEvent(self, evt)

    def _on_enter(self, _):
        if self._enabled:
            self._hover = True
            self.Refresh()

    def _on_leave(self, _):
        self._hover = False
        self._pressed = False
        self.Refresh()

    def _on_down(self, evt):
        if not self._enabled:
            return
        self.SetFocus()
        self._pressed = True
        self.CaptureMouse()
        self.Refresh()

    def _on_up(self, evt):
        if not self._enabled:
            return
        if self.HasCapture():
            self.ReleaseMouse()
        was_pressed = self._pressed
        self._pressed = False
        self.Refresh()
        if was_pressed and self.GetClientRect().Contains(evt.GetPosition()):
            self._emit()

    def _on_key(self, evt):
        if not self._enabled:
            return
        if evt.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
            self._emit()
            return
        evt.Skip()

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()

        if not self._enabled:
            border = Color.BORDER
            text = Color.TEXT_TERTIARY
            fill = Color.BG_PAGE
        elif self._pressed:
            border = self._accent
            text = self._accent
            fill = Color.BG_ELEVATED
        elif self._hover:
            border = self._accent
            text = self._accent
            fill = Color.BG_HOVER
        else:
            # Resting: accent border so the button reads as clickable against
            # the page background. (BORDER_STRONG was 1.3:1 contrast which
            # made these chips nearly invisible.)
            border = self._accent
            text = Color.TEXT_PRIMARY
            fill = Color.BG_SURFACE

        radius = ui_scale.space(2)
        gc.SetBrush(wx.Brush(fill))
        gc.SetPen(wx.Pen(border, 1))
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, radius)

        font = wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        gc.SetFont(font, text)
        text_max_w = max(0, w - 2 * ui_scale.space(2))
        label = self._fit(gc, self._label, text_max_w)
        tw, th = gc.GetTextExtent(label)
        gc.DrawText(label, (w - tw) // 2, (h - th) // 2)

    @staticmethod
    def _fit(gc, text: str, max_w: int) -> str:
        if max_w <= 0 or not text:
            return text
        if gc.GetTextExtent(text)[0] <= max_w:
            return text
        ell = "…"
        if gc.GetTextExtent(ell)[0] >= max_w:
            return ""
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if gc.GetTextExtent(text[:mid] + ell)[0] <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + ell
