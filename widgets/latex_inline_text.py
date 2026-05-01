"""
LaTeX inline-math → readable Unicode text, for answer-option labels.

Question prompts and explanations render via ``MathView`` (KaTeX in a
WebView), so they support full LaTeX. Answer options are rendered as
``wx.StaticText`` (so the radio-button row can wrap cleanly), which
means raw LaTeX like ``\\(\\frac{1}{6}\\)`` shows up as literal text on
screen (GitHub #8, #9).

This module normalises the common LaTeX inline-math patterns the
question bank uses into Unicode so the StaticText labels are readable
without a re-architecture of the options UI. The grammar is intentionally
tiny — it targets the macros that actually appear in option.text in the
shipped DB, not a general LaTeX parser.

Out of scope: multi-line display math, matrices, integrals, nested
`\\sqrt`, aligned equations. Those render with delimiters stripped and
the macro names left in place, which is no worse than today.
"""
from __future__ import annotations

import re


# Unicode superscripts and subscripts for digits + the letters that
# actually exist as precomposed Unicode codepoints. ``str.maketrans``
# requires the two sides to be the same length, so pair them carefully.
_SUPERSCRIPT = str.maketrans(
    "0123456789+-=()abcdefghijklmnoprstuvwxyz",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ",
)
_SUBSCRIPT = str.maketrans(
    "0123456789+-=()aehijklmnoprstuvx",
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ",
)
_SUPERSCRIPTABLE = set("0123456789+-=()abcdefghijklmnoprstuvwxyz")
_SUBSCRIPTABLE = set("0123456789+-=()aehijklmnoprstuvx")

# Simple LaTeX macro → Unicode. Longer keys first so we don't partially
# match (e.g. \leq before \le).
_MACRO_UNICODE = [
    (r"\\times", "×"),
    (r"\\cdot", "·"),
    (r"\\div", "÷"),
    (r"\\pm", "±"),
    (r"\\mp", "∓"),
    (r"\\leq", "≤"),
    (r"\\geq", "≥"),
    (r"\\neq", "≠"),
    (r"\\le\b", "≤"),
    (r"\\ge\b", "≥"),
    (r"\\ne\b", "≠"),
    (r"\\approx", "≈"),
    (r"\\equiv", "≡"),
    (r"\\infty", "∞"),
    (r"\\sim", "∼"),
    (r"\\propto", "∝"),
    (r"\\circ", "°"),
    (r"\\degree", "°"),
    (r"\\to", "→"),
    (r"\\rightarrow", "→"),
    (r"\\leftarrow", "←"),
    (r"\\Rightarrow", "⇒"),
    (r"\\Leftarrow", "⇐"),
    (r"\\in\b", "∈"),
    (r"\\notin", "∉"),
    (r"\\subset", "⊂"),
    (r"\\supset", "⊃"),
    (r"\\cup", "∪"),
    (r"\\cap", "∩"),
    (r"\\emptyset", "∅"),
    (r"\\forall", "∀"),
    (r"\\exists", "∃"),
    (r"\\angle", "∠"),
    (r"\\triangle", "△"),
    (r"\\square", "□"),
    (r"\\bullet", "•"),
    (r"\\ldots", "…"),
    (r"\\dots", "…"),
    # Greek — uppercase first so \Pi doesn't match as \pi followed by I.
    (r"\\Alpha", "Α"), (r"\\Beta", "Β"), (r"\\Gamma", "Γ"),
    (r"\\Delta", "Δ"), (r"\\Theta", "Θ"), (r"\\Lambda", "Λ"),
    (r"\\Mu", "Μ"), (r"\\Pi", "Π"), (r"\\Sigma", "Σ"),
    (r"\\Phi", "Φ"), (r"\\Psi", "Ψ"), (r"\\Omega", "Ω"),
    (r"\\alpha", "α"), (r"\\beta", "β"), (r"\\gamma", "γ"),
    (r"\\delta", "δ"), (r"\\epsilon", "ε"), (r"\\zeta", "ζ"),
    (r"\\eta", "η"), (r"\\theta", "θ"), (r"\\iota", "ι"),
    (r"\\kappa", "κ"), (r"\\lambda", "λ"), (r"\\mu", "μ"),
    (r"\\nu", "ν"), (r"\\xi", "ξ"), (r"\\pi", "π"),
    (r"\\rho", "ρ"), (r"\\sigma", "σ"), (r"\\tau", "τ"),
    (r"\\phi", "φ"), (r"\\chi", "χ"), (r"\\psi", "ψ"),
    (r"\\omega", "ω"),
    # Escaped punctuation.
    (r"\\\$", "$"), (r"\\%", "%"), (r"\\#", "#"),
    (r"\\&", "&"), (r"\\_", "_"),
    # Thin-space-ish macros the bank uses as visual spacers.
    (r"\\,", " "), (r"\\;", " "), (r"\\:", " "), (r"\\ ", " "),
    # Text-mode macros — just strip the wrapper.
    (r"\\text\s*\{([^{}]*)\}", r"\1"),
    (r"\\mathrm\s*\{([^{}]*)\}", r"\1"),
    (r"\\operatorname\s*\{([^{}]*)\}", r"\1"),
    # Left/right delimiter decorators — keep the delimiter, drop the macro.
    (r"\\left", ""), (r"\\right", ""),
]

