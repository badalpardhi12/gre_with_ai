"""
ETS GRE on-screen calculator (Quantitative Reasoning sections).

Faithful replica of the official ETS calculator described in
``docs/gre_ui_spec_2026_06.md`` §4. Distinct from the dark study-app chrome:
a floating, draggable, light-grey window with bevel keys, a light LCD display
with dark monospace digits, and a blue focus outline. Re-skinned with the
``ExamColor`` exam-mode palette.

Key set (§4.1, §4.2)
    Row 1   MR  MC  M+                 ← memory row (top)
    Row 2   CE  C   ±   √              ← clear / utility row
    Row 3   7   8   9   ÷
    Row 4   4   5   6   ×
    Row 5   1   2   3   −
    Row 6   0   .   =   +
    Row 7   (   )                      ← single-level parentheses pair
    [ Transfer Display ]               ← full-width bar at the very bottom

Behaviors (§4.3)
    * PEMDAS precedence via a char-whitelisted, ``**``-rejecting ``eval`` —
      full internal float precision is kept; only the rendered string is
      clamped to 8 digits with American thousands commas.
    * ``√`` is postfix/unary on the current value; ``±`` toggles the sign of
      the current display value.
    * ``ERROR`` (caps) on ÷0, √(negative), or a result > 99,999,999. Only
      ``C`` dismisses ERROR; the display is locked until then.
    * A positive result < 1e-7 renders as ``0`` while the true internal value
      is retained for the next operation.
    * Memory: ``M+`` accumulates (does not overwrite) and lights the ``M``
      indicator; ``MR`` recalls; ``MC`` clears memory + ``M``; ``C`` clears the
      display but leaves memory + ``M`` intact.
    * Keyboard shortcuts when focused: ``0-9 . + - * / ( ) =`` and Enter. No
      shortcut for ``± √ C CE``; backspace does NOT clear.

Public API (preserved + extended)
    CalculatorWidget(parent, floating=True, inline=False)
        ``set_on_transfer(callback)``   — callback(value_str) on Transfer.
        ``set_transfer_enabled(bool)``  — gate the Transfer Display button.
        ``get_value()``                 — current display string.
        ``Show(bool)`` / ``Hide()`` / ``IsShown()`` — toggle visibility
            (forwarded to the floating window in the default presentation).
"""
from decimal import Decimal

import wx

from widgets.theme import ExamColor
from widgets import ui_scale


# Maximum integer magnitude the display can show (8 nines).
_OVERFLOW_LIMIT = 99_999_999
# Positive results smaller than this render as "0" (true value kept).
_TINY_THRESHOLD = 1e-7
# Number of significant digits the LCD can show.
_DISPLAY_DIGITS = 8
_ERROR = "ERROR"


def _safe_expr_token(value):
    """Render ``value`` as a whitelist-safe, exponent-free decimal string.

    The eval pass only permits ``0-9 . + - * / ( )`` — Python's ``repr`` may
    emit scientific notation (``1e-09``), whose ``e`` would fail that
    whitelist and falsely trip ERROR when a tiny/huge result is chained into
    the next operation. ``Decimal(repr(v))`` round-trips the float exactly,
    and ``format(..., 'f')`` forces plain decimal — preserving full internal
    precision (spec §4.3 "keep the true internal value for the next op").
    """
    return format(Decimal(repr(float(value))), "f")


def _format_display(value):
    """Format a numeric value for the 8-digit LCD per spec §4.3.

    * Integers get American thousands commas (e.g. ``1,234,567``).
    * Non-integers are rounded to fit 8 significant digits, trailing zeros
      trimmed, integer part comma-grouped.
    * A positive value strictly between 0 and ``_TINY_THRESHOLD`` renders as
      ``"0"`` (the caller keeps the true internal value).
    * Returns ``None`` to signal overflow (caller raises ERROR).
    """
    if value != value:  # NaN guard
        return None

    # Overflow: anything that rounds to magnitude > 99,999,999.
    if abs(value) > _OVERFLOW_LIMIT + 0.5:
        return None

    # Tiny positive value collapses to "0" on the LCD (true value retained).
    if 0 < value < _TINY_THRESHOLD:
        return "0"
    if value == 0:
        return "0"

    neg = value < 0
    mag = abs(value)

    # Exact integer → comma-grouped, no decimals.
    if mag == int(mag) and mag <= _OVERFLOW_LIMIT:
        s = "{:,}".format(int(mag))
        return ("-" + s) if neg else s

    # Fractional value: fit within 8 significant figures. Determine how many
    # digits are taken by the integer part, give the remainder to decimals.
    int_part = int(mag)
    int_len = len(str(int_part))
    decimals = max(0, _DISPLAY_DIGITS - int_len)
    rounded = round(mag, decimals)

    # Rounding may have bumped the integer part across a digit boundary
    # (e.g. 99999999.6 → 100000000) — re-check overflow.
    if rounded > _OVERFLOW_LIMIT + 0.5:
        return None
    # Rounding a tiny value down to exactly 0 should still read "0".
    if rounded == 0:
        return "0"

    if rounded == int(rounded):
        s = "{:,}".format(int(rounded))
        return ("-" + s) if neg else s

    # Format with the computed decimals, strip trailing zeros, comma-group
    # the integer portion.
    text = "{:.{}f}".format(rounded, decimals).rstrip("0").rstrip(".")
    if "." in text:
        whole, frac = text.split(".", 1)
        whole = "{:,}".format(int(whole))
        out = whole + "." + frac
    else:
        out = "{:,}".format(int(text))
    return ("-" + out) if neg else out


