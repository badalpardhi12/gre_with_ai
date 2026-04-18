"""
Mastery heatmap — visual grid of all subtopics, color-coded by mastery band.

Cell layout: cells are grouped by topic (Algebra, Geometry, …); within a topic
they're rendered as a horizontal flow of fixed-size colored rectangles.
Click a cell → fires `EVT_HEATMAP_SELECT` with the subtopic id in
`evt.GetString()`.

This is custom-painted because (a) we want exact control over layout and
hover state, and (b) wxPython's grid controls don't offer per-cell color +
hit-testing in a clean way.
"""
from typing import Callable, Dict, List, Optional, Tuple

import wx
import wx.lib.newevent

from models.taxonomy import (
    QUANT_TAXONOMY, VERBAL_TAXONOMY, subtopic_display_name,
)
from widgets import ui_scale
from widgets.theme import Color, mastery_color


HeatmapSelectEvent, EVT_HEATMAP_SELECT = wx.lib.newevent.NewCommandEvent()


# Filter chip ids
FILTER_ALL = "all"
FILTER_WEAK = "weak"
FILTER_MASTERED = "mastered"
FILTER_NEW = "new"


class MasteryHeatmap(wx.ScrolledWindow):
    """Vertical-scrolling heatmap. `set_data(subtopic_summary_dict)` to populate."""

    # Cell layout — denser, cleaner per Apple HIG (less chrome per cell, more
    # whitespace between groups, stronger section/topic hierarchy).
    CELL_W = 152
    CELL_H = 60
    CELL_GAP = 10
    GROUP_GAP = 28
    SECTION_GAP = 36
    TOPIC_HEADER_H = 24
    SECTION_HEADER_H = 36

    def __init__(self, parent):
        super().__init__(parent, style=wx.VSCROLL)
        self.SetBackgroundColour(Color.BG_PAGE)
        self.SetScrollRate(0, 14)
        self._data: Dict[str, dict] = {}
        self._filter = FILTER_ALL
        self._selected: Optional[str] = None
        self._hover: Optional[str] = None
        self._cells: List[Tuple[wx.Rect, str]] = []  # (rect, subtopic_id)
        self._last_layout_width: int = 0  # so first real width triggers reflow

        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_UP, self._on_click)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_SIZE, self._on_size)

    # ── public API ────────────────────────────────────────────────────

    def set_data(self, summary: Dict[str, dict]):
        """`summary[subtopic] = {question_count, mastery, attempts, has_lesson}`."""
        self._data = summary or {}
        self._relayout_and_refresh()
        # If the first set_data ran before the parent gave us a real width,
        # reflow on the next idle tick when GetClientSize will be correct.
        if self.GetClientSize().GetWidth() < 100:
            wx.CallAfter(self._relayout_and_refresh)

    def _on_size(self, _):
        # Always reflow on resize — the previous width may have been wrong
        # (e.g. zero on first show before splitter measured us), or the
        # window may have grown / shrunk and the per-row count changed.
        self._relayout_and_refresh()

    def set_filter(self, f: str):
        if f == self._filter:
            return
        self._filter = f
        self._relayout_and_refresh()

    def set_selected(self, subtopic: Optional[str]):
        self._selected = subtopic
        self.Refresh()

    @property
    def selected(self) -> Optional[str]:
        return self._selected

    # ── layout ────────────────────────────────────────────────────────

    def _relayout_and_refresh(self):
        self._compute_cells()
        # Set virtual size so the scrollbar appears when needed.
        if self._cells:
            max_y = max(rect.GetBottom() for rect, _ in self._cells) + 24
        else:
            max_y = self.GetClientSize().GetHeight()
        self.SetVirtualSize((-1, max_y))
        self.Refresh()

    def _passes_filter(self, info: dict) -> bool:
        if self._filter == FILTER_ALL:
            return True
        if self._filter == FILTER_NEW:
            return info.get("attempts", 0) == 0
        m = info.get("mastery") or 0
        if self._filter == FILTER_WEAK:
            return info.get("attempts", 0) > 0 and m < 0.6
        if self._filter == FILTER_MASTERED:
            return m >= 0.8 and info.get("attempts", 0) > 0
        return True

    def _compute_cells(self):
        self._cells = []
        cw = ui_scale.font_size(self.CELL_W)
        ch = ui_scale.font_size(self.CELL_H)
        gap = ui_scale.font_size(self.CELL_GAP)
        group_gap = ui_scale.font_size(self.GROUP_GAP)
        section_gap = ui_scale.font_size(self.SECTION_GAP)
        topic_h = ui_scale.font_size(self.TOPIC_HEADER_H)
        section_h = ui_scale.font_size(self.SECTION_HEADER_H)

        client_w = self.GetClientSize().GetWidth() or ui_scale.font_size(560)
        margin = ui_scale.space(4)
        per_row = max(1, (client_w - 2 * margin + gap) // (cw + gap))

        y = margin
        # Render Quant block then Verbal block.
        for taxonomy_label, taxonomy in (
            ("Quantitative", QUANT_TAXONOMY),
            ("Verbal", VERBAL_TAXONOMY),
        ):
            section_started = False
            for topic_id, td in taxonomy.items():
                topic_label = td.get("display_name", topic_id)
                subs = list(td.get("subtopics", {}).keys())
                visible_subs = [
                    s for s in subs
                    if self._passes_filter(self._data.get(s, {
                        "attempts": 0, "mastery": None,
                    }))
                ]
                if not visible_subs:
                    continue
                if not section_started:
                    section_started = True
                    # Section heading (Quantitative / Verbal)
                    if y > margin:
                        y += section_gap
                    self._cells.append((wx.Rect(margin, y, client_w - 2 * margin,
                                                section_h),
                                        f"__section__:{taxonomy_label}"))
                    y += section_h + ui_scale.space(1)
                else:
                    y += group_gap
                # Topic header
                self._cells.append((wx.Rect(margin, y, client_w - 2 * margin,
                                            topic_h),
                                    f"__topic__:{topic_label}"))
                y += topic_h

                col = 0
                for sub_id in visible_subs:
                    x = margin + col * (cw + gap)
                    self._cells.append((wx.Rect(x, y, cw, ch), sub_id))
                    col += 1
                    if col >= per_row:
                        col = 0
                        y += ch + gap
                if col != 0:
                    y += ch + gap
        self._content_h = y

    # ── events ────────────────────────────────────────────────────────

    def _on_motion(self, evt):
        pos = self.CalcUnscrolledPosition(evt.GetPosition())
        new_hover = None
        for rect, sub_id in self._cells:
            if sub_id.startswith("__"):
                continue
            if rect.Contains(pos):
                new_hover = sub_id
                break
        if new_hover != self._hover:
            self._hover = new_hover
            self.Refresh()

    def _on_leave(self, _):
        if self._hover is not None:
            self._hover = None
            self.Refresh()

    def _on_click(self, evt):
        pos = self.CalcUnscrolledPosition(evt.GetPosition())
        for rect, sub_id in self._cells:
            if sub_id.startswith("__"):
                continue
            if rect.Contains(pos):
                self._selected = sub_id
                self.Refresh()
                e = HeatmapSelectEvent(self.GetId())
                e.SetEventObject(self)
                e.SetString(sub_id)
                wx.PostEvent(self, e)
                return

    # ── painting ──────────────────────────────────────────────────────

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(Color.BG_PAGE))
        dc.Clear()
        self.PrepareDC(dc)
        gc = wx.GraphicsContext.Create(dc)

        section_font = wx.Font(ui_scale.text_xl(), wx.FONTFAMILY_DEFAULT,
                               wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        topic_font = wx.Font(ui_scale.text_xs(), wx.FONTFAMILY_DEFAULT,
                             wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        sub_font = wx.Font(ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
                           wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        meta_font = wx.Font(ui_scale.text_xs(), wx.FONTFAMILY_DEFAULT,
                            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)

        for rect, sub_id in self._cells:
            if sub_id.startswith("__section__:"):
                section = sub_id.split(":", 1)[1]
                gc.SetFont(section_font, Color.TEXT_PRIMARY)
                gc.DrawText(section, rect.x, rect.y)
                # Subtle underline for the section header.
                line_y = rect.y + rect.height - ui_scale.space(1)
                gc.SetPen(wx.Pen(Color.BORDER, 1))
                gc.StrokeLine(rect.x, line_y,
                              rect.x + rect.width, line_y)
            elif sub_id.startswith("__topic__:"):
                topic = sub_id.split(":", 1)[1]
                gc.SetFont(topic_font, Color.TEXT_TERTIARY)
                gc.DrawText(topic.upper(), rect.x, rect.y +
                            (rect.height - ui_scale.text_xs()) // 2)
            else:
                self._paint_cell(gc, rect, sub_id, sub_font, meta_font)

    def _paint_cell(self, gc, rect, sub_id, sub_font, meta_font):
        info = self._data.get(sub_id, {
            "mastery": None, "attempts": 0, "question_count": 0,
            "has_lesson": False,
        })
        m = info.get("mastery") or 0
        attempts = info.get("attempts", 0)
        is_selected = sub_id == self._selected
        is_hover = sub_id == self._hover

        # Background: subtle surface; the mastery indicator lives at the
        # bottom edge as a thin colored bar (Apple-style progress hint).
        radius = ui_scale.space(2)
        gc.SetBrush(wx.Brush(Color.BG_SURFACE))
        if is_selected:
            gc.SetPen(wx.Pen(Color.ACCENT, 2))
        elif is_hover:
            gc.SetPen(wx.Pen(Color.BORDER_STRONG, 1))
        else:
            gc.SetPen(wx.Pen(Color.BORDER, 1))
        gc.DrawRoundedRectangle(rect.x, rect.y, rect.width,
                                rect.height, radius)

        # Title — pretty display name from the taxonomy (fall back to a
        # Title-cased id for orphan subtopics).
        display = subtopic_display_name(sub_id)
        gc.SetFont(sub_font, Color.TEXT_PRIMARY)
        title_y = rect.y + ui_scale.space(2)
        title_x = rect.x + ui_scale.space(2)
        max_w = rect.width - 2 * ui_scale.space(2) - ui_scale.space(3)
        line1, line2 = self._wrap_two_lines(gc, display, max_w)
        gc.DrawText(line1, title_x, title_y)
        if line2:
            gc.DrawText(line2, title_x,
                        title_y + ui_scale.text_sm() + ui_scale.space(0))

        # Lesson glyph in the top-right corner.
        if info.get("has_lesson"):
            gc.SetFont(meta_font, Color.TEXT_TERTIARY)
            glyph_w, _ = gc.GetTextExtent("📖")
            gc.DrawText("📖", rect.x + rect.width - glyph_w - ui_scale.space(2),
                        rect.y + ui_scale.space(1))

        # Bottom area: meta line + mastery bar.
        meta_text = self._meta_text(attempts, m, info.get('question_count', 0))
        gc.SetFont(meta_font, Color.TEXT_SECONDARY)
        meta_y = rect.y + rect.height - ui_scale.text_xs() \
            - ui_scale.space(3)
        gc.DrawText(meta_text, rect.x + ui_scale.space(2), meta_y)

        # Thin mastery progress bar pinned to the bottom edge.
        bar_h = ui_scale.space(1)
        bar_y = rect.y + rect.height - bar_h
        gc.SetBrush(wx.Brush(Color.BG_ELEVATED))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRectangle(rect.x, bar_y, rect.width, bar_h)
        if attempts > 0:
            gc.SetBrush(wx.Brush(mastery_color(m, attempts)))
            gc.DrawRectangle(rect.x, bar_y, max(2, int(rect.width * m)),
                             bar_h)

    @staticmethod
    def _wrap_two_lines(gc, text: str, max_w: int):
        """Greedy two-line wrap; truncates the second line with an ellipsis."""
        words = text.split()
        line1 = ""
        i = 0
        while i < len(words):
            candidate = (line1 + " " + words[i]).strip()
            w, _ = gc.GetTextExtent(candidate)
            if w > max_w and line1:
                break
            line1 = candidate
            i += 1
        if i >= len(words):
            return line1, ""
        line2 = " ".join(words[i:])
        # Truncate line 2 if too wide.
        while line2 and gc.GetTextExtent(line2 + "…")[0] > max_w:
            line2 = line2[:-1].rstrip()
        if i < len(words):
            line2 = (line2 + "…") if line2 else words[i][:8] + "…"
        return line1, line2

    @staticmethod
    def _meta_text(attempts: int, mastery: float, q_count: int) -> str:
        if attempts == 0:
            return f"New · {q_count} qs"
        return f"{int(mastery * 100)}% · {q_count} qs"