# `\frac{a}{b}` — when both halves are trivial numerals/letters use the
# Unicode fraction slash (e.g. 1⁄6); otherwise parenthesise for clarity.
# Also covers ``\dfrac`` (display-style) and ``\tfrac`` (text-style)
# which Manhattan-5lb items frequently use.
_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")

# Thousands-separator brace artefact — ``100{,}000`` is LaTeX for
# "render a comma without a digit gap", but the raw text breaks our
# non-recursive brace matching in ``\frac{...}{...}``. Strip it
# pre-pass so the fraction regex sees flat numerals.
_THOUSANDS_BRACE_RE = re.compile(r"\{,\}")

# `\sqrt{...}` — small radicand keeps as ``√x``, longer gets ``√(expr)``.
_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")

# ``x^{abc}`` / ``x^a`` — convert digits and the small alphabet to
# superscript characters where the input is trivially mappable.
_SUP_BRACED_RE = re.compile(r"\^\{([^{}]+)\}")
_SUP_BARE_RE = re.compile(r"\^([A-Za-z0-9])")
_SUB_BRACED_RE = re.compile(r"_\{([^{}]+)\}")
_SUB_BARE_RE = re.compile(r"_([A-Za-z0-9])")

# Inline-math delimiters the bank uses.
_INLINE_DELIM_RE = re.compile(r"\\\(|\\\)|\\\[|\\\]|\$\$")


def _to_superscript(inner: str) -> str:
    stripped = inner.strip()
    if stripped and all(c in _SUPERSCRIPTABLE for c in stripped):
        return stripped.translate(_SUPERSCRIPT)
    # Not fully mappable — preserve explicit ``^`` notation so the
    # reader still sees the exponent.
    return f"^({stripped})" if len(stripped) > 1 else f"^{stripped}"


def _to_subscript(inner: str) -> str:
    stripped = inner.strip()
    if stripped and all(c in _SUBSCRIPTABLE for c in stripped):
        return stripped.translate(_SUBSCRIPT)
    return f"_({stripped})" if len(stripped) > 1 else f"_{stripped}"


def _render_frac(match: "re.Match[str]") -> str:
    num, den = match.group(1).strip(), match.group(2).strip()
    # A "simple" token is a bare numeric/letter run, possibly with a
    # decimal point or thousands-separator commas. Anything with
    # operators, whitespace, or grouping gets parenthesised for
    # precedence clarity.
    simple = re.compile(r"^[A-Za-z0-9.,]+$")
    if simple.match(num) and simple.match(den):
        return f"{num}⁄{den}"
    lhs = num if simple.match(num) else f"({num})"
    rhs = den if simple.match(den) else f"({den})"
    return f"{lhs}/{rhs}"


def _render_sqrt(match: "re.Match[str]") -> str:
    inner = match.group(1).strip()
    if re.match(r"^[A-Za-z0-9]+$", inner):
        return f"√{inner}"
    return f"√({inner})"


def latex_inline_to_text(text: str) -> str:
    """Normalise LaTeX inline-math in *text* into readable Unicode.

    The function is idempotent: running it twice is a no-op. It preserves
    non-math text verbatim (no sanitisation, no whitespace changes beyond
    what the macro substitutions produce)."""
    if not text or "\\" not in text and "^" not in text and "_" not in text:
        return text

    out = text

    # 0) Strip the ``{,}`` thousands-separator artefact so our
    # non-recursive fraction regex sees flat numerals.
    out = _THOUSANDS_BRACE_RE.sub(",", out)
    # 1) Fractions first — they often wrap other macros we rewrite below.
    out = _FRAC_RE.sub(_render_frac, out)
    # 2) Square roots.
    out = _SQRT_RE.sub(_render_sqrt, out)
    # 3) Named macros (longer first via the ordered list).
    for pattern, repl in _MACRO_UNICODE:
        out = re.sub(pattern, repl, out)
    # 4) Super/subscripts.
    out = _SUP_BRACED_RE.sub(lambda m: _to_superscript(m.group(1)), out)
    out = _SUP_BARE_RE.sub(lambda m: _to_superscript(m.group(1)), out)
    out = _SUB_BRACED_RE.sub(lambda m: _to_subscript(m.group(1)), out)
    out = _SUB_BARE_RE.sub(lambda m: _to_subscript(m.group(1)), out)
    # 5) Finally strip the inline-math delimiters.
    out = _INLINE_DELIM_RE.sub("", out)

    return out