class _CalcEngine:
    """Headless calculation core — UI-free so it is trivially testable.

    Holds the pending expression string, the true numeric value of the last
    result, the memory slot, and the error-lock flag. The owning panel pushes
    button labels in via :meth:`press` and reads :attr:`display` back out.
    """

    # Display glyphs → Python operators for the eval pass.
    _OP_MAP = {"÷": "/", "×": "*", "−": "-"}
    _EVAL_ALLOWED = set("0123456789.+-*/() ")

    def __init__(self):
        self._expr = ""        # expression being typed, in Python operators
        self._value = 0.0      # true numeric value of the current display
        self._memory = 0.0
        self._has_memory = False
        self.display = "0"     # rendered LCD string
        self._error = False    # ERROR lock; only C clears
        # True right after a value is "committed" (=, √, ±, MR, M+). The next
        # *digit / "." / "("* must start a brand-new entry instead of appending
        # to the rendered result — matching every real calculator (and the ETS
        # one): 2 + 3 = 5, then pressing 7 shows 7, not 5.07. A following
        # *operator*, by contrast, chains off the committed value.
        self._committed = False

    # ── queries ───────────────────────────────────────────────────────
    @property
    def memory_active(self):
        return self._has_memory

    @property
    def in_error(self):
        return self._error

    # ── public command dispatch ───────────────────────────────────────
    def press(self, label):
        """Apply a single key. ``label`` is a display glyph (e.g. ``"÷"``)."""
        # When locked in ERROR, only C does anything.
        if self._error and label != "C":
            return

        if label == "C":
            self._clear_entry(full=True)
        elif label == "CE":
            self._clear_entry(full=False)
        elif label == "=":
            self._evaluate()
        elif label == "±":
            self._toggle_sign()
        elif label == "√":
            self._sqrt()
        elif label == "MR":
            self._memory_recall()
        elif label == "MC":
            self._memory_clear()
        elif label == "M+":
            self._memory_add()
        elif label in self._OP_MAP or label in "+()":
            self._append(self._OP_MAP.get(label, label))
        elif label == ".":
            self._append_decimal()
        elif label.isdigit():
            self._append_digit(label)
        # anything else is silently ignored

    # ── editing ───────────────────────────────────────────────────────
    def _append(self, token):
        # An operator/paren after a committed result chains off that result:
        # keep the stored value as the expression head, just clear the commit
        # latch. A "(" instead starts a fresh expression (you can't multiply
        # implicitly into a parenthesis on this calculator).
        if self._committed:
            self._committed = False
            if token == "(":
                self._expr = ""
                self._value = 0.0
        self._expr += token
        self.display = self._expr_as_display()

    def _append_digit(self, digit):
        # A digit after a committed result starts a brand-new entry rather than
        # appending to the rendered value (2+3=5 then 7 → 7, not 5.07).
        if self._committed:
            self._committed = False
            self._expr = ""
            self._value = 0.0
        if self._expr in ("", "0"):
            self._expr = digit
        else:
            self._expr += digit
        self.display = self._expr_as_display()

    def _append_decimal(self):
        """"." starts a fresh entry after a commit and prints a leading zero.

        A bare leading decimal reads ``0.`` on the LCD (real-calculator look),
        and a second "." within the same number literal is ignored.
        """
        if self._committed:
            self._committed = False
            self._expr = ""
            self._value = 0.0
        # Reject a second decimal point inside the current number literal.
        tail = self._current_number_tail()
        if "." in tail:
            return
        if self._expr in ("", "0"):
            self._expr = "0."
        else:
            self._expr += "."
        self.display = self._expr_as_display()

    def _current_number_tail(self):
        """The trailing run of the expression that forms the number being typed
        (everything after the last operator or parenthesis)."""
        idx = -1
        for i, ch in enumerate(self._expr):
            if ch in "+-*/()":
                idx = i
        return self._expr[idx + 1:]

    def _expr_as_display(self):
        """Show the in-progress expression with ETS glyphs (not eval ops)."""
        glyphs = {"/": "÷", "*": "×", "-": "−"}
        return "".join(glyphs.get(ch, ch) for ch in self._expr) or "0"

    def _clear_entry(self, full):
        """C (full) clears all & dismisses ERROR; CE clears the entry only.

        Memory + the M indicator survive both (spec §4.3). In this single-line
        model CE behaves like C for the typed expression but, unlike C, does
        not dismiss an ERROR lock.
        """
        if full:
            self._error = False
        elif self._error:
            # CE does not dismiss ERROR.
            return
        self._expr = ""
        self._value = 0.0
        self._committed = False
        self.display = "0"

    # ── evaluation ────────────────────────────────────────────────────
    def _evaluate(self):
        if not self._expr:
            return
        if "**" in self._expr:
            # Even with builtins scrubbed, 9**9**9 would block the UI on a
            # huge BigInt — reject outright (safety, spec §4.3).
            self._raise_error()
            return
        if not all(c in self._EVAL_ALLOWED for c in self._expr):
            self._raise_error()
            return
        try:
            result = eval(self._expr, {"__builtins__": {}}, {})  # noqa: S307
        except ZeroDivisionError:
            self._raise_error()
            return
        except Exception:
            self._raise_error()
            return
        if isinstance(result, complex):
            self._raise_error()
            return
        self._set_result(float(result))

    def _toggle_sign(self):
        """± flips the sign of the *current display value* (spec §4.3)."""
        self._sync_value_from_expr()
        self._set_result(-self._value)

    def _sqrt(self):
        """√ is postfix/unary on the current value (spec §4.3)."""
        self._sync_value_from_expr()
        if self._value < 0:
            self._raise_error()
            return
        self._set_result(self._value ** 0.5)

    def _sync_value_from_expr(self):
        """Resolve the typed expression to a number before a unary op.

        If the user typed an expression and then pressed ± or √ without =, we
        evaluate first so the unary acts on the resolved value.
        """
        if self._expr == "":
            self._value = 0.0
            return
        # A lone numeric literal: parse directly to keep full precision.
        try:
            self._value = float(self._expr)
            return
        except ValueError:
            pass
        # Otherwise evaluate the expression; on failure leave value at 0.
        if "**" in self._expr or not all(c in self._EVAL_ALLOWED for c in self._expr):
            self._raise_error()
            return
        try:
            self._value = float(eval(self._expr, {"__builtins__": {}}, {}))  # noqa: S307
        except Exception:
            self._raise_error()

    # ── memory ────────────────────────────────────────────────────────
    def _memory_recall(self):
        self._set_result(self._memory)

    def _memory_clear(self):
        self._memory = 0.0
        self._has_memory = False

    def _memory_add(self):
        """M+ accumulates the current value into memory (does not overwrite)."""
        self._sync_value_from_expr()
        if self._error:
            return
        self._memory += self._value
        self._has_memory = True
        # M+ leaves the display showing the current (evaluated) value.
        self._set_result(self._value)

    # ── result plumbing ───────────────────────────────────────────────
    def _set_result(self, value):
        """Store the true value, render the clamped 8-digit string.

        On overflow / NaN this raises ERROR. The pending expression is reset
        to the resolved numeric value so the next operator chains off it.
        """
        rendered = _format_display(value)
        if rendered is None:
            self._raise_error()
            return
        self._value = value
        self._error = False
        # Keep full internal precision for chaining: store the exact value as
        # the new expression so a following op (e.g. "+ 1") uses it verbatim.
        # Use an exponent-free token so the eval whitelist accepts it.
        self._expr = _safe_expr_token(value)
        self._committed = True
        self.display = rendered

    def _raise_error(self):
        self._error = True
        self._expr = ""
        self._value = 0.0
        self._committed = False
        self.display = _ERROR


