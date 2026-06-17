"""
Question navigation widget — the ETS GRE footer navigator (spec §3.6, §1).

Renders the 1..N questions as round numbered circles sitting on the navy
footer bar. Four visual states, with "marked" as an independent axis:

* **Current**   — blue ring (``ExamColor.NAV_CURRENT``) on white, dark number.
* **Answered**  — filled navy (``ExamColor.NAV_ANSWERED``), white number.
* **Unanswered / Skipped** — open circle (``NAV_UNANSWERED_BORDER`` outline) on
  the navy footer, light number.
* **Marked**    — an independent amber flag badge (``NAV_MARKED_BADGE``) in the
  top-right corner, layered on top of *any* of the above. A circle can be
  Answered+Marked or Unanswered+Marked.

A "Hide Progress" / "Show Progress" toggle hides or shows the circle strip
(``set_progress_hidden(bool)``).

The circle strip is owner-drawn with ``wx.GraphicsContext`` for crisp,
anti-aliased circles; clicking a circle invokes the navigate callback with the
clicked question index.

Public API (preserved for existing callers in ``screens/question_screen.py``):
    QuestionNav(parent, total_questions=0)
    .set_state(current_index, answered, marked)
    .set_on_navigate(callback)          # callback(index)
    .rebuild(total_questions)
New, additive:
    .set_progress_hidden(hidden: bool)
    .is_progress_hidden() -> bool
"""
import wx

from widgets.theme import ExamColor
from widgets import ui_scale


