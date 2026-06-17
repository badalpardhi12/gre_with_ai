"""
Headless tests for widgets/numeric_entry.py (ETS Numeric Entry, spec §5.5).

Constructs the widget on a hidden wx.Frame and exercises:
  * single box accepts a decimal ("12.5") and rejects "$"/"," at the keystroke
    level;
  * fraction mode exposes numerator + denominator boxes laid out *stacked*
    (numerator above the fraction bar above the denominator), with a drawn bar;
  * optional unit prefix ("$") / suffix ("feet") render and toggle;
  * get_response / set_response / clear round-trip for both modes.
"""
import pytest

wx = pytest.importorskip("wx")

from widgets.numeric_entry import NumericEntry  # noqa: E402


@pytest.fixture(scope="module")
def app():
    application = wx.App(False)
    yield application


@pytest.fixture
def frame(app):
    frm = wx.Frame(None)
    yield frm
    frm.Destroy()


def _char_event(ctrl, ch):
    """Build a wx.EVT_CHAR event for character ``ch`` aimed at ``ctrl``."""
    evt = wx.KeyEvent(wx.wxEVT_CHAR)
    evt.SetEventObject(ctrl)
    evt.SetKeyCode(ord(ch))
    return evt


def _type(widget, ctrl, allowed, validator, text):
    """Drive characters through the widget's real _on_char filter, mimicking
    keyboard entry: accepted chars get appended (Skip()=True), rejected ones
    are swallowed."""
    for ch in text:
        evt = _char_event(ctrl, ch)
        # The handler reads GetSelection() to know where the keystroke lands;
        # collapse the selection to a caret at the end of the current text so
        # we faithfully mimic appending one char at a time.
        end = ctrl.GetLastPosition()
        ctrl.SetSelection(end, end)
        before = ctrl.GetValue()
        widget._on_char(evt, ctrl, allowed, validator)
        if evt.GetSkipped():
            # Simulate the OS inserting the (allowed) character at the caret.
            ctrl.SetValue(before + ch)
        else:
            # Swallowed — value unchanged.
            assert ctrl.GetValue() == before, (
                "rejected char %r should not change the box" % ch)


# ── Single-value box ──────────────────────────────────────────────────
def test_single_box_accepts_decimal(frame):
    w = NumericEntry(frame, fraction_mode=False)
    from widgets.numeric_entry import _ALLOWED_DECIMAL, _would_be_valid_decimal
    _type(w, w.value_ctrl, _ALLOWED_DECIMAL, _would_be_valid_decimal, "12.5")
    assert w.value_ctrl.GetValue() == "12.5"
    assert w.get_response() == {"value": "12.5"}


def test_single_box_rejects_symbols(frame):
    w = NumericEntry(frame, fraction_mode=False)
    from widgets.numeric_entry import _ALLOWED_DECIMAL, _would_be_valid_decimal
    # Try to type "$1,2%/3" — only the digits should survive.
    _type(w, w.value_ctrl, _ALLOWED_DECIMAL, _would_be_valid_decimal, "$1,2%/3")
    assert w.value_ctrl.GetValue() == "123"
    # And a forbidden symbol on its own is fully swallowed.
    w2 = NumericEntry(frame, fraction_mode=False)
    _type(w2, w2.value_ctrl, _ALLOWED_DECIMAL, _would_be_valid_decimal, "$")
    assert w2.value_ctrl.GetValue() == ""


def test_single_box_rejects_second_decimal_point(frame):
    w = NumericEntry(frame, fraction_mode=False)
    from widgets.numeric_entry import _ALLOWED_DECIMAL, _would_be_valid_decimal
    _type(w, w.value_ctrl, _ALLOWED_DECIMAL, _would_be_valid_decimal, "1.2.3")
    assert w.value_ctrl.GetValue() == "1.23"  # second '.' dropped


# ── Fraction mode: stacked layout ─────────────────────────────────────
def test_fraction_mode_exposes_num_and_den(frame):
    w = NumericEntry(frame, fraction_mode=True)
    assert w.fraction_mode is True
    assert isinstance(w.num_ctrl, wx.TextCtrl)
    assert isinstance(w.den_ctrl, wx.TextCtrl)
    assert hasattr(w, "fraction_bar")