# ── exam-mode key fills (light grey-on-light, dark glyphs) ─────────────
# Native wx.Button ignores SetBackgroundColour on macOS Dark appearance, so
# the keys are owner-drawn (immune to the OS dark chrome). ETS keys are a light
# bevel with DARK glyphs; operators sit slightly darker, memory keys faint navy.
_KEY_BG = wx.Colour(0xEC, 0xEC, 0xEC)       # light-grey digit key [I]
_KEY_BG_HOVER = wx.Colour(0xF6, 0xF6, 0xF6) # lit-up on hover
_KEY_BG_PRESS = wx.Colour(0xD8, 0xD8, 0xD8) # pressed-in
_OP_BG = wx.Colour(0xDC, 0xDC, 0xDC)        # darker bevel for operators
_OP_BG_HOVER = wx.Colour(0xE6, 0xE6, 0xE6)
_OP_BG_PRESS = wx.Colour(0xC8, 0xC8, 0xC8)
_MEM_BG = wx.Colour(0xD2, 0xD8, 0xE2)       # faint-navy memory keys
_MEM_BG_HOVER = wx.Colour(0xDE, 0xE3, 0xEB)
_MEM_BG_PRESS = wx.Colour(0xBE, 0xC6, 0xD4)
_KEY_BORDER = wx.Colour(0xB8, 0xB8, 0xB8)   # bevel outline


