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
            self._append(".")
        elif label.isdigit():
            self._append_digit(label)
        # anything else is silently ignored

    # ── editing ───────────────────────────────────────────────────────
    def _append(self, token):
        # Starting fresh from a bare "0" with an operator keeps the 0 so the
        # operator binds to it (e.g. 0 - 5). Digits replace the leading 0.
        self._expr += token
        self.display = self._expr_as_display()

    def _append_digit(self, digit):
        if self._expr in ("", "0"):
            self._expr = digit
        else:
            self._expr += digit
        self.display = self._expr_as_display()

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
        self.display = rendered

    def _raise_error(self):
        self._error = True
        self._expr = ""
        self._value = 0.0
        self.display = _ERROR


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

        # ── exam-mode skin ────────────────────────────────────────────
        body_bg = wx.Colour(0xEC, 0xEC, 0xEC)        # light-grey body [I]
        lcd_bg = wx.Colour(0xF2, 0xF2, 0xE6)         # light LCD [I]
        digit_fg = ExamColor.TEXT                    # dark digits
        key_bg = wx.Colour(0xFA, 0xFA, 0xFA)         # light bevel key
        op_bg = wx.Colour(0xDC, 0xDC, 0xDC)          # darker bevel key
        mem_bg = wx.Colour(0xD2, 0xD8, 0xE2)         # memory keys, faint navy
        key_fg = ExamColor.TEXT
        self.SetBackgroundColour(body_bg)

        # ── display (8-digit, right-aligned, monospace, M indicator) ───
        # The M indicator + LCD live inside ``disp_panel`` so they must be
        # parented to it (a sizer only manages windows whose parent is the
        # sizer's owning window).
        disp_row = wx.BoxSizer(wx.HORIZONTAL)
        disp_panel = wx.Panel(self)
        disp_panel.SetBackgroundColour(lcd_bg)

        self._mem_indicator = wx.StaticText(disp_panel, label=" ")
        self._mem_indicator.SetFont(ui_scale.make_font(
            ui_scale.font_size(14), weight=wx.FONTWEIGHT_BOLD,
            family=wx.FONTFAMILY_TELETYPE))
        self._mem_indicator.SetForegroundColour(digit_fg)
        self._mem_indicator.SetBackgroundColour(lcd_bg)

        self.display = wx.TextCtrl(
            disp_panel, style=wx.TE_RIGHT | wx.TE_READONLY | wx.BORDER_NONE)
        self.display.SetFont(ui_scale.make_font(
            ui_scale.font_size(18), weight=wx.FONTWEIGHT_NORMAL,
            family=wx.FONTFAMILY_TELETYPE))
        self.display.SetValue("0")
        self.display.SetBackgroundColour(lcd_bg)
        self.display.SetForegroundColour(digit_fg)
        # Forward keystrokes typed on the LCD to the shortcut handler.
        self.display.Bind(wx.EVT_CHAR, self._on_char)

        dp_sizer = wx.BoxSizer(wx.HORIZONTAL)
        dp_sizer.Add(self._mem_indicator, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        dp_sizer.Add(self.display, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 2)
        disp_panel.SetSizer(dp_sizer)
        disp_row.Add(disp_panel, 1, wx.EXPAND)

        # ── key grid ──────────────────────────────────────────────────
        grid = wx.GridBagSizer(vgap=3, hgap=3)
        btn_font = ui_scale.exam_sans(13)
        cell = (ui_scale.font_size(44), ui_scale.font_size(34))
        self._buttons = {}
        for r, row in enumerate(self._LAYOUT):
            for c, label in enumerate(row):
                if label is None:
                    continue
                btn = wx.Button(self, label=label, size=cell,
                                style=wx.BU_EXACTFIT)
                btn.SetFont(btn_font)
                if label in self._MEM_KEYS:
                    btn.SetBackgroundColour(mem_bg)
                elif label in self._OP_KEYS:
                    btn.SetBackgroundColour(op_bg)
                else:
                    btn.SetBackgroundColour(key_bg)
                btn.SetForegroundColour(key_fg)
                btn.Bind(wx.EVT_BUTTON, lambda e, l=label: self._press(l))
                # Keep button focus from stealing the keyboard handler.
                btn.Bind(wx.EVT_CHAR, self._on_char)
                grid.Add(btn, pos=(r, c), flag=wx.EXPAND)
                self._buttons[label] = btn
        for col in range(4):
            grid.AddGrowableCol(col)

        # ── Transfer Display (full-width bottom bar) ──────────────────
        self.transfer_btn = wx.Button(self, label="Transfer Display")
        self.transfer_btn.SetFont(btn_font)
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
        """Keyboard shortcuts (§4.3): 0-9 . + - * / ( ) = and Enter."""
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
        # the read-only TextCtrl never echoes raw characters.
        if code in (wx.WXK_BACK, wx.WXK_DELETE):
            return
        # Let navigation keys (arrows, tab) through.
        event.Skip()

    # ── view refresh ──────────────────────────────────────────────────
    def _refresh(self):
        self.display.SetValue(self._engine.display)
        self._mem_indicator.SetLabel("M" if self._engine.memory_active else " ")

    # ── transfer ──────────────────────────────────────────────────────
    def set_transfer_enabled(self, enabled):
        self._transfer_enabled = bool(enabled)
        self._apply_transfer_style()

    def _apply_transfer_style(self):
        if self._transfer_enabled:
            self.transfer_btn.Enable(True)
            self.transfer_btn.SetBackgroundColour(ExamColor.BTN_GREY)
            self.transfer_btn.SetForegroundColour(ExamColor.BTN_TEXT)
        else:
            self.transfer_btn.Enable(False)
            self.transfer_btn.SetBackgroundColour(ExamColor.BTN_DISABLED)
            self.transfer_btn.SetForegroundColour(ExamColor.TEXT_ON_NAVY)
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
        # Draw a blue focus outline on the keypad when the window is active.
        try:
            border = ExamColor.BTN_NEXT_BLUE if event.GetActive() else ExamColor.DIVIDER
            self._keypad.SetBackgroundColour(border)
            self._keypad.Refresh()
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
