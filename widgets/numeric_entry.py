"""
Numeric entry widget — ETS GRE Numeric Entry (spec §5.5).

Two presentations, selected by ``fraction_mode`` at construction time:

* **Single value** — one clean white answer box (``wx.TextCtrl``); the user
  types an integer or a decimal via the keyboard.
* **Fraction** — TWO boxes **stacked** vertically (numerator on top,
  denominator below) separated by a drawn horizontal fraction bar, mirroring
  the official ETS fraction control. (Older revisions laid these side-by-side
  with a literal ``/`` glyph; the stacked form is the faithful one.)

A per-question unit/currency label may sit adjacent to the box: a ``$`` to the
**left** (prefix) and/or a unit word such as ``feet`` to the **right**
(suffix). The user still types only the number; the labels are decoration.

Input is restricted at the keystroke level: only digits, a single leading
minus, and a single decimal point are accepted into a box. Symbols the ETS UI
forbids (``%``, ``$``, ``/``, commas, whitespace, ``e``/``E``) are rejected as
typed, in addition to the value-level validation in ``get_response``.

Public API (consumed by ``screens/question_screen.py`` and, via the response
dict shape, by ``screens/answer_review_dialog.py``):

* ``NumericEntry(parent, fraction_mode=False, prefix=None, suffix=None)``
* ``fraction_mode`` attribute
* ``get_response()`` -> ``{"value": str}`` | ``{"numerator": int, "denominator": int}`` | ``{}``
* ``set_response(payload)``
* ``clear()``
* ``set_on_change(callback)``
* ``set_unit(prefix=None, suffix=None)``  (new; optional per-question label)
"""
import math
import re

import wx

from widgets import ui_scale
from widgets.theme import ExamColor


# Allow optional sign + digits only — no e/E, decimal points, or whitespace.
_INT_LITERAL = re.compile(r"^[+-]?\d+$")

# Characters accepted into the boxes as typed (numerator/denominator boxes drop
# the decimal point — fractions are integer/integer).
_ALLOWED_DECIMAL = set("0123456789.-")
_ALLOWED_INTEGER = set("0123456789-")


def _is_int_literal(s: str) -> bool:
    return bool(_INT_LITERAL.match(s))


def _would_be_valid_decimal(current: str, sel_start: int, sel_end: int,
                            insert: str) -> bool:
    """Return True if inserting ``insert`` (replacing the current selection)
    yields a string that is still a *prefix* of a valid signed decimal.

    Permits intermediate states a user passes through while typing (``-``,
    ``-.``, ``.5``) but rejects a second sign, a second decimal point, or a
    sign anywhere but the first position.
    """
    candidate = current[:sel_start] + insert + current[sel_end:]
    if candidate == "":
        return True
    if candidate.count("-") > 1:
        return False
    if "-" in candidate and not candidate.startswith("-"):
        return False
    if candidate.count(".") > 1:
        return False
    # Strip the allowed sign/point; everything left must be a digit.
    body = candidate.lstrip("-").replace(".", "", 1)
    return body == "" or body.isdigit()


def _would_be_valid_integer(current: str, sel_start: int, sel_end: int,
                            insert: str) -> bool:
    """Like :func:`_would_be_valid_decimal` but for integer boxes (num/den):
    no decimal point allowed."""
    candidate = current[:sel_start] + insert + current[sel_end:]
    if candidate == "":
        return True
    if candidate.count("-") > 1:
        return False
    if "-" in candidate and not candidate.startswith("-"):
        return False
    body = candidate.lstrip("-")
    return body == "" or body.isdigit()


class _FractionBar(wx.Window):
    """Owner-drawn horizontal rule that separates the stacked numerator and
    denominator boxes, emulating the ETS fraction bar."""

    def __init__(self, parent):
        super().__init__(parent, size=(-1, max(3, ui_scale.space(1))))
        self.SetBackgroundColour(ExamColor.CONTENT_BG)
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def _on_paint(self, _event):
        dc = wx.PaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(ExamColor.CONTENT_BG))
        dc.Clear()
        pen = wx.Pen(ExamColor.TEXT, max(2, ui_scale.font_size(2)))
        dc.SetPen(pen)
        y = h // 2
        dc.DrawLine(0, y, w, y)