class _CalcKey(wx.Panel):
    """Owner-drawn calculator key — a filled rounded rect + centered glyph.

    Native ``wx.Button`` ignores ``SetBackgroundColour`` / ``SetForegroundColour``
    on macOS Dark appearance, so a digit like ``7`` renders as a light glyph on
    the OS dark chrome and becomes invisible. This control paints exactly the
    colours it is given (a light bevel with dark text), so it stays legible in
    both Light and Dark appearance. Emits :data:`wx.EVT_BUTTON` like the native
    button it replaces, and exposes ``Enable`` / ``IsEnabled`` so the Transfer
    Display gating keeps working.
    """

    def __init__(self, parent, label, fills, text_colour, font, size):
        super().__init__(parent, size=size, style=wx.WANTS_CHARS)
        self._label = label
        self._fill, self._fill_hover, self._fill_press = fills
        self._text = text_colour
        self._font = font
        self._hover = False
        self._pressed = False
        self._enabled = True

        self.SetMinSize(size)
        self.SetBackgroundColour(parent.GetBackgroundColour())
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_up)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)

    # ── enable/disable (mirror the wx.Window surface used by callers) ──
    def Enable(self, enable=True):  # noqa: N802 — wx idiom
        self._enabled = bool(enable)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND if enable else wx.CURSOR_ARROW))
        self.Refresh()
        return super().Enable(enable)

    def Disable(self):  # noqa: N802
        return self.Enable(False)

    def IsEnabled(self):  # noqa: N802
        return self._enabled

    def set_fills(self, fills, text_colour):
        self._fill, self._fill_hover, self._fill_press = fills
        self._text = text_colour
        self.Refresh()

    # ── event plumbing (emit wx.EVT_BUTTON, like wx.Button) ───────────
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

    def _on_down(self, _):
        if not self._enabled:
            return
        self._pressed = True
        if not self.HasCapture():
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

    def _on_capture_lost(self, _):
        self._pressed = False

    # ── painting ──────────────────────────────────────────────────────
    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        # Paint the panel background (parent colour) so the rounded corners read
        # clean against the calculator body in either appearance.
        gc.SetBrush(wx.Brush(self.GetParent().GetBackgroundColour()))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRectangle(0, 0, w, h)

        if not self._enabled:
            bg = ExamColor.BTN_DISABLED
            fg = ExamColor.TEXT_ON_NAVY
        elif self._pressed:
            bg, fg = self._fill_press, self._text
        elif self._hover:
            bg, fg = self._fill_hover, self._text
        else:
            bg, fg = self._fill, self._text

        radius = max(2, ui_scale.font_size(4))
        gc.SetPen(wx.Pen(_KEY_BORDER, 1))
        gc.SetBrush(wx.Brush(bg))
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, radius)

        gc.SetFont(self._font, fg)
        tw, th = gc.GetTextExtent(self._label)
        gc.DrawText(self._label, (w - tw) / 2.0, (h - th) / 2.0)


