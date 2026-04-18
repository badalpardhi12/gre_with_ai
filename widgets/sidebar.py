"""
Persistent left-rail navigation. Five top-level sections + streak badge +
settings cog. The active tab is highlighted; clicking a tab fires a
callback registered via `set_on_select`.

The sidebar is custom-painted (`wx.PaintDC`) instead of `wx.Notebook` because
wxPython's tab controls render inconsistently in dark mode on macOS, and
because we want the streak badge / settings cog at the bottom rail (not
something a Notebook supports).
"""
from typing import Callable, List, Optional

import wx

from widgets import ui_scale
from widgets.theme import Color


class Sidebar(wx.Panel):
    """Vertical nav rail.

    Tabs are dicts: {"id": str, "label": str, "icon": str}.
    The icon is a unicode glyph (emoji or single character).
    """

    DEFAULT_TABS = [
        {"id": "today",    "label": "Today",    "icon": "◐"},
        {"id": "learn",    "label": "Learn",    "icon": "📖"},
        {"id": "practice", "label": "Practice", "icon": "✎"},
        {"id": "vocab",    "label": "Vocab",    "icon": "Aa"},
        {"id": "insights", "label": "Insights", "icon": "▦"},
    ]

    SETTINGS_ID = "__settings__"

    def __init__(self, parent, tabs: Optional[List[dict]] = None,
                 width: Optional[int] = None):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self.SetBackgroundColour(Color.BG_SURFACE)
        self._tabs = tabs or self.DEFAULT_TABS
        self._active_id = self._tabs[0]["id"]
        self._hover_id = None
        self._on_select: Optional[Callable[[str], None]] = None
        self._streak_text = ""

        rail_width = width if width is not None else ui_scale.font_size(200)
        self.SetMinSize((rail_width, -1))
        self.SetSize((rail_width, -1))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetCursor(wx.Cursor(wx.CURSOR_ARROW))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_UP, self._on_click)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_SIZE, lambda _: self.Refresh())

    # ── public API ────────────────────────────────────────────────────

    def set_on_select(self, cb: Callable[[str], None]):
        self._on_select = cb

    def set_active(self, tab_id: str):
        if tab_id == self._active_id:
            return
        self._active_id = tab_id
        self.Refresh()

    def set_streak(self, text: str):
        """E.g. "🔥 12-day streak". Empty string hides the badge."""
        self._streak_text = text
        self.Refresh()

    @property
    def active_id(self) -> str:
        return self._active_id

    # ── geometry ──────────────────────────────────────────────────────

    def _row_height(self) -> int:
        return ui_scale.space(11)

    def _header_height(self) -> int:
        return ui_scale.space(15)

    def _bottom_height(self) -> int:
        return ui_scale.space(20)  # streak badge + settings cog

    def _hit_test(self, y: int) -> Optional[str]:
        """Return tab id (or SETTINGS_ID) under y, or None."""
        rh = self._row_height()
        top = self._header_height()
        for i, tab in enumerate(self._tabs):
            row_top = top + i * rh
            if row_top <= y < row_top + rh:
                return tab["id"]
        # Bottom region: settings cog
        client_h = self.GetClientSize().GetHeight()
        cog_top = client_h - ui_scale.space(11)
        if cog_top <= y <= client_h:
            return self.SETTINGS_ID
        return None

    # ── events ────────────────────────────────────────────────────────

    def _on_motion(self, evt):
        new_hover = self._hit_test(evt.GetY())
        if new_hover != self._hover_id:
            self._hover_id = new_hover
            self.Refresh()

    def _on_leave(self, _):
        if self._hover_id is not None:
            self._hover_id = None
            self.Refresh()

    def _on_click(self, evt):
        target = self._hit_test(evt.GetY())
        if target is None:
            return
        if target == self.SETTINGS_ID:
            if self._on_select:
                self._on_select(self.SETTINGS_ID)
            return
        self.set_active(target)
        if self._on_select:
            self._on_select(target)

    # ── painting ──────────────────────────────────────────────────────

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(Color.BG_SURFACE))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()

        # ── Header: app title ─────────────────────────────────────────
        title_font = wx.Font(ui_scale.text_lg(), wx.FONTFAMILY_DEFAULT,
                             wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        gc.SetFont(title_font, Color.TEXT_PRIMARY)
        gc.DrawText("GRE prep", ui_scale.space(5), ui_scale.space(6))
        sub_font = wx.Font(ui_scale.text_xs(), wx.FONTFAMILY_DEFAULT,
                           wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL)
        gc.SetFont(sub_font, Color.TEXT_TERTIARY)
        gc.DrawText("with AI tutoring", ui_scale.space(5),
                    ui_scale.space(6) + ui_scale.text_lg() + ui_scale.space(1))

        # ── Tab rows ──────────────────────────────────────────────────
        rh = self._row_height()
        top = self._header_height()
        row_font = wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                           wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        active_font = wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                              wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)

        for i, tab in enumerate(self._tabs):
            y = top + i * rh
            is_active = tab["id"] == self._active_id
            is_hover = tab["id"] == self._hover_id and not is_active

            if is_active:
                # Highlighted background bar with accent stripe.
                gc.SetBrush(wx.Brush(Color.BG_ELEVATED))
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.DrawRectangle(0, y, w, rh)
                gc.SetBrush(wx.Brush(Color.ACCENT))
                gc.DrawRectangle(0, y, ui_scale.space(1), rh)
            elif is_hover:
                gc.SetBrush(wx.Brush(Color.BG_ELEVATED))
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.DrawRectangle(0, y, w, rh)

            text_color = Color.TEXT_PRIMARY if is_active else Color.TEXT_SECONDARY
            gc.SetFont(active_font if is_active else row_font, text_color)
            icon_x = ui_scale.space(5)
            label_x = icon_x + ui_scale.space(7)
            tw, th = gc.GetTextExtent(tab["label"])
            ty = y + (rh - th) // 2
            gc.DrawText(tab["icon"], icon_x, ty)
            gc.DrawText(tab["label"], label_x, ty)

        # ── Bottom rail: streak + settings ────────────────────────────
        # Streak badge (~24px tall band) — truncated to keep within rail width.
        if self._streak_text:
            badge_y = h - ui_scale.space(20)
            badge_font = wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                 wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
            gc.SetFont(badge_font, Color.STREAK)
            label = self._streak_text
            if len(label) > 18:
                label = label[:17] + "…"
            gc.DrawText(label, ui_scale.space(5), badge_y)

        # Settings cog
        cog_y = h - ui_scale.space(11)
        is_hover = self._hover_id == self.SETTINGS_ID
        if is_hover:
            gc.SetBrush(wx.Brush(Color.BG_ELEVATED))
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.DrawRectangle(0, cog_y, w, ui_scale.space(11))
        cog_font = wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                           wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        gc.SetFont(cog_font,
                   Color.TEXT_PRIMARY if is_hover else Color.TEXT_SECONDARY)
        gc.DrawText("⚙  Settings", ui_scale.space(5),
                    cog_y + (ui_scale.space(11) - ui_scale.text_md()) // 2)
