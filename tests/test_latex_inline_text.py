"""Tests for ``widgets.latex_inline_text.latex_inline_to_text``.

The helper renders LaTeX inline-math into readable Unicode for the
answer-option labels that render as ``wx.StaticText``. Scope: the
patterns actually present in the shipped question bank (GitHub #8, #9).
"""
from widgets.latex_inline_text import latex_inline_to_text


# ── Real-world option text from the shipped DB ─────────────────────────

def test_simple_fraction():
    """GitHub #8 (Q1801): option A was ``\\(\\frac{1}{6}\\)``."""
    assert latex_inline_to_text(r"\(\frac{1}{6}\)") == "1⁄6"


def test_linear_equation_strips_delimiters():
    """GitHub #9 (Q3726): option A was ``\\(6y + 6x = 7\\)``."""
    assert latex_inline_to_text(r"\(6y + 6x = 7\)") == "6y + 6x = 7"


def test_vertical_line_option():
    """GitHub #9 (Q3726): option E was ``\\(x = -2\\)``."""
    assert latex_inline_to_text(r"\(x = -2\)") == "x = -2"


# ── Individual macro coverage ──────────────────────────────────────────

def test_fraction_with_complex_denominator_uses_parens():
    assert latex_inline_to_text(r"\frac{1}{n+1}") == "1/(n+1)"


def test_dfrac_and_tfrac_treated_like_frac():
    """Manhattan-5lb uses \\dfrac liberally. Regression for the
    'displayed as raw macro' bug in the dry-run sample."""
    assert latex_inline_to_text(r"\dfrac{1}{6}") == "1⁄6"
    assert latex_inline_to_text(r"\tfrac{a}{b}") == "a⁄b"


def test_thousands_separator_brace_stripped():
    """``100{,}000`` is a LaTeX non-breaking-thousand-separator idiom;
    strip the ``{,}`` artefact so the fraction regex still matches."""
    assert latex_inline_to_text(r"\frac{100{,}000}{100{,}000b}") == "100,000⁄100,000b"


def test_sqrt_simple():
    assert latex_inline_to_text(r"\sqrt{3}") == "√3"


def test_sqrt_complex():
    assert latex_inline_to_text(r"\sqrt{a+b}") == "√(a+b)"


def test_times_cdot_div():
    assert latex_inline_to_text(r"2 \times 3") == "2 × 3"
    assert latex_inline_to_text(r"a \cdot b") == "a · b"
    assert latex_inline_to_text(r"10 \div 2") == "10 ÷ 2"


def test_inequalities():
    assert latex_inline_to_text(r"x \leq 5") == "x ≤ 5"
    assert latex_inline_to_text(r"x \geq 5") == "x ≥ 5"
    assert latex_inline_to_text(r"x \neq 0") == "x ≠ 0"
    assert latex_inline_to_text(r"x \le 5") == "x ≤ 5"


def test_greek_letters():
    assert latex_inline_to_text(r"\pi r^2") == "π r²"
    assert latex_inline_to_text(r"\theta") == "θ"


def test_pm_mp():
    assert latex_inline_to_text(r"a \pm b") == "a ± b"


def test_superscript_digit():
    assert latex_inline_to_text(r"x^2") == "x²"


def test_superscript_braced():
    assert latex_inline_to_text(r"x^{10}") == "x¹⁰"


def test_superscript_fallback_for_unmappable():
    # "Σ" has no superscript codepoint — fall back to ^(...)
    out = latex_inline_to_text(r"a^{Σ}")
    assert "Σ" in out and "^" in out


def test_subscript_digit():
    assert latex_inline_to_text(r"x_1") == "x₁"


def test_subscript_braced():
    assert latex_inline_to_text(r"x_{10}") == "x₁₀"


# ── Idempotency / pass-through ────────────────────────────────────────

def test_plain_text_passthrough():
    assert latex_inline_to_text("Quantity A is greater.") == "Quantity A is greater."


def test_empty_passthrough():
    assert latex_inline_to_text("") == ""
    assert latex_inline_to_text(None) is None  # type: ignore[arg-type]


def test_idempotent():
    once = latex_inline_to_text(r"\(\frac{1}{6}\)")
    twice = latex_inline_to_text(once)
    assert once == twice


# ── Safety: the option renderer in question_screen calls this on EVERY
# option label, so regression-guard it against pathological but legal
# LaTeX ───────────────────────────────────────────────────────────────

def test_text_macro_unwrapped():
    assert latex_inline_to_text(r"\text{hello}") == "hello"


def test_left_right_delimiters_stripped():
    assert latex_inline_to_text(r"\left(a+b\right)") == "(a+b)"


def test_escaped_punctuation():
    assert latex_inline_to_text(r"\$5") == "$5"
    assert latex_inline_to_text(r"50\%") == "50%"