class _CircleStrip(wx.Panel):
    """Owner-drawn strip of round numbered navigator circles on navy.

    Internal helper; the public surface is :class:`QuestionNav`. Lays the
    circles out left-to-right, wrapping onto additional rows when the strip is
    narrower than ``total`` circles. Click hit-testing maps a point back to a
    question index and fires the parent's navigate callback.
    """

    # Geometry (base sizes; routed through ui_scale so DPI / Cmd-+/- apply).
    _BASE_DIAMETER = 26     # circle diameter at scale 1.0
    _BASE_GAP = 6           # gap between circles
    _BASE_PAD = 6           # padding around the strip

    def __init__(self, parent):
        super().__init__(parent, style=wx.FULL_REPAINT_ON_RESIZE)
        self.SetBackgroundColour(ExamColor.HEADER_NAVY)

        self.total = 0
        self.current_index = 0
        self.answered = set()
        self.marked = set()
        self._on_navigate = None

        # Cached per-circle hit rectangles, rebuilt on every paint.
        self._hit_rects = []

        # Owner-draw: avoid background erase flicker.
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    # ── Geometry helpers ──────────────────────────────────────────────
    def _diameter(self):
        return max(14, int(round(self._BASE_DIAMETER * ui_scale.scale())))

    def _gap(self):
        return max(3, int(round(self._BASE_GAP * ui_scale.scale())))

    def _pad(self):
        return max(3, int(round(self._BASE_PAD * ui_scale.scale())))

    def _cols(self, width):
        """How many circles fit per row at the current width."""
        d = self._diameter()
        gap = self._gap()
        pad = self._pad()
        usable = max(1, width - 2 * pad)
        # n circles + (n-1) gaps must fit: n*d + (n-1)*gap <= usable
        cols = (usable + gap) // (d + gap)
        return max(1, int(cols))

    def DoGetBestSize(self):  # noqa: N802 (wx override name)
        """Best size so the sizer reserves room for the rows of circles."""
        d = self._diameter()
        gap = self._gap()
        pad = self._pad()
        if self.total <= 0:
            return wx.Size(d + 2 * pad, d + 2 * pad)
        width = self.GetSize().GetWidth()
        cols = self._cols(width) if width > 0 else min(self.total, 10)
        rows = (self.total + cols - 1) // cols
        h = rows * d + (rows - 1) * gap + 2 * pad
        w = cols * d + (cols - 1) * gap + 2 * pad
        return wx.Size(w, h)

    def _layout_rects(self, width):
        """Compute the (index, wx.Rect) list for the current width."""
        rects = []
        if self.total <= 0:
            return rects
        d = self._diameter()
        gap = self._gap()
        pad = self._pad()
        cols = self._cols(width)
        for i in range(self.total):
            row = i // cols
            col = i % cols
            x = pad + col * (d + gap)
            y = pad + row * (d + gap)
            rects.append((i, wx.Rect(x, y, d, d)))
        return rects

    # ── State ─────────────────────────────────────────────────────────
    def set_state(self, current_index, answered, marked):
        self.current_index = current_index
        self.answered = set(answered)
        self.marked = set(marked)
        self.Refresh()

    def set_total(self, total):
        self.total = max(0, int(total))
        self.current_index = 0
        self.answered = set()
        self.marked = set()
        self.InvalidateBestSize()
        self.Refresh()

    def set_on_navigate(self, callback):
        self._on_navigate = callback

    # ── Painting ──────────────────────────────────────────────────────
    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(ExamColor.HEADER_NAVY))
        dc.Clear()

        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return

        width = self.GetSize().GetWidth()
        self._hit_rects = self._layout_rects(width)

        d = self._diameter()
        ring_w = max(2, int(round(2 * ui_scale.scale())))
        num_font = ui_scale.exam_sans(ui_scale.EXAM_NAV_PT,
                                      weight=wx.FONTWEIGHT_BOLD)

        for i, rect in self._hit_rects:
            self._draw_circle(gc, rect, i, d, ring_w, num_font)

    def _draw_circle(self, gc, rect, index, diameter, ring_w, num_font):
        """Draw one navigator circle in its state, plus a marked badge."""
        x, y = rect.x, rect.y
        is_current = (index == self.current_index)
        is_answered = (index in self.answered)
        is_marked = (index in self.marked)

        if is_current:
            # White fill + blue ring; dark number.
            fill = ExamColor.CONTENT_BG
            border = ExamColor.NAV_CURRENT
            text_color = ExamColor.TEXT
            border_w = ring_w
        elif is_answered:
            # Filled navy; white number. Light outline keeps it visible on the
            # same-navy footer.
            fill = ExamColor.NAV_ANSWERED
            border = ExamColor.NAV_UNANSWERED_BORDER
            text_color = ExamColor.TEXT_ON_NAVY
            border_w = 1
        else:
            # Open circle on navy; light number.
            fill = ExamColor.HEADER_NAVY
            border = ExamColor.NAV_UNANSWERED_BORDER
            text_color = ExamColor.TEXT_ON_NAVY
            border_w = 1

        gc.SetBrush(wx.Brush(fill))
        gc.SetPen(wx.Pen(border, border_w))
        # Inset by half the pen width so the stroke stays inside the rect.
        inset = border_w / 2.0
        gc.DrawEllipse(x + inset, y + inset,
                       diameter - 2 * inset, diameter - 2 * inset)

        # Centered number.
        gc.SetFont(num_font, text_color)
        label = str(index + 1)
        tw, th = gc.GetTextExtent(label)[:2]
        gc.DrawText(label, x + (diameter - tw) / 2.0,
                    y + (diameter - th) / 2.0)

        # Marked badge — independent amber dot in the top-right corner,
        # layered on top of whatever base state the circle has.
        if is_marked:
            self._draw_marked_badge(gc, rect, diameter)

    def _draw_marked_badge(self, gc, rect, diameter):
        """Amber corner badge marking the question as flagged (independent
        of answered/current state)."""
        badge_d = max(7, int(round(diameter * 0.42)))
        bx = rect.x + diameter - badge_d
        by = rect.y
        gc.SetBrush(wx.Brush(ExamColor.NAV_MARKED_BADGE))
        # Thin white ring so the amber badge reads on navy and on the navy
        # answered fill alike.
        gc.SetPen(wx.Pen(ExamColor.TEXT_ON_NAVY, 1))
        gc.DrawEllipse(bx, by, badge_d, badge_d)

    # ── Hit testing ───────────────────────────────────────────────────
    def _on_left_up(self, event):
        pt = event.GetPosition()
        for i, rect in self._hit_rects:
            if rect.Contains(pt):
                if self._on_navigate:
                    self._on_navigate(i)
                return

    def index_at(self, point):
        """Return the question index under ``point`` (a wx.Point or (x, y)),
        or ``None``. Exposed for tests and programmatic hit-testing.

        Uses the most recently painted layout; if the strip has not painted
        yet (e.g. headless tests on a hidden frame) it computes the layout
        on demand from the current width.
        """
        if not self._hit_rects:
            self._hit_rects = self._layout_rects(self.GetSize().GetWidth())
        pt = wx.Point(point[0], point[1]) if not isinstance(point, wx.Point) \
            else point
        for i, rect in self._hit_rects:
            if rect.Contains(pt):
                return i
        return None

    def rect_for(self, index):
        """Return the wx.Rect of circle ``index`` in this strip's coordinates,
        or ``None``. Exposed for tests / click simulation."""
        if not self._hit_rects:
            self._hit_rects = self._layout_rects(self.GetSize().GetWidth())
        for i, rect in self._hit_rects:
            if i == index:
                return rect
        return None