class NumericEntry(wx.Panel):
    """Numeric Entry input field(s) for GRE quantitative questions.

    ``fraction_mode=False`` builds a single decimal box; ``True`` builds the
    stacked numerator-over-denominator fraction control.
    """

    def __init__(self, parent, fraction_mode=False, prefix=None, suffix=None):
        super().__init__(parent)
        self.fraction_mode = fraction_mode
        self._on_change = None
        self._prefix = prefix
        self._suffix = suffix
        # Populated by the builders so set_unit() can show/hide them.
        self._prefix_label = None
        self._suffix_label = None

        self.SetBackgroundColour(ExamColor.CONTENT_BG)

        if fraction_mode:
            self._build_fraction_ui()
        else:
            self._build_decimal_ui()

    # ── Construction helpers ──────────────────────────────────────────
    def _make_box(self, size, allowed_chars, validator_fn):
        """Build a visibly WHITE answer box with DARK text and keystroke
        filtering.

        On macOS Dark appearance a bare ``wx.TextCtrl`` ignores
        ``SetBackgroundColour`` and renders with the OS dark field chrome (a dark
        rectangle), so the ETS white answer box looks wrong. We can't owner-draw
        the *editable* text (real typing needs a native ``wx.TextCtrl``), but we
        CAN owner-draw the field's background: the wrapper ``wx.Panel`` paints
        its own solid-white rectangle + 1px ``ExamColor.OVAL_BORDER`` frame in an
        ``EVT_PAINT`` handler, which is the only dark-mode-proof way to guarantee
        white (``SetBackgroundColour`` alone is ignored by the OS field/panel
        chrome). The borderless ``wx.TextCtrl`` sits on top of that white field
        with black text.

        Two failure modes are fixed here:

        * **Collapsed height.** A panel-with-sizer reports its *best* size from
          its children (the TextCtrl's tiny best height), and the outer layout
          adds the wrapper with ``proportion 0`` / no ``wx.EXPAND`` — so it
          adopts that best size, not ``SetMinSize``, and the box renders a few px
          tall. ``SetInitialSize((w, h))`` pins both the min size AND the
          effective best size, and we also ``SetSize`` the wrapper and give the
          inner control an explicit size so the height can't collapse.
        * **Dark backing.** Even with the control's background forced white, some
          macOS builds keep a dark backing until the next focus paint. The
          owner-drawn white wrapper underneath guarantees the field reads white
          regardless; the TextCtrl is inset so the white frame always shows.

        Returns ``(wrapper_panel, text_ctrl)``: add the wrapper to the layout,
        but keep the inner ``wx.TextCtrl`` as the public ``*_ctrl`` so the
        existing API (GetValue/SetValue/GetSelection/…) and tests keep working.
        """
        white = wx.Colour(0xFF, 0xFF, 0xFF)
        black = ExamColor.TEXT
        w, h = int(size[0]), int(size[1])

        wrap = wx.Panel(self, size=(w, h))
        wrap.SetBackgroundColour(white)
        # Pin the wrapper's size at every level the layout might consult: the
        # initial/best size (so proportion-0 sizers don't shrink to the child's
        # tiny best height), the min size, and the actual size.
        wrap.SetInitialSize((w, h))
        wrap.SetMinSize((w, h))
        wrap.SetMaxSize((w, h))
        wrap.SetSize((w, h))
        # Owner-draw a solid white field + 1px border — dark-mode-proof.
        wrap.Bind(wx.EVT_PAINT, lambda evt, p=wrap: self._paint_box(evt, p))

        # Inset so the inner control floats on the white field, leaving a visible
        # white frame on all sides; the control gets an explicit size so its own
        # best height can't drag the box down.
        pad = max(2, ui_scale.font_size(3))
        ctrl_w = max(1, w - 2 * pad)
        ctrl_h = max(1, h - 2 * pad)
        ctrl = wx.TextCtrl(
            wrap, pos=(pad, pad), size=(ctrl_w, ctrl_h),
            style=wx.TE_PROCESS_ENTER | wx.BORDER_NONE)
        ctrl.SetInitialSize((ctrl_w, ctrl_h))
        ctrl.SetMinSize((ctrl_w, ctrl_h))
        ctrl.SetBackgroundColour(white)
        ctrl.SetForegroundColour(black)
        # Default style governs how *typed* text is coloured; without it the OS
        # can paint new glyphs with the dark-mode default (light-on-dark).
        try:
            ctrl.SetDefaultStyle(wx.TextAttr(black, white))
        except Exception:
            pass
        try:
            ctrl.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
        except Exception:
            pass
        ctrl.Bind(wx.EVT_TEXT, self._fire_change)
        ctrl.Bind(wx.EVT_CHAR, lambda evt, c=ctrl, a=allowed_chars,
                  v=validator_fn: self._on_char(evt, c, a, v))

        # No sizer on the wrapper: a sizer would recompute the wrapper's best
        # size from the child and re-collapse it. The control is positioned
        # absolutely and re-fitted to the white field on resize.
        wrap.Bind(wx.EVT_SIZE, lambda evt, p=wrap, c=ctrl, pd=pad:
                  self._fit_ctrl(evt, p, c, pd))

        # Force the white repaint to take immediately (dark-mode controls can
        # otherwise keep the initial dark backing until the next focus event).
        try:
            ctrl.ForceRefresh()
            wrap.Refresh()
        except Exception:
            pass

        return wrap, ctrl

    @staticmethod
    def _paint_box(event, panel):
        """Owner-draw the answer box: a solid white fill with a 1px border.

        Painting the background ourselves is the only dark-mode-proof way to
        guarantee a white field — ``SetBackgroundColour`` on a panel/TextCtrl is
        silently ignored by the macOS dark chrome.
        """
        dc = wx.PaintDC(panel)
        w, h = panel.GetClientSize()
        dc.SetBrush(wx.Brush(wx.Colour(0xFF, 0xFF, 0xFF)))
        dc.SetPen(wx.Pen(ExamColor.OVAL_BORDER, 1))
        dc.DrawRectangle(0, 0, w, h)

    @staticmethod
    def _fit_ctrl(event, panel, ctrl, pad):
        """Keep the inner control inset within the (resized) white field."""
        event.Skip()
        w, h = panel.GetClientSize()
        ctrl.SetSize(pad, pad, max(1, w - 2 * pad), max(1, h - 2 * pad))

    def _make_unit_label(self, text):
        lbl = wx.StaticText(self, label=text or "")
        lbl.SetForegroundColour(ExamColor.TEXT)
        try:
            lbl.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
        except Exception:
            pass
        if not text:
            lbl.Hide()
        return lbl

    def _build_decimal_ui(self):
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        prompt = wx.StaticText(self, label="Your answer: ")
        prompt.SetForegroundColour(ExamColor.TEXT)
        try:
            prompt.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT))
        except Exception:
            pass

        self._prefix_label = self._make_unit_label(self._prefix)
        self.value_box, self.value_ctrl = self._make_box(
            (ui_scale.font_size(120), ui_scale.font_size(34)),
            _ALLOWED_DECIMAL, _would_be_valid_decimal)
        self._suffix_label = self._make_unit_label(self._suffix)

        pad = ui_scale.space(1)
        sizer.Add(prompt, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        sizer.Add(self._prefix_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        sizer.Add(self.value_box, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(self._suffix_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, pad)
        self.SetSizer(sizer)

    def _build_fraction_ui(self):
        # Outer row: [prompt] [prefix] [stacked-fraction] [suffix]
        row = wx.BoxSizer(wx.HORIZONTAL)

        prompt = wx.StaticText(self, label="Your answer: ")
        prompt.SetForegroundColour(ExamColor.TEXT)
        try:
            prompt.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT))
        except Exception:
            pass

        self._prefix_label = self._make_unit_label(self._prefix)

        # Stacked column: numerator box, fraction bar, denominator box.
        box_size = (ui_scale.font_size(90), ui_scale.font_size(34))
        self.num_box, self.num_ctrl = self._make_box(
            box_size, _ALLOWED_INTEGER, _would_be_valid_integer)
        self.fraction_bar = _FractionBar(self)
        self.den_box, self.den_ctrl = self._make_box(
            box_size, _ALLOWED_INTEGER, _would_be_valid_integer)

        stack = wx.BoxSizer(wx.VERTICAL)
        gap = max(1, ui_scale.space(1) // 2)
        stack.Add(self.num_box, 0, wx.ALIGN_CENTER_HORIZONTAL)
        stack.Add(self.fraction_bar, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, gap)
        stack.Add(self.den_box, 0, wx.ALIGN_CENTER_HORIZONTAL)

        self._suffix_label = self._make_unit_label(self._suffix)

        pad = ui_scale.space(1)
        row.Add(prompt, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        row.Add(self._prefix_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        row.Add(stack, 0, wx.ALIGN_CENTER_VERTICAL)
        row.Add(self._suffix_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, pad)
        self.SetSizer(row)

    # ── Keystroke filtering ───────────────────────────────────────────
    def _on_char(self, event, ctrl, allowed_chars, validator_fn):
        """Reject forbidden characters as typed; allow navigation/editing keys
        and any insertion that keeps the box a valid number-in-progress."""
        key = event.GetKeyCode()

        # Always allow control keys: navigation, delete/backspace, tab,
        # enter, and anything with a modifier (copy/paste/select-all etc.).
        if key < wx.WXK_SPACE or key == wx.WXK_DELETE or key > 255:
            event.Skip()
            return
        if event.ControlDown() or event.CmdDown() or event.AltDown():
            event.Skip()
            return

        ch = chr(key)
        if ch not in allowed_chars:
            return  # swallow forbidden symbol (%, $, /, comma, e, space, …)

        sel_start, sel_end = ctrl.GetSelection()
        if sel_start == sel_end:
            sel_end = sel_start  # plain insertion point
        if validator_fn(ctrl.GetValue(), sel_start, sel_end, ch):
            event.Skip()
        # else: swallow (e.g. second '.', misplaced '-')

    # ── Public API ────────────────────────────────────────────────────
    def get_response(self):
        """Return the response dict for scoring.

        * fraction mode → ``{"numerator": int, "denominator": int}``
        * single mode   → ``{"value": str}``
        * empty/invalid → ``{}``
        """
        if self.fraction_mode:
            num = self.num_ctrl.GetValue().strip()
            den = self.den_ctrl.GetValue().strip()
            if num and den:
                # Reject anything but a decimal integer (no scientific
                # notation, decimals, or other surprises in num/den).
                if not _is_int_literal(num) or not _is_int_literal(den):
                    return {}
                try:
                    n, d = int(num), int(den)
                    if d == 0:
                        return {}
                    return {"numerator": n, "denominator": d}
                except ValueError:
                    pass
            return {}
        else:
            val = self.value_ctrl.GetValue().strip()
            if val:
                try:
                    parsed = float(val)
                except ValueError:
                    return {}
                if not math.isfinite(parsed):
                    return {}
                return {"value": val}
            return {}

    def set_response(self, payload):
        """Restore a saved response (inverse of :meth:`get_response`)."""
        if not isinstance(payload, dict):
            return
        if self.fraction_mode:
            self.num_ctrl.SetValue(str(payload.get("numerator", "")))
            self.den_ctrl.SetValue(str(payload.get("denominator", "")))
        else:
            self.value_ctrl.SetValue(str(payload.get("value", "")))

    def clear(self):
        """Empty all input box(es)."""
        if self.fraction_mode:
            self.num_ctrl.SetValue("")
            self.den_ctrl.SetValue("")
        else:
            self.value_ctrl.SetValue("")

    def set_on_change(self, callback):
        """Register a callback invoked with the current response dict on edit."""
        self._on_change = callback

    def set_unit(self, prefix=None, suffix=None):
        """Set/replace the adjacent unit labels for this question.

        ``prefix`` prints to the LEFT of the box (e.g. ``"$"``); ``suffix`` to
        the RIGHT (e.g. ``"feet"``). Pass ``None``/empty to hide a label.
        """
        self._prefix = prefix
        self._suffix = suffix
        self._apply_label(self._prefix_label, prefix)
        self._apply_label(self._suffix_label, suffix)
        self.Layout()

    def _apply_label(self, label, text):
        if label is None:
            return
        label.SetLabel(text or "")
        label.Show(bool(text))

    def _fire_change(self, event):
        if self._on_change:
            self._on_change(self.get_response())