class _CalcDisplay(wx.Panel):
    """Owner-drawn LCD — a light field with dark, right-aligned monospace digits
    and an ``M`` memory indicator on the left.

    Replaces the former ``wx.TextCtrl``: a read-only ``wx.TextCtrl`` renders with
    the OS dark chrome on macOS Dark appearance (dark field, faint digits),
    defeating the light-LCD look. Painting it ourselves keeps it dark-on-light in
    every appearance. Still accepts keystrokes (it has focus) and forwards them
    to the keypad's shortcut handler.
    """

    def __init__(self, parent, lcd_bg, digit_fg, digit_font, mem_font, height):
        super().__init__(parent, size=(-1, height), style=wx.WANTS_CHARS)
        self._lcd_bg = lcd_bg
        self._fg = digit_fg
        self._digit_font = digit_font
        self._mem_font = mem_font
        self._value = "0"
        self._mem = False
        self.SetMinSize((-1, height))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        # Click to focus so keyboard shortcuts land here.
        self.Bind(wx.EVT_LEFT_DOWN, lambda _e: self.SetFocus())

    def SetValue(self, value):  # noqa: N802 — mirror wx.TextCtrl
        self._value = value
        self.Refresh()

    def GetValue(self):  # noqa: N802 — mirror wx.TextCtrl
        return self._value

    def set_memory(self, active):
        self._mem = bool(active)
        self.Refresh()

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        # Light LCD field with a thin inset border.
        gc.SetBrush(wx.Brush(self._lcd_bg))
        gc.SetPen(wx.Pen(_KEY_BORDER, 1))
        gc.DrawRectangle(0.5, 0.5, w - 1, h - 1)

        pad = max(4, ui_scale.font_size(4))
        # 'M' indicator on the left.
        gc.SetFont(self._mem_font, self._fg)
        if self._mem:
            gc.DrawText("M", pad, (h - gc.GetTextExtent("M")[1]) / 2.0)

        # Right-aligned monospace value.
        gc.SetFont(self._digit_font, self._fg)
        tw, th = gc.GetTextExtent(self._value)
        gc.DrawText(self._value, max(pad, w - tw - pad), (h - th) / 2.0)


