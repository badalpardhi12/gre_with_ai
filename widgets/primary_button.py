"""
Primary call-to-action button: filled accent surface, white text, generous
padding. Used for the single primary action on each screen ("Continue
today's session", "Start drill", "Submit essay").

A real wx.Button on macOS dark mode renders inconsistently. We custom-paint
to keep visuals identical across platforms.
"""
import wx

from widgets import ui_scale
from widgets.theme import Color


class PrimaryButton(wx.Panel):
    """Custom-painted accent button. Emits wx.EVT_BUTTON to listeners."""

    def __init__(self, parent, label: str, height=None,
                 subtitle: str = "",
                 icon: str = ""):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self._label = label
        self._subtitle = subtitle
        self._icon = icon
        self._hover = False
        self._pressed = False
        self._enabled = True

        h = height if height is not None else ui_scale.space(14)
        if subtitle:
            h = max(h, ui_scale.space(18))
        # Authoritative height — DoGetBestClientSize returns this so a later
        # SetMinSize((w, -1)) call can't accidentally clobber it.
        self._desired_h = h
        self.SetMinSize((-1, h))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_up)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key)

    def DoGetBestClientSize(self):  # noqa: N802 — wx idiom
        return wx.Size(-1, self._desired_h)

    # ── public API ────────────────────────────────────────────────────

    def set_label(self, label: str, subtitle: str = ""):
        self._label = label
        self._subtitle = subtitle
        self.Refresh()

    def Enable(self, enable: bool = True):  # noqa: N802 — wx idiom
        self._enabled = bool(enable)
        self.Refresh()
        return super().Enable(enable)

    Disable = lambda self: self.Enable(False)  # noqa: E731

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
        if not self._enabled:
            return
        if evt.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
            self._emit_clicked()
            return
        evt.Skip()

    # ── painting ──────────────────────────────────────────────────────

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()

        if not self._enabled:
            bg = Color.BG_ELEVATED
            fg = Color.TEXT_TERTIARY
        elif self._pressed:
            bg = Color.ACCENT_DARK
            fg = Color.TEXT_INVERSE
        elif self._hover:
            # Slightly brighter than the resting accent.
            bg = Color.ACCENT
            fg = Color.TEXT_INVERSE
        else:
            bg = Color.ACCENT
            fg = Color.TEXT_INVERSE

        radius = ui_scale.space(2)
        gc.SetBrush(wx.Brush(bg))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(0, 0, w, h, radius)

        # Label
        lbl = (self._icon + "  " + self._label) if self._icon else self._label
        title_font = wx.Font(ui_scale.text_lg(), wx.FONTFAMILY_DEFAULT,
                             wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        gc.SetFont(title_font, fg)
        # Available text width — leave a side margin on each end.
        text_max_w = max(0, w - 2 * ui_scale.space(5))
        lbl = self._fit(gc, lbl, text_max_w)
        tw, th = gc.GetTextExtent(lbl)
        if self._subtitle:
            sub_font = wx.Font(ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
                               wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
            gc.SetFont(sub_font, fg)
            sub = self._fit(gc, self._subtitle, text_max_w)
            sw, sh = gc.GetTextExtent(sub)
            gc.SetFont(title_font, fg)
            block_h = th + ui_scale.space(1) + sh
            # If the requested height can't fit the two-line block, drop
            # the subtitle to keep the primary label readable.
            if block_h > h - ui_scale.space(2):
                x = ui_scale.space(5)
                y = (h - th) // 2
                gc.DrawText(lbl, x, y)
                return
            x_title = ui_scale.space(5)
            y_title = (h - block_h) // 2
            gc.DrawText(lbl, x_title, y_title)
            gc.SetFont(sub_font, fg)
            gc.DrawText(sub, x_title, y_title + th + ui_scale.space(1))
        else:
            x = ui_scale.space(5)
            y = (h - th) // 2
            gc.DrawText(lbl, x, y)

    @staticmethod
    def _fit(gc, text: str, max_w: int) -> str:
        """Truncate `text` with an ellipsis so it fits in `max_w` pixels."""
        if max_w <= 0 or not text:
            return text
        tw, _ = gc.GetTextExtent(text)
        if tw <= max_w:
            return text
        ell = "…"
        ell_w, _ = gc.GetTextExtent(ell)
        if ell_w >= max_w:
            return ""
        # Binary trim — much faster than per-char in worst case.
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if gc.GetTextExtent(text[:mid] + ell)[0] <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + ell
