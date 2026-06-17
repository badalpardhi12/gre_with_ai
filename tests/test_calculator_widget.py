"""
Headless tests for the rebuilt ETS GRE on-screen calculator
(``widgets/calculator.py``, spec ``docs/gre_ui_spec_2026_06.md`` §4).

Exercises the calculation engine + UI behaviors on a hidden ``wx.Frame``:
PEMDAS precedence, ÷0 / overflow / √(negative) → ERROR with C-only dismissal,
√ postfix, ± sign toggle, memory M+/MR/MC, Transfer Display gating, and
American thousands commas. Also asserts the preserved public API surface
(constructor arg, ``set_on_transfer``, ``get_value``, ``Show``/``Hide``/
``IsShown``) so the existing ``QuestionScreen`` caller keeps working.

Skipped wholesale when wxPython isn't importable (headless CI).
"""
import pytest

pytest.importorskip("wx", reason="CalculatorWidget requires wxPython")

import wx  # noqa: E402

from widgets.calculator import CalculatorWidget, _format_display  # noqa: E402


@pytest.fixture(scope="module")
def wx_app():
    """One wx.App per module — many apps leak resources on macOS."""
    app = wx.App(False)
    yield app
    # No MainLoop; let the app fall out of scope.


@pytest.fixture
def frame(wx_app):
    f = wx.Frame(None)
    yield f
    f.Destroy()


@pytest.fixture
def calc(frame):
    """A calculator built inline on a hidden frame (deterministic, no
    floating top-level window to manage in the test)."""
    c = CalculatorWidget(frame, inline=True)
    yield c


def _enter(calc, *keys):
    for k in keys:
        calc.press(k)


# ── PEMDAS ──────────────────────────────────────────────────────────────

def test_pemdas_precedence(calc):
    # ETS canonical example: 1 + 2 × 4 = 9 (not 12).
    _enter(calc, "1", "+", "2", "×", "4", "=")
    assert calc.get_value() == "9"


def test_single_level_parentheses(calc):
    _enter(calc, "(", "1", "+", "2", ")", "×", "4", "=")
    assert calc.get_value() == "12"


# ── division by zero → ERROR; only C dismisses ──────────────────────────

def test_divide_by_zero_error_only_c_clears(calc):
    _enter(calc, "1", "÷", "0", "=")
    assert calc.get_value() == "ERROR"
    assert calc.in_error is True

    # CE must NOT dismiss ERROR.
    calc.press("CE")
    assert calc.get_value() == "ERROR"
    # Digits are locked out while in ERROR.
    calc.press("5")
    assert calc.get_value() == "ERROR"

    # Only C clears it.
    calc.press("C")
    assert calc.in_error is False
    assert calc.get_value() == "0"


# ── square root (postfix/unary) ─────────────────────────────────────────

def test_sqrt_of_nine_is_three(calc):
    _enter(calc, "9", "√")
    assert calc.get_value() == "3"


def test_sqrt_of_negative_is_error(calc):
    # ±  toggles 4 → -4, then √ on a negative value errors.
    _enter(calc, "4", "±", "√")
    assert calc.get_value() == "ERROR"


# ── ± sign toggle ───────────────────────────────────────────────────────

def test_sign_toggle(calc):
    _enter(calc, "5", "±")
    assert calc.get_value() == "-5"
    calc.press("±")
    assert calc.get_value() == "5"


# ── overflow (> 99,999,999) → ERROR ─────────────────────────────────────

def test_nine_digit_overflow_is_error(calc):
    # 999,999,999 is 9 digits → exceeds the 8-digit display → ERROR.
    _enter(calc, "9", "9", "9", "9", "9", "9", "9", "9", "9", "=")
    assert calc.get_value() == "ERROR"


def test_multiplication_overflow_is_error(calc):
    # 99999 × 99999 = 9,999,800,001 → overflow.
    _enter(calc, "9", "9", "9", "9", "9", "×", "9", "9", "9", "9", "9", "=")
    assert calc.get_value() == "ERROR"


# ── memory M+ / MR / MC ─────────────────────────────────────────────────