class _CalcKeypad(wx.Panel):
    """The visible calculator surface (display + keys + Transfer Display).

    Hosted either inside the floating ``wx.MiniFrame`` (default presentation)
    or directly inline. All button presses and keyboard shortcuts drive a
    shared :class:`_CalcEngine`.
    """

    # Layout per spec §4.2. ``None`` is a blank cell (row 7 only has "( )").
    _LAYOUT = [
        ["MR", "MC", "M+", None],
        ["CE", "C", "±", "√"],
        ["7", "8", "9", "÷"],
        ["4", "5", "6", "×"],
        ["1", "2", "3", "−"],
        ["0", ".", "=", "+"],
        ["(", ")", None, None],
    ]

    # Keyboard char → display glyph (spec §4.3 — no shortcut for ± √ C CE).
    _KEY_TO_LABEL = {
        ".": ".", "+": "+", "-": "−", "*": "×", "/": "÷",
        "(": "(", ")": ")", "=": "=",
    }

    # Distinct fills: operators/utility get the darker bevel, digits lighter.
    _OP_KEYS = {"÷", "×", "−", "+", "=", "√", "(", ")", "±", "CE", "C"}
    _MEM_KEYS = {"MR", "MC", "M+"}

    def __init__(self, parent, engine, on_transfer_getter):
        super().__init__(parent)
        self._engine = engine
        # Callable returning the current ``set_on_transfer`` callback (lets the
        # owning widget keep the callback even if the keypad is rebuilt).
        self._on_transfer_getter = on_transfer_getter
        self._transfer_enabled = True
        # When the floating window is active, draw a blue focus *ring* around
        # the body (spec §4.4 "blue outline on the whole calculator"). This is
        # a painted border only — the body fill stays grey so the owner-drawn
        # keys keep painting their corners against the body colour rather than
        # against a solid blue field.
        self._focused = False

        # ── exam-mode skin ────────────────────────────────────────────
        self._body_bg = wx.Colour(0xEC, 0xEC, 0xEC)  # light-grey body [I]
        body_bg = self._body_bg
        lcd_bg = wx.Colour(0xF2, 0xF2, 0xE6)         # light LCD [I]
        digit_fg = ExamColor.TEXT                    # dark digits
        self.SetBackgroundColour(body_bg)
        # Paint the focus ring ourselves over the sizer's outer margin.
        self.Bind(wx.EVT_PAINT, self._on_paint)

        # ── display (8-digit, right-aligned, monospace, M indicator) ───
        # Owner-drawn so the LCD stays light-field / dark-digit in macOS Dark
        # appearance (a read-only wx.TextCtrl would render with the OS dark
        # chrome and the digits would be barely legible).
        digit_font = ui_scale.make_font(
            ui_scale.font_size(18), weight=wx.FONTWEIGHT_NORMAL,
            family=wx.FONTFAMILY_TELETYPE)
        mem_font = ui_scale.make_font(
            ui_scale.font_size(14), weight=wx.FONTWEIGHT_BOLD,
            family=wx.FONTFAMILY_TELETYPE)
        disp_h = ui_scale.font_size(34)
        self.display = _CalcDisplay(
            self, lcd_bg, digit_fg, digit_font, mem_font, disp_h)
        # Forward keystrokes typed on the LCD to the shortcut handler.
        self.display.Bind(wx.EVT_CHAR, self._on_char)
        self.display.Bind(wx.EVT_KEY_DOWN, self._on_key_down)

        disp_row = wx.BoxSizer(wx.HORIZONTAL)
        disp_row.Add(self.display, 1, wx.EXPAND)

        # ── key grid ──────────────────────────────────────────────────
        grid = wx.GridBagSizer(vgap=3, hgap=3)
        btn_font = ui_scale.exam_sans(13)
        cell = (ui_scale.font_size(44), ui_scale.font_size(34))
        self._buttons = {}
        for r, row in enumerate(self._LAYOUT):
            for c, label in enumerate(row):
                if label is None:
                    continue
                if label in self._MEM_KEYS:
                    fills = (_MEM_BG, _MEM_BG_HOVER, _MEM_BG_PRESS)
                elif label in self._OP_KEYS:
                    fills = (_OP_BG, _OP_BG_HOVER, _OP_BG_PRESS)
                else:
                    fills = (_KEY_BG, _KEY_BG_HOVER, _KEY_BG_PRESS)
                btn = _CalcKey(self, label, fills, ExamColor.TEXT, btn_font, cell)
                btn.Bind(wx.EVT_BUTTON, lambda e, l=label: self._press(l))
                # Keep keyboard shortcuts working from a focused key too.
                btn.Bind(wx.EVT_CHAR, self._on_char)
                grid.Add(btn, pos=(r, c), flag=wx.EXPAND)
                self._buttons[label] = btn
        for col in range(4):
            grid.AddGrowableCol(col)

        # ── Transfer Display (full-width bottom bar) ──────────────────
        # Owner-drawn (same theme-proof control) so the bar's fill survives
        # macOS Dark appearance. Enabled = mid-grey clickable; disabled =
        # ExamColor.BTN_DISABLED non-clickable.
        self.transfer_btn = _CalcKey(
            self, "Transfer Display",
            (ExamColor.BTN_GREY, ExamColor.BTN_GREY_HOVER, ExamColor.BTN_GREY),
            ExamColor.BTN_TEXT, btn_font, (-1, ui_scale.font_size(30)))
        self.transfer_btn.Bind(wx.EVT_BUTTON, self._on_transfer)
        self._apply_transfer_style()

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(disp_row, 0, wx.EXPAND | wx.ALL, 6)
        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        sizer.Add(self.transfer_btn, 0, wx.EXPAND | wx.ALL, 6)
        self.SetSizerAndFit(sizer)

        self._refresh()

    # ── press dispatch ────────────────────────────────────────────────
    def _press(self, label):
        self._engine.press(label)
        self._refresh()

    def _on_char(self, event):
        """Keyboard shortcuts (§4.3): 0-9 . + - * / ( ) = and Enter.

        This is the single source of truth for keyboard input. ``EVT_CHAR``
        fires once per printable keystroke on every platform and already
        carries the resolved character, so digits/operators/Enter are handled
        *only* here. ``EVT_KEY_DOWN`` deliberately does NOT also act on them
        (that would double-fire on macOS, turning ``5`` into ``55``); it only
        swallows backspace/delete and lets everything else through to become a
        CHAR event.
        """
        code = event.GetKeyCode()
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._press("=")
            return
        ch = chr(code) if 0 < code < 256 else ""
        if ch.isdigit():
            self._press(ch)
            return
        if ch in self._KEY_TO_LABEL:
            self._press(self._KEY_TO_LABEL[ch])
            return
        # Backspace and everything else: do NOT clear (spec §4.3). Swallow so
        # the read-only display never echoes raw characters.
        if code in (wx.WXK_BACK, wx.WXK_DELETE):
            return
        # Let navigation keys (arrows, tab) through.
        event.Skip()

    def _on_key_down(self, event):
        """Pre-pass for the owner-drawn display.

        Only intercepts backspace/delete (so they don't reach the value and
        never clear — spec §4.3). Printable keys are intentionally passed
        through with ``Skip()`` so they arrive once as an ``EVT_CHAR`` and are
        handled solely by :meth:`_on_char`. Handling them here too would
        double-fire on macOS.
        """
        code = event.GetKeyCode()
        if code in (wx.WXK_BACK, wx.WXK_DELETE):
            # Swallow: backspace must not clear and must not echo.
            return
        event.Skip()

    # ── view refresh ──────────────────────────────────────────────────
    def _refresh(self):
        self.display.SetValue(self._engine.display)
        self.display.set_memory(self._engine.memory_active)

    # ── focus ring (blue outline when the floating window is active) ───
    def set_focused(self, focused):
        """Show/hide the blue focus ring around the calculator body (§4.4).

        Crucially this does NOT change the body background colour: the
        owner-drawn keys paint their rounded corners against the parent's
        background, so a blue body would tint every key. We paint a thin ring
        in the sizer's outer margin instead.
        """
        focused = bool(focused)
        if focused != self._focused:
            self._focused = focused
            self.Refresh()

    def _on_paint(self, event):
        # Self-contained paint: fill the body grey, then (when focused) draw a
        # thin blue ring in the sizer's outer margin. We don't call
        # ``event.Skip()`` so there is never a second PaintDC live at once.
        dc = wx.PaintDC(self)
        dc.SetBackground(wx.Brush(self._body_bg))
        dc.Clear()
        if not self._focused:
            return
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        gc.SetPen(wx.Pen(ExamColor.BTN_NEXT_BLUE, 2))
        gc.DrawRoundedRectangle(1.5, 1.5, w - 3, h - 3, 4)

    # ── transfer ──────────────────────────────────────────────────────
    def set_transfer_enabled(self, enabled):
        self._transfer_enabled = bool(enabled)
        self._apply_transfer_style()

    def _apply_transfer_style(self):
        if self._transfer_enabled:
            self.transfer_btn.Enable(True)
            self.transfer_btn.set_fills(
                (ExamColor.BTN_GREY, ExamColor.BTN_GREY_HOVER, ExamColor.BTN_GREY),
                ExamColor.BTN_TEXT)
        else:
            self.transfer_btn.Enable(False)
            self.transfer_btn.set_fills(
                (ExamColor.BTN_DISABLED, ExamColor.BTN_DISABLED,
                 ExamColor.BTN_DISABLED),
                ExamColor.TEXT_ON_NAVY)
        self.transfer_btn.Refresh()

    def _on_transfer(self, event):
        if not self._transfer_enabled:
            return
        cb = self._on_transfer_getter()
        if cb:
            cb(self.display.GetValue())

    def get_value(self):
        return self.display.GetValue()


