"""
ExamButton — owner-drawn button for the ETS GRE exam-mode chrome.

Native wx.Button ignores SetBackgroundColour on macOS, so the ETS-colored
controls (blue Next, grey Mark/Back, mauve Submit Section) are custom-painted.
Mirrors PrimaryButton's event plumbing (emits wx.EVT_BUTTON) and adds an
optional leading/trailing glyph for the arrows/checkbox icons in the reference
UI. Colors come from widgets.theme.ExamColor.
"""
import wx

from widgets import ui_scale
from widgets.theme import ExamColor


# Named ETS button kinds → (fill, hover_fill, text).
_KINDS = {
    "next":  (ExamColor.BTN_NEXT_BLUE, ExamColor.BTN_NEXT_BLUE_HOVER, ExamColor.BTN_TEXT),
    "grey":  (ExamColor.BTN_GREY, ExamColor.BTN_GREY_HOVER, ExamColor.BTN_TEXT),
    "mauve": (ExamColor.SUBMIT_MAUVE, ExamColor.SUBMIT_MAUVE_HOVER, ExamColor.BTN_TEXT),
}


class ExamButton(wx.Panel):
    """Custom-painted ETS exam button. Emits wx.EVT_BUTTON to listeners.

    kind: 'next' | 'grey' | 'mauve'.
    icon: optional glyph drawn before the label (e.g. '◀', '▶', '☐', '⬆').
    icon_after: when True the glyph trails the label (for '▶').
    """

    def __init__(self, parent, label, kind="grey", icon="", icon_after=False,
                 height=None, min_width=None):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self._label = label
        self._kind = kind if kind in _KINDS else "grey"
        self._icon = icon
        self._icon_after = icon_after
        self._hover = False
        self._pressed = False
        self._enabled = True

        self._desired_h = height if height is not None else ui_scale.space(11)
        self._min_w = min_width if min_width is not None else ui_scale.space(24)
        self.SetMinSize((self._min_w, self._desired_h))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_up)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key)

    def DoGetBestClientSize(self):  # noqa: N802 — wx idiom
        return wx.Size(self._min_w, self._desired_h)

    # ── public API ────────────────────────────────────────────────────

    def set_label(self, label, icon=None):
        self._label = label
        if icon is not None:
            self._icon = icon
        self.Refresh()

    def set_kind(self, kind):
        if kind in _KINDS:
            self._kind = kind
            self.Refresh()

    def Enable(self, enable=True):  # noqa: N802 — wx idiom
        self._enabled = bool(enable)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND if enable else wx.CURSOR_ARROW))
        self.Refresh()
        return super().Enable(enable)

    def Disable(self):  # noqa: N802
        return self.Enable(False)

    # ── event plumbing ────────────────────────────────────────────────

    def _emit_clicked(self):
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
            self._emit_clicked()

    def _on_key(self, evt):
        if self._enabled and evt.GetKeyCode() in (
                wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
            self._emit_clicked()
            return
        evt.Skip()

    # ── painting ──────────────────────────────────────────────────────

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        # transparent panel bg so corners read clean on navy/white hosts
        parent_bg = self.GetParent().GetBackgroundColour()
        gc.SetBrush(wx.Brush(parent_bg))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRectangle(0, 0, w, h)

        fill, hover, text = _KINDS[self._kind]
        if not self._enabled:
            bg, fg = ExamColor.BTN_DISABLED, ExamColor.BTN_TEXT
        elif self._pressed or self._hover:
            bg, fg = hover, text
        else:
            bg, fg = fill, text

        radius = ui_scale.space(1)
        gc.SetBrush(wx.Brush(bg))
        gc.DrawRoundedRectangle(0, 0, w, h, radius)

        font = ui_scale.exam_sans(ui_scale.EXAM_BTN_PT, wx.FONTWEIGHT_BOLD)
        gc.SetFont(font, fg)
        lbl = self._label
        if self._icon:
            lbl = f"{lbl}  {self._icon}" if self._icon_after else f"{self._icon}  {lbl}"
        tw, th = gc.GetTextExtent(lbl)
        gc.DrawText(lbl, max(ui_scale.space(2), (w - tw) // 2), (h - th) // 2)