def test_memory_accumulate_recall_clear(calc):
    assert calc.memory_active is False

    # M+ accumulates (does not overwrite) and lights M.
    _enter(calc, "5", "M+")
    assert calc.memory_active is True

    # Clear the display; memory survives (C leaves memory intact).
    calc.press("C")
    assert calc.get_value() == "0"
    assert calc.memory_active is True

    # Add another value: memory now 5 + 3 = 8 (accumulate, not overwrite).
    _enter(calc, "3", "M+")

    # MR recalls the accumulated total.
    calc.press("C")
    calc.press("MR")
    assert calc.get_value() == "8"

    # MC clears memory and removes the M indicator.
    calc.press("MC")
    assert calc.memory_active is False
    calc.press("C")
    calc.press("MR")
    assert calc.get_value() == "0"


# ── Transfer Display gating ─────────────────────────────────────────────

def test_set_transfer_enabled_disables_transfer(calc):
    received = []
    calc.set_on_transfer(lambda v: received.append(v))

    # Enabled by default: a transfer fires the callback with the verbatim
    # display value.
    _enter(calc, "4", "2", "=")
    assert calc.transfer_enabled is True
    calc._keypad._on_transfer(None)
    assert received == ["42"]

    # Disabled: button is greyed/non-clickable; the callback must NOT fire.
    calc.set_transfer_enabled(False)
    assert calc.transfer_enabled is False
    assert calc._keypad.transfer_btn.IsEnabled() is False
    calc._keypad._on_transfer(None)
    assert received == ["42"]  # unchanged

    # Re-enable restores clickability.
    calc.set_transfer_enabled(True)
    assert calc._keypad.transfer_btn.IsEnabled() is True


def test_transfer_copies_value_verbatim_with_commas(calc):
    received = []
    calc.set_on_transfer(lambda v: received.append(v))
    _enter(calc, "1", "2", "3", "4", "5", "6", "7", "=")
    assert calc.get_value() == "1,234,567"
    calc._keypad._on_transfer(None)
    # Transfer copies the display string verbatim (commas included, per §4.4).
    assert received == ["1,234,567"]


# ── American thousands commas in the display ────────────────────────────

def test_commas_in_display(calc):
    _enter(calc, "1", "0", "0", "0", "×", "1", "0", "0", "0", "=")
    assert calc.get_value() == "1,000,000"


def test_format_display_helpers():
    # Direct unit coverage of the formatter (no wx needed).
    assert _format_display(0) == "0"
    assert _format_display(9) == "9"
    assert _format_display(1234567) == "1,234,567"
    assert _format_display(-1234) == "-1,234"
    assert _format_display(99999999) == "99,999,999"
    assert _format_display(100000000) is None          # overflow
    assert _format_display(1e-9) == "0"                # tiny positive → "0"
    # Non-terminating result fits to 8 significant digits.
    assert _format_display(1.0 / 3.0).startswith("0.333")


def test_tiny_value_renders_zero_but_keeps_true_value(calc):
    # 1 ÷ 1000000000 = 1e-9 < 1e-7 → renders "0".
    _enter(calc, "1", "÷", "1", "0", "0", "0", "0", "0", "0", "0", "0", "0", "=")
    assert calc.get_value() == "0"
    assert calc.in_error is False
    # The true internal value is retained: × 1e9 brings it back to 1.
    _enter(calc, "×", "1", "0", "0", "0", "0", "0", "0", "0", "0", "0", "=")
    assert calc.get_value() == "1"


# ── preserved public API surface ────────────────────────────────────────

def test_public_api_preserved(frame):
    # Default presentation is a floating window; the proxy is sizer-safe and
    # the standard window visibility API toggles the floating frame.
    c = CalculatorWidget(frame)
    host_sizer = wx.BoxSizer(wx.VERTICAL)
    host_sizer.Add(c, 0, wx.EXPAND)   # must not assert (sizer-safe proxy)
    frame.SetSizer(host_sizer)
    frame.Layout()

    assert c.IsShown() is False       # starts hidden
    c.Show(True)
    assert c.IsShown() is True
    c.Show(not c.IsShown())           # the exact toggle question_screen uses
    assert c.IsShown() is False
    c.Hide()
    assert c.IsShown() is False

    # set_on_transfer / get_value still exist and behave.
    assert hasattr(c, "set_on_transfer")
    assert c.get_value() == "0"