class CalculatorWidget(wx.Panel):
    """ETS GRE on-screen calculator (spec §4).

    Constructed as a thin, zero-size :class:`wx.Panel` proxy so existing
    callers can keep adding it to a sizer and toggling it with the standard
    ``Show`` / ``Hide`` / ``IsShown`` window API. By default the calculator
    *presents* as a floating, draggable :class:`wx.MiniFrame` (spec §4.5);
    those visibility calls are forwarded to that frame. Pass ``inline=True``
    (or ``floating=False``) to embed the keypad directly in this panel instead.

    Public API (preserved):
        ``set_on_transfer(callback)`` — ``callback(value_str)`` on Transfer.
        ``get_value()``              — current display string.
        ``Show`` / ``Hide`` / ``IsShown`` — standard window visibility.
    Public API (added):
        ``set_transfer_enabled(bool)`` — gate the Transfer Display button.
    """

    def __init__(self, parent, floating=True, inline=False):
        super().__init__(parent)
        # ``inline=True`` is the explicit opt-out; ``floating=False`` aliases
        # it for symmetry with the spec's wording.
        self._floating = floating and not inline
        self._engine = _CalcEngine()
        self._on_transfer_callback = None
        self._frame = None

        if self._floating:
            # Zero-footprint proxy in the host layout; the real UI lives in a
            # floating draggable window parented to the top-level frame.
            self.SetMinSize((0, 0))
            self.SetSize((0, 0))
            self._build_floating_window(parent)
        else:
            # Embed the keypad directly in this panel.
            sizer = wx.BoxSizer(wx.VERTICAL)
            self._keypad = _CalcKeypad(
                self, self._engine, lambda: self._on_transfer_callback)
            sizer.Add(self._keypad, 1, wx.EXPAND)
            self.SetSizerAndFit(sizer)

    # ── floating window construction ──────────────────────────────────
    def _build_floating_window(self, parent):
        tlw = wx.GetTopLevelParent(parent)
        # MiniFrame gives a compact title bar that is draggable for free, with
        # a close box that we intercept to hide-not-destroy.
        self._frame = wx.MiniFrame(
            tlw, title="Calculator",
            style=wx.CAPTION | wx.CLOSE_BOX | wx.FRAME_FLOAT_ON_PARENT
            | wx.FRAME_TOOL_WINDOW)
        self._frame.SetBackgroundColour(wx.Colour(0xEC, 0xEC, 0xEC))

        outer = wx.BoxSizer(wx.VERTICAL)

        # Explicit draggable title strip (beyond the native caption) so the
        # body itself can be grabbed and moved — spec §4.5 "draggable title
        # area".
        self._drag_bar = wx.Panel(self._frame)
        self._drag_bar.SetBackgroundColour(ExamColor.HEADER_NAVY)
        drag_label = wx.StaticText(self._drag_bar, label="Calculator")
        drag_label.SetForegroundColour(ExamColor.TEXT_ON_NAVY)
        drag_label.SetFont(ui_scale.exam_sans(12, weight=wx.FONTWEIGHT_BOLD))
        db_sizer = wx.BoxSizer(wx.HORIZONTAL)
        db_sizer.Add(drag_label, 0, wx.ALL, 4)
        self._drag_bar.SetSizer(db_sizer)
        self._wire_drag(self._drag_bar)
        self._wire_drag(drag_label)
        outer.Add(self._drag_bar, 0, wx.EXPAND)

        self._keypad = _CalcKeypad(
            self._frame, self._engine, lambda: self._on_transfer_callback)
        outer.Add(self._keypad, 1, wx.EXPAND)

        self._frame.SetSizerAndFit(outer)
        self._frame.Hide()
        # Close box hides instead of destroying so the toggle keeps working.
        self._frame.Bind(wx.EVT_CLOSE, self._on_frame_close)
        # Blue focus outline cue: paint a border when the frame is active.
        self._frame.Bind(wx.EVT_ACTIVATE, self._on_frame_activate)

    def _wire_drag(self, widget):
        widget.Bind(wx.EVT_LEFT_DOWN, self._on_drag_start)
        widget.Bind(wx.EVT_LEFT_UP, self._on_drag_end)
        widget.Bind(wx.EVT_MOTION, self._on_drag_motion)
        widget.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_drag_lost)

    # ── dragging the floating window ──────────────────────────────────
    def _on_drag_start(self, event):
        w = event.GetEventObject()
        self._drag_origin = event.GetPosition()
        self._dragging = True
        if not w.HasCapture():
            w.CaptureMouse()

    def _on_drag_motion(self, event):
        if getattr(self, "_dragging", False) and event.Dragging() and event.LeftIsDown():
            screen = wx.GetMousePosition()
            self._frame.Move(screen - self._drag_origin)
        else:
            event.Skip()

    def _on_drag_end(self, event):
        w = event.GetEventObject()
        self._dragging = False
        if w.HasCapture():
            w.ReleaseMouse()

    def _on_drag_lost(self, event):
        self._dragging = False

    def _on_frame_close(self, event):
        # Hide instead of destroy so re-toggling re-shows the same window.
        self._frame.Hide()

    def _on_frame_activate(self, event):
        # Draw a thin blue focus ring around the keypad when the window is
        # active (spec §4.4). We must NOT recolour the keypad background — the
        # owner-drawn keys paint their rounded corners against the parent's
        # background, so a blue body would tint every key. ``set_focused``
        # paints a ring in the outer margin instead.
        try:
            self._keypad.set_focused(bool(event.GetActive()))
        except Exception:
            pass
        event.Skip()

    # ── visibility (forwarded to the floating window when floating) ────
    def Show(self, show=True):
        if self._floating and self._frame is not None:
            self._frame.Show(show)
            if show:
                self._frame.Raise()
            return True
        return super().Show(show)

    def Hide(self):
        return self.Show(False)

    def IsShown(self):
        if self._floating and self._frame is not None:
            return self._frame.IsShown()
        return super().IsShown()

    # ── preserved + extended public API ───────────────────────────────
    def set_on_transfer(self, callback):
        """Set callback: ``callback(value_string)`` when Transfer is clicked."""
        self._on_transfer_callback = callback

    def set_transfer_enabled(self, enabled):
        """Enable/disable the Transfer Display button (spec §4.4).

        Disabled on QC, all MC, and fraction-form Numeric Entry; enabled only
        on single-box Numeric Entry. When disabled the button is greyed
        (``ExamColor.BTN_DISABLED``) and non-clickable.
        """
        self._keypad.set_transfer_enabled(enabled)

    def get_value(self):
        """Return the current display string (verbatim, with commas)."""
        return self._keypad.get_value()

    # ── test / programmatic helpers ───────────────────────────────────
    def press(self, label):
        """Apply a single calculator key by its display glyph.

        Convenience for tests and any caller that wants to drive the engine
        without synthesizing wx button events.
        """
        self._keypad._press(label)

    @property
    def memory_active(self):
        """True when the memory slot is in use (the ``M`` indicator is lit)."""
        return self._engine.memory_active

    @property
    def in_error(self):
        """True while the display is ERROR-locked (only ``C`` dismisses)."""
        return self._engine.in_error

    @property
    def transfer_enabled(self):
        """Whether the Transfer Display button is currently clickable."""
        return self._keypad._transfer_enabled

    def Destroy(self):
        # Tear down the floating window alongside the proxy.
        if self._frame is not None:
            self._frame.Destroy()
            self._frame = None
        return super().Destroy()