def test_fraction_boxes_are_stacked(frame):
    """Numerator must sit ABOVE the fraction bar ABOVE the denominator
    (stacked, not side-by-side)."""
    w = NumericEntry(frame, fraction_mode=True)
    # Force a layout so positions are real.
    w.SetSize((300, 200))
    w.Layout()
    num_y = w.num_ctrl.GetScreenPosition().y
    bar_y = w.fraction_bar.GetScreenPosition().y
    den_y = w.den_ctrl.GetScreenPosition().y
    assert num_y < bar_y < den_y, (num_y, bar_y, den_y)
    # And they are vertically aligned (roughly same x), not side-by-side.
    num_x = w.num_ctrl.GetScreenPosition().x
    den_x = w.den_ctrl.GetScreenPosition().x
    assert abs(num_x - den_x) <= 4, (num_x, den_x)


def test_fraction_mode_round_trip(frame):
    w = NumericEntry(frame, fraction_mode=True)
    w.set_response({"numerator": 3, "denominator": 4})
    assert w.num_ctrl.GetValue() == "3"
    assert w.den_ctrl.GetValue() == "4"
    assert w.get_response() == {"numerator": 3, "denominator": 4}


def test_fraction_zero_denominator_is_invalid(frame):
    w = NumericEntry(frame, fraction_mode=True)
    w.set_response({"numerator": 1, "denominator": 0})
    assert w.get_response() == {}


def test_fraction_box_rejects_decimal_point(frame):
    """Numerator/denominator boxes are integers — no '.' allowed."""
    w = NumericEntry(frame, fraction_mode=True)
    from widgets.numeric_entry import _ALLOWED_INTEGER, _would_be_valid_integer
    _type(w, w.num_ctrl, _ALLOWED_INTEGER, _would_be_valid_integer, "1.5")
    assert w.num_ctrl.GetValue() == "15"  # '.' swallowed


# ── Unit / currency labels ────────────────────────────────────────────
def test_unit_prefix_suffix_render_via_constructor(frame):
    w = NumericEntry(frame, fraction_mode=False, prefix="$", suffix="feet")
    assert w._prefix_label.GetLabel() == "$"
    assert w._prefix_label.IsShown()
    assert w._suffix_label.GetLabel() == "feet"
    assert w._suffix_label.IsShown()


def test_no_unit_labels_hidden_by_default(frame):
    w = NumericEntry(frame, fraction_mode=False)
    assert not w._prefix_label.IsShown()
    assert not w._suffix_label.IsShown()


def test_set_unit_updates_labels(frame):
    w = NumericEntry(frame, fraction_mode=False)
    w.set_unit(prefix="$", suffix="dollars")
    assert w._prefix_label.GetLabel() == "$" and w._prefix_label.IsShown()
    assert w._suffix_label.GetLabel() == "dollars" and w._suffix_label.IsShown()
    # Clearing hides them again.
    w.set_unit(prefix=None, suffix=None)
    assert not w._prefix_label.IsShown()
    assert not w._suffix_label.IsShown()


def test_set_unit_works_in_fraction_mode(frame):
    w = NumericEntry(frame, fraction_mode=True)
    w.set_unit(prefix="$", suffix="ft")
    assert w._prefix_label.GetLabel() == "$" and w._prefix_label.IsShown()
    assert w._suffix_label.GetLabel() == "ft" and w._suffix_label.IsShown()


# ── get/set/clear round-trip (single) ─────────────────────────────────
def test_single_round_trip_and_clear(frame):
    w = NumericEntry(frame, fraction_mode=False)
    w.set_response({"value": "42.0"})
    assert w.value_ctrl.GetValue() == "42.0"
    assert w.get_response() == {"value": "42.0"}
    w.clear()
    assert w.value_ctrl.GetValue() == ""
    assert w.get_response() == {}


def test_clear_fraction(frame):
    w = NumericEntry(frame, fraction_mode=True)
    w.set_response({"numerator": 9, "denominator": 7})
    w.clear()
    assert w.num_ctrl.GetValue() == ""
    assert w.den_ctrl.GetValue() == ""
    assert w.get_response() == {}


# ── on_change callback fires ──────────────────────────────────────────
def test_on_change_callback_fires(frame):
    w = NumericEntry(frame, fraction_mode=False)
    seen = []
    w.set_on_change(lambda resp: seen.append(resp))
    w.value_ctrl.SetValue("7")  # triggers EVT_TEXT
    assert seen, "on_change should have fired"
    assert seen[-1] == {"value": "7"}
