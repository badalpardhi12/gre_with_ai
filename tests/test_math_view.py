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


def test_div_wrapper_does_not_block_newline_conversion():
    """GitHub #4, #5: QC prompts land in ``<div class="prompt">`` before
    the newline pass; the old guard treated `<div>` as a line-break
    carrier and collapsed every `\\n` in the QC question bank (411 live
    items). A bare `<div>` wrapper around plain-text-with-newlines must
    still convert."""
    raw = '<div class="prompt">Quantity A: x\nQuantity B: y</div>'
    out = _newlines_to_html(raw)
    assert out == '<div class="prompt">Quantity A: x<br>Quantity B: y</div>'


# ── Markdown pipe-table → HTML (GitHub #12) ───────────────────────────

from widgets.math_view import _markdown_tables_to_html


def test_md_table_basic():
    raw = (
        "| h1 | h2 |\n"
        "|---|---|\n"
        "| a | b |\n"
        "| c | d |\n"
    )
    out = _markdown_tables_to_html(raw)
    assert "<table" in out
    assert "<th" in out and "h1" in out and "h2" in out
    assert "<td" in out and "a" in out and "b" in out and "c" in out and "d" in out
    # No literal pipes leak into the output.
    assert "|" not in out


def test_md_table_alignment_hints():
    raw = (
        "| a | b | c |\n"
        "|:---|:---:|---:|\n"
        "| 1 | 2 | 3 |\n"
    )
    out = _markdown_tables_to_html(raw)
    assert "text-align:left" in out
    assert "text-align:center" in out
    assert "text-align:right" in out


def test_md_table_inside_div_wrapper():
    """GitHub #12 (Q3610): the markdown table lives inside a stimulus
    ``<div>...</div>`` wrapper followed by an ``<img>`` tag. The
    converter must find the table block regardless of surrounding HTML."""
    raw = (
        '<div>caption text\n'
        '\n'
        '| col1 | col2 |\n'
        '|---|---|\n'
        '| 70 | 246 |\n'
        '</div>'
        '<div><img src="data:image/png;base64,AAA"/></div>'
    )
    out = _markdown_tables_to_html(raw)
    assert "<table" in out
    assert "caption text" in out
    # The img survives too.
    assert "<img" in out
    # No pipe-table remnants.
    assert "|---|" not in out


def test_md_table_skipped_when_no_separator():
    """A single ``| a | b |`` line without the ``|---|`` separator is
    prose, not a table — leave it alone."""
    raw = "See the chart (axes | x: time | y: revenue)."
    assert _markdown_tables_to_html(raw) == raw


def test_md_table_no_op_without_pipes():
    assert _markdown_tables_to_html("plain prose") == "plain prose"
    assert _markdown_tables_to_html("") == ""
    assert _markdown_tables_to_html(None) is None  # type: ignore[arg-type]


def test_empty_input_pass_through():
    assert _newlines_to_html("") == ""
    assert _newlines_to_html(None) is None


# ─────────────────────────────────────────────────────────────────────
# Markdown inline rewrite (GitHub issue #2 regression)
# ─────────────────────────────────────────────────────────────────────
def test_markdown_bold_becomes_strong():
    assert _normalise_plain_math("**Solution:** answer") == "<strong>Solution:</strong> answer"


def test_markdown_underscore_bold_becomes_strong():
    assert _normalise_plain_math("__bold__ text") == "<strong>bold</strong> text"


def test_markdown_italic_becomes_em():
    # Italic needs a preceding non-word char to disambiguate from a
    # stray asterisk mid-word.
    assert _normalise_plain_math("it is *important* here") == "it is <em>important</em> here"


def test_markdown_does_not_leak_into_math_blocks():
    # Inside \(..\) the regex is never invoked.
    assert _normalise_plain_math(r"See \(3^{**2**}\) or done") == r"See \(3^{**2**}\) or done"


def test_markdown_bold_in_prose_with_adjacent_math():
    out = _normalise_plain_math(r"**Step 1:** compute \(x^2\) carefully")
    assert "<strong>Step 1:</strong>" in out
    assert r"\(x^2\)" in out


def test_markdown_no_asterisks_no_change():
    # Hot-path guard: text without `*` or `_` skips the regex.
    assert _normalise_plain_math("plain prose here") == "plain prose here"
