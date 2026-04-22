"""
Unit tests for `widgets.math_view` text-normalisation helpers.

The module also defines `MathView` (a wxPython panel), but those classes
need a wx app instance to instantiate — out of scope for the headless
test suite. The regex-driven helpers are pure-Python and test cleanly.

Regression context for the assertions below: the previous version of
`_normalise_plain_math` ran its `x^n` rewrite over the whole string,
including content already wrapped in `\\(...\\)`. That re-wrapped
`25^{x}` into `\\(25^{{x}}\\)` and broke KaTeX rendering for any
question with nested LaTeX. The new version splits on math blocks and
only normalises the prose between them.

`_newlines_to_html` was added so prompts stored as
"Quantity A: …\\nQuantity B: …" don't collapse to one visual line in
the WebView.
"""
import pytest

from widgets.math_view import _newlines_to_html, _normalise_plain_math


# ── _normalise_plain_math ──────────────────────────────────────────

def test_plain_sqrt_rewritten():
    out = _normalise_plain_math("the value of sqrt(3) is irrational")
    assert "\\(\\sqrt{3}\\)" in out


def test_plain_caret_rewritten_to_inline_latex():
    out = _normalise_plain_math("x^2 + y^2")
    assert "\\(x^{2}\\)" in out
    assert "\\(y^{2}\\)" in out


def test_existing_latex_block_left_intact():
    """The regression: a `25^{x}` inside an existing `\\(...\\)` must
    NOT be re-wrapped, otherwise the result is `\\(25^{{x}}\\)` which
    KaTeX renders as raw text."""
    raw = r"\(\left(\left(25^{x}\right)^{-2}\right)^{3}\)"
    out = _normalise_plain_math(raw)
    assert out == raw, f"plain-math rewriter touched a math block: {out!r}"


def test_mix_of_math_block_and_prose_normalises_only_prose():
    raw = r"Given x^2 = 1 and \(y^{3}\), find x^4 directly."
    out = _normalise_plain_math(raw)
    # Math block stays untouched.
    assert r"\(y^{3}\)" in out
    # Prose `x^2` and `x^4` got wrapped.
    assert r"\(x^{2}\)" in out
    assert r"\(x^{4}\)" in out


def test_normalise_handles_empty_input():
    assert _normalise_plain_math("") == ""
    assert _normalise_plain_math(None) is None


def test_display_math_block_left_intact():
    raw = r"\[\frac{a}{b}\] then x^2 follows"
    out = _normalise_plain_math(raw)
    assert r"\[\frac{a}{b}\]" in out
    assert r"\(x^{2}\)" in out


def test_dollar_display_math_block_left_intact():
    raw = r"$$y^{2} + 1$$ is positive when x^2 > 0"
    out = _normalise_plain_math(raw)
    assert r"$$y^{2} + 1$$" in out
    assert r"\(x^{2}\)" in out


# ── _newlines_to_html ──────────────────────────────────────────────

def test_single_newline_becomes_br():
    out = _newlines_to_html("Quantity A: x\nQuantity B: y")
    assert out == "Quantity A: x<br>Quantity B: y"


def test_double_newline_becomes_paragraph_break():
    out = _newlines_to_html("Setup line\n\nQuantity A: x\nQuantity B: y")
    # Two newlines → two <br> (paragraph-style break).
    assert "Setup line<br><br>Quantity A: x" in out


def test_html_input_left_alone():
    """If the prompt already has `<p>` tags, don't double-break it."""
    raw = "<p>Quantity A: x</p>\n<p>Quantity B: y</p>"
    out = _newlines_to_html(raw)
    assert out == raw  # unchanged


def test_input_with_existing_br_left_alone():
    raw = "Quantity A: x<br>Quantity B: y"
    assert _newlines_to_html(raw) == raw


def test_empty_input_pass_through():
    assert _newlines_to_html("") == ""
    assert _newlines_to_html(None) is None