class QuestionNav(wx.Panel):
    """Question navigation footer showing answered / marked / current status.

    Used at the bottom of exam sections (and re-used by the review screen). The
    1..N questions render as round numbered circles on the navy footer bar with
    a "Hide Progress" toggle to collapse the strip.

    Public API (stable — see module docstring): ``set_state``,
    ``set_on_navigate``, ``rebuild`` (preserved); ``set_progress_hidden`` /
    ``is_progress_hidden`` (new).
    """

    # ── Legacy status colours (preserved public attributes) ───────────
    # Kept for backward compatibility; the navy-footer renderer reads from
    # ``ExamColor`` (spec §1). No external caller references these, but they
    # were part of the widget's public surface before the ETS re-skin.
    CLR_DEFAULT = wx.Colour(230, 230, 230)
    CLR_CURRENT = wx.Colour(100, 149, 237)          # cornflower blue
    CLR_ANSWERED = wx.Colour(144, 238, 144)         # light green
    CLR_MARKED = wx.Colour(255, 200, 100)           # orange
    CLR_MARKED_ANSWERED = wx.Colour(255, 165, 0)    # darker orange

    def __init__(self, parent, total_questions=0):
        super().__init__(parent)
        self.SetBackgroundColour(ExamColor.HEADER_NAVY)

        self.total = total_questions
        self.current_index = 0
        self.answered = set()
        self.marked = set()
        self._on_navigate = None
        self._progress_hidden = False

        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────
    def _build_ui(self):
        # Toggle row: the "Hide Progress" / "Show Progress" button.
        self.toggle_btn = wx.Button(self, label="Hide Progress",
                                    style=wx.BU_EXACTFIT)
        self.toggle_btn.SetFont(ui_scale.exam_sans(ui_scale.EXAM_BTN_PT))
        self.toggle_btn.Bind(wx.EVT_BUTTON, self._on_toggle)

        toggle_sizer = wx.BoxSizer(wx.HORIZONTAL)
        toggle_sizer.Add(self.toggle_btn, 0, wx.ALIGN_CENTER_VERTICAL)

        # Owner-drawn circle strip.
        self.strip = _CircleStrip(self)
        self.strip.set_total(self.total)
        self.strip.set_on_navigate(self._on_click)

        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(toggle_sizer, 0, wx.ALL, ui_scale.space(1))
        main_sizer.Add(self.strip, 1, wx.EXPAND | wx.LEFT | wx.RIGHT |
                       wx.BOTTOM, ui_scale.space(1))
        self.SetSizer(main_sizer)

        self._apply_progress_hidden()

    # ── Public API (preserved) ────────────────────────────────────────
    def set_state(self, current_index, answered, marked):
        """Update the navigation state and refresh.

        ``answered`` and ``marked`` are iterables of question indices.
        ``marked`` is an independent axis — a question may be in both
        ``answered`` and ``marked`` (or in ``marked`` alone).
        """
        self.current_index = current_index
        self.answered = set(answered)
        self.marked = set(marked)
        self.strip.set_state(current_index, self.answered, self.marked)

    def set_on_navigate(self, callback):
        """Set callback invoked with the clicked question index. callback(index)."""
        self._on_navigate = callback

    def rebuild(self, total_questions):
        """Rebuild for a new question count, resetting state."""
        self.total = total_questions
        self.current_index = 0
        self.answered = set()
        self.marked = set()
        self.strip.set_total(total_questions)
        self.Layout()

    # ── Public API (new, additive) ────────────────────────────────────
    def set_progress_hidden(self, hidden):
        """Hide or show the circle strip (the "Hide Progress" toggle).

        When hidden, the strip is collapsed and the toggle reads
        "Show Progress"; when shown it reads "Hide Progress".
        """
        self._progress_hidden = bool(hidden)
        self._apply_progress_hidden()

    def is_progress_hidden(self):
        """Return whether the circle strip is currently hidden."""
        return self._progress_hidden

    # ── Internals ─────────────────────────────────────────────────────
    def _apply_progress_hidden(self):
        self.strip.Show(not self._progress_hidden)
        self.toggle_btn.SetLabel(
            "Show Progress" if self._progress_hidden else "Hide Progress")
        self.Layout()

    def _on_toggle(self, event):
        self.set_progress_hidden(not self._progress_hidden)

    def _on_click(self, index):
        if self._on_navigate:
            self._on_navigate(index)
