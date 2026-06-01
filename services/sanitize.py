"""LaTeX / HTML / MCQ encoding sweep utilities.

Phase 3.3 of the cleanup plan (docs/implementation_plan_2026_05_18.md
§322-330). Two responsibilities:

1.  ``find_latex_encoding_issues(text)`` — pure-function detector that
    returns a list of ``(issue_type, snippet)`` tuples for the
    encoding hazards that have caused renderer regressions in the
    past:

    * ``unmatched_dollar``    — odd count of ``$`` (KaTeX inline
                                delimiter parity broken).
    * ``unmatched_dollar_dd`` — odd count of ``$$`` (display).
    * ``mixed_delimiters``    — both ``$…$`` and ``\\(…\\)`` in
                                the same string. Both are valid
                                LaTeX, but mixing them confuses the
                                KaTeX auto-render contrib because it
                                tries the first match first.
    * ``smart_quotes_in_math``— curly quotes inside a math span
                                (KaTeX rejects them with a parse
                                error).
    * ``nbsp_in_math``        — non-breaking-space (literal U+00A0
                                or the entity ``&nbsp;``) inside a
                                math span. KaTeX's lexer doesn't
                                recognise it.
    * ``unescaped_html``      — bare ``<``, ``>``, or ``&`` outside
                                a recognised HTML tag.
    * ``mixed_mcq_prefix``    — when applied to a *list* of option
                                texts via ``find_mcq_option_issues``
                                — surface mixed prefix conventions
                                such as ``"(A) "`` vs ``"A. "`` vs
                                ``"A) "`` vs no prefix.

2.  ``sanitize_html(raw, mode='lenient')`` — thin wrapper around
    ``widgets.html_sanitizer.safe_html`` that adds the strict/lenient
    mode parity asked for in the plan. ``strict`` raises a
    ``LatexEncodingError`` when a hazard is detected; ``lenient``
    logs and returns a best-effort fix.

The detector is intentionally conservative — it flags suspicious
patterns rather than overreaching, because a false positive becomes
a SME-review burden.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, List, Optional, Sequence, Tuple

# We keep ``safe_html`` as the canonical HTML sanitiser; this module
# only adds the issue-detection + auto-fix layer on top of it.
from widgets.html_sanitizer import safe_html as _safe_html

logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────


class LatexEncodingError(ValueError):
    """Raised by ``sanitize_html(..., mode='strict')`` when one or more
    encoding hazards are detected in the input."""


# ── Issue-type constants ─────────────────────────────────────────────

ISSUE_UNMATCHED_DOLLAR = "unmatched_dollar"
ISSUE_UNMATCHED_DOLLAR_DD = "unmatched_dollar_dd"
ISSUE_MIXED_DELIMITERS = "mixed_delimiters"
ISSUE_SMART_QUOTES_IN_MATH = "smart_quotes_in_math"
ISSUE_NBSP_IN_MATH = "nbsp_in_math"
ISSUE_UNESCAPED_HTML = "unescaped_html"
ISSUE_MIXED_MCQ_PREFIX = "mixed_mcq_prefix"
ISSUE_TRIPLE_DOLLAR = "triple_dollar"

ALL_ISSUE_TYPES = frozenset({
    ISSUE_UNMATCHED_DOLLAR,
    ISSUE_UNMATCHED_DOLLAR_DD,
    ISSUE_MIXED_DELIMITERS,
    ISSUE_SMART_QUOTES_IN_MATH,
    ISSUE_NBSP_IN_MATH,
    ISSUE_UNESCAPED_HTML,
    ISSUE_MIXED_MCQ_PREFIX,
    ISSUE_TRIPLE_DOLLAR,
})


# ── Regex helpers ────────────────────────────────────────────────────

# A ``$`` preceded by ``\`` is escaped — not a delimiter.
# We strip those first via ``_strip_escaped_dollars`` so the parity
# checks operate on real delimiters.
_ESCAPED_DOLLAR_RE = re.compile(r"\\\$")

# ``$$`` blocks — stripped before counting bare ``$`` so display-math
# delimiters don't pollute the inline-math parity check.
_DD_BLOCK_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)

# ``\(...\)`` and ``\[...\]`` — inline / display math. Stripped before
# counting bare ``$`` because they represent already-paired math.
_BACKSLASH_DELIM_RE = re.compile(r"\\[\(\[].*?\\[\)\]]", re.DOTALL)

# Smart-quote codepoints. Either side of these is bad inside math:
# ``\frac{1}{2}`` rendered with U+2018 instead of straight quotes
# isn't actually a thing, but author imports occasionally drop
# curly quotes inside an inline span.
_SMART_QUOTE_CHARS = "‘’‚‛“”„‟«»"
_SMART_QUOTE_CLASS = re.compile(f"[{_SMART_QUOTE_CHARS}]")
_SMART_QUOTE_TRANSLATE = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "«": '"', "»": '"',
})

# NBSP detection inside math. KaTeX's lexer trips on U+00A0 and the
# HTML entity ``&nbsp;``.
_NBSP_RE = re.compile(r" |&nbsp;", re.IGNORECASE)

# Math-span finder for the smart-quote / NBSP checks. We deliberately
# accept all four canonical math delimiter pairs *except* bare ``$…$``
# — that pattern produces too many false positives with currency
# amounts (``$5 per … $63 cumulative``). KaTeX still scans bare
# ``$…$`` in production, but the *encoding-hazard* check restricts
# itself to unambiguous math regions.
_MATH_SPAN_RE = re.compile(
    r"\\\(.*?\\\)|\\\[.*?\\\]|\$\$.*?\$\$",
    re.DOTALL,
)

# Bare HTML angle-bracket. We allow the canonical tag set from the
# bleach allowlist — anything else is suspect.
# We compile a permissive tag-name regex; the actual tag-allowlist
# check happens after lookup.
_HTML_TAG_RE = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")
# A ``<`` or ``>`` is suspect ONLY when it looks like a malformed
# tag (e.g. ``<3``, ``<table missing close``). Bare math comparisons
# in prose (``a > b``, ``x < 5``) are NOT a hazard — KaTeX never
# touches characters outside its delimiters and the bleach pass
# leaves bare angles alone if they don't open a tag.
#
# Concretely we flag:
#   * ``<word`` immediately followed by a non-letter and no later ``>``
#     before the next non-tag character — i.e. ``<malformed`` with
#     nothing closing it.
#   * a ``>`` before any ``<`` that isn't accounted for by an HTML tag.
#
# Both checks are deliberately tight; the detector errs toward false
# negatives.
_LIKELY_BAD_LT_RE = re.compile(r"<[^a-zA-Z\s/!?>][^>]*?(?:$|<|>)")
# Bare ``&`` not part of a valid named entity or numeric reference.
_VALID_ENTITY_RE = re.compile(
    r"&(?:[a-zA-Z][a-zA-Z0-9]{1,30}|#[0-9]{1,7}|#x[0-9a-fA-F]{1,6});"
)
_BARE_AMP_RE = re.compile(r"&(?![a-zA-Z#])")

# MCQ-prefix detectors. The four conventions we see in the wild:
#   "(A) text"      — paren-prefix
#   "A. text"       — dot-prefix
#   "A) text"       — paren-suffix
#   "text"          — no prefix
_MCQ_PAREN_RE = re.compile(r"^\s*\(\s*[A-Z]\s*\)\s+")
_MCQ_DOT_RE = re.compile(r"^\s*[A-Z]\.\s+")
_MCQ_PARENSUF_RE = re.compile(r"^\s*[A-Z]\)\s+")

# Triple-dollar: ``$$$`` or longer. Almost always a markdown copy-paste
# artifact (someone wrote ``$$ $$`` and then tried to escape with a
# third ``$``). Always fixable to ``$$``.
_TRIPLE_DOLLAR_RE = re.compile(r"\${3,}")


# ── Internal helpers ─────────────────────────────────────────────────


def _strip_for_dollar_parity(text: str) -> str:
    """Remove escaped ``$``, ``\\(...\\)``/``\\[...\\]`` math, and
    ``$$...$$`` blocks so the remaining ``$`` count reflects only
    bare inline-math delimiters.

    Also strips ``literal-dollar-amount`` patterns: ``$N`` or ``$N.NN``
    where N is digits — these are currency, not math. We keep this
    pre-pass conservative: we only strip when the ``$`` is followed
    by a digit and bounded by whitespace/punctuation/end.
    """
    s = _ESCAPED_DOLLAR_RE.sub("", text)
    s = _DD_BLOCK_RE.sub("", s)
    s = _BACKSLASH_DELIM_RE.sub("", s)
    # Strip currency-style ``$5``, ``$1,200``, ``$3.50``, ``$.99`` so
    # they don't skew the parity check. Must be preceded by start/space/
    # punctuation and followed by a digit.
    s = re.sub(
        r"(^|[\s\(\[\{,;:>])\$\d[\d,\.]*",
        r"\1",
        s,
    )
    # Same for ``$.50`` (no leading digit).
    s = re.sub(
        r"(^|[\s\(\[\{,;:>])\$\.\d+",
        r"\1",
        s,
    )
    return s


def _count_dd_pairs(text: str) -> Tuple[int, int]:
    """Return ``(num_dd_pairs, num_lone_dd)``.

    ``$$`` is the display-math delimiter — they MUST come in pairs.
    Triple-dollar (``$$$``) is always a hazard, regardless of count.
    """
    # Strip escaped first.
    s = _ESCAPED_DOLLAR_RE.sub("", text)
    # Triple-dollar collapses to leftover lone ``$$``-or-more — flagged
    # separately by the triple-dollar detector. We count strict ``$$``
    # occurrences here (not greedy).
    occ = re.findall(r"\$\$", s)
    n = len(occ)
    return n // 2, n % 2


def _has_inline_math_paren(text: str) -> bool:
    return bool(re.search(r"\\\(", text))


def _has_inline_math_dollar(text: str) -> bool:
    """Detect bare inline ``$…$`` — i.e. a ``$`` not part of ``$$``,
    not escaped, that is followed by something that looks like math
    content."""
    stripped = _strip_for_dollar_parity(text)
    return bool(re.search(r"(?<!\$)\$(?!\$)[^$]+\$", stripped))


def _truncate_snippet(text: str, around: int = 0, width: int = 80) -> str:
    """Return a short context window around ``around`` for human review."""
    if len(text) <= width:
        return text
    half = width // 2
    start = max(0, around - half)
    end = min(len(text), start + width)
    snip = text[start:end]
    if start > 0:
        snip = "…" + snip
    if end < len(text):
        snip = snip + "…"
    return snip


# ── Public API ───────────────────────────────────────────────────────


def find_latex_encoding_issues(
    text: Optional[str],
) -> List[Tuple[str, str]]:
    """Return ``[(issue_type, snippet), ...]`` for all encoding hazards
    found in *text*.

    Returns ``[]`` for ``None``, an empty string, or text that has no
    detectable hazards.

    The detector is *conservative*: it errs toward false negatives
    rather than spamming SME review with false positives. In particular:

    * ``$5 per item`` is NOT flagged — ``$`` followed by a digit and
      a word, with no math-signature characters in between, is treated
      as a literal dollar amount.
    * ``<3`` outside a tag-like context IS flagged as ``unescaped_html``.
    * NBSP outside math is NOT flagged (markup whitespace is fine).
    """
    if not text:
        return []

    issues: List[Tuple[str, str]] = []

    # 1) Triple-dollar — flag first because it would skew the parity
    #    counters downstream. Only flag once per text (we just need the
    #    SME to know it's there).
    triple_dollar_matches = list(_TRIPLE_DOLLAR_RE.finditer(text))
    if triple_dollar_matches:
        issues.append((
            ISSUE_TRIPLE_DOLLAR,
            _truncate_snippet(text, triple_dollar_matches[0].start()),
        ))

    # 2) Unmatched ``$$`` (display) — odd count is broken.
    _, lone_dd = _count_dd_pairs(text)
    if lone_dd:
        # Show the first lone ``$$`` for context.
        m = re.search(r"\$\$", _ESCAPED_DOLLAR_RE.sub("", text))
        snip = _truncate_snippet(text, m.start() if m else 0)
        issues.append((ISSUE_UNMATCHED_DOLLAR_DD, snip))

    # 3) Unmatched bare ``$`` (inline) — strip ``$$`` blocks and
    #    backslash-delimited math first, then count remaining ``$``.
    #    Skip when triple-dollar fired — that detector already covers
    #    the same hazard (and the parity check below would double-count).
    if not triple_dollar_matches:
        stripped = _strip_for_dollar_parity(text)
        n_bare = stripped.count("$")
        if n_bare % 2 == 1:
            m = re.search(r"\$", stripped)
            snip = _truncate_snippet(text, m.start() if m else 0)
            issues.append((ISSUE_UNMATCHED_DOLLAR, snip))

    # 4) Mixed delimiters — both ``\(...\)`` and bare ``$...$`` in the
    #    same string. (Mixing ``\(...\)`` with ``$$...$$`` is fine —
    #    that's display vs inline math.)
    if _has_inline_math_paren(text) and _has_inline_math_dollar(text):
        issues.append((ISSUE_MIXED_DELIMITERS, _truncate_snippet(text, 0)))

    # 5) Smart quotes / NBSP inside any math span.
    has_smart_quote_in_math = False
    has_nbsp_in_math = False
    for span in _MATH_SPAN_RE.finditer(text):
        body = span.group(0)
        if not has_smart_quote_in_math and _SMART_QUOTE_CLASS.search(body):
            issues.append((ISSUE_SMART_QUOTES_IN_MATH,
                           _truncate_snippet(text, span.start())))
            has_smart_quote_in_math = True
        if not has_nbsp_in_math and _NBSP_RE.search(body):
            issues.append((ISSUE_NBSP_IN_MATH,
                           _truncate_snippet(text, span.start())))
            has_nbsp_in_math = True
        if has_smart_quote_in_math and has_nbsp_in_math:
            break

    # 6) Unescaped HTML. We are deliberately conservative here:
    #    bare-angle math comparisons (``a > b``, ``x < 5``) appear
    #    in millions of words of prose and are NOT hazardous — the
    #    bleach pass passes them through unchanged and KaTeX only
    #    looks inside its delimiters. We flag only:
    #      (a) clearly malformed tags (``<3``, ``<bad``)
    #      (b) bare ``&`` that doesn't form a valid entity
    if _LIKELY_BAD_LT_RE.search(text):
        m = _LIKELY_BAD_LT_RE.search(text)
        issues.append((ISSUE_UNESCAPED_HTML,
                       _truncate_snippet(text, m.start())))
    else:
        for m in _BARE_AMP_RE.finditer(text):
            # Only flag when the surrounding context isn't an HTML
            # attribute value or URL query string. We don't have a
            # full parser, so use a cheap proxy: skip if the bare
            # ``&`` is between quotes.
            ctx_start = max(0, m.start() - 32)
            ctx = text[ctx_start:m.start()]
            if '="' in ctx or "='" in ctx:
                continue
            issues.append((ISSUE_UNESCAPED_HTML,
                           _truncate_snippet(text, m.start())))
            break

    return issues


def find_mcq_option_issues(
    options: Sequence[str],
) -> List[Tuple[str, str]]:
    """Detect mixed MCQ-prefix conventions across a *list* of option
    texts. Returns ``[]`` if all options use the same convention
    (or no detectable convention)."""
    if not options or len(options) < 2:
        return []

    styles = []
    for opt in options:
        if not isinstance(opt, str):
            opt = str(opt or "")
        if _MCQ_PAREN_RE.match(opt):
            styles.append("paren")
        elif _MCQ_DOT_RE.match(opt):
            styles.append("dot")
        elif _MCQ_PARENSUF_RE.match(opt):
            styles.append("parensuf")
        else:
            styles.append("none")

    distinct = set(styles)
    # Only flag when *prefixed* options coexist with un-prefixed or a
    # different prefix style. ``{none}`` alone is fine; ``{paren}``
    # alone is fine.
    if len(distinct) <= 1:
        return []
    # ``{none, paren}`` mixed is suspicious only if at least one
    # option carries an explicit letter prefix. The same goes for
    # mixed ``paren`` + ``dot`` etc.
    snip = "; ".join(
        f"{style}:{(opt[:24] + '…') if len(opt) > 24 else opt}"
        for style, opt in zip(styles, options)
    )
    return [(ISSUE_MIXED_MCQ_PREFIX, snip[:200])]


def auto_fix_text(text: Optional[str]) -> Tuple[str, List[str]]:
    """Apply *only* the unambiguous fixes to *text*.

    Returns ``(fixed_text, applied_fix_codes)``. The fix codes are the
    ISSUE_* constants of every transform we applied (so the caller can
    log what changed).

    Currently fixed:

    * Smart quotes inside math spans → straight quotes (whole-text
      smart-quote rewrite would damage prose like "the senator's
      memoir", so we constrain to math-span bodies).
    * ``$$$``+ → ``$$``.
    * NBSP inside math → regular space.
    * Mixed inline ``$…$`` + ``\\(…\\)`` — canonicalised to
      ``\\(…\\)`` ONLY when both forms coexist. Bare ``$…$`` alone is
      left untouched (it's valid LaTeX; some authors prefer it).

    Idempotent: applying twice produces the same result as once.
    """
    if not text:
        return text or "", []

    applied: List[str] = []
    out = text

    # 1) Triple-dollar collapse.
    if _TRIPLE_DOLLAR_RE.search(out):
        out = _TRIPLE_DOLLAR_RE.sub("$$", out)
        applied.append(ISSUE_TRIPLE_DOLLAR)

    # 2) Smart-quote / NBSP inside math spans.
    smart_q_fixed = False
    nbsp_fixed = False

    def _fix_math_span(match: "re.Match[str]") -> str:
        nonlocal smart_q_fixed, nbsp_fixed
        body = match.group(0)
        new = body
        if _SMART_QUOTE_CLASS.search(new):
            new = new.translate(_SMART_QUOTE_TRANSLATE)
            smart_q_fixed = True
        if _NBSP_RE.search(new):
            new = _NBSP_RE.sub(" ", new)
            nbsp_fixed = True
        return new

    out = _MATH_SPAN_RE.sub(_fix_math_span, out)
    if smart_q_fixed:
        applied.append(ISSUE_SMART_QUOTES_IN_MATH)
    if nbsp_fixed:
        applied.append(ISSUE_NBSP_IN_MATH)

    # 3) Mixed-delimiter canonicalisation: when BOTH forms exist,
    #    convert bare ``$…$`` → ``\(…\)``. Skip when only one form is
    #    present.
    if _has_inline_math_paren(out) and _has_inline_math_dollar(out):
        # The inline-dollar regex from latex_inline_text — same idea:
        # only convert when the inner content has a math signature
        # (\, ^, _) so dollar amounts like ``$5`` stay literal.
        def _wrap_paren(match: "re.Match[str]") -> str:
            inner = match.group(1)
            if any(ch in inner for ch in ("\\", "^", "_")):
                return f"\\({inner}\\)"
            return match.group(0)
        # Match a paired ``$…$`` that doesn't span ``$$`` boundaries.
        out_new = re.sub(r"(?<!\$)\$([^$]+?)\$(?!\$)", _wrap_paren, out)
        if out_new != out:
            out = out_new
            applied.append(ISSUE_MIXED_DELIMITERS)

    return out, applied


# ── Single-dollar inline-math conversion ─────────────────────────────
#
# The KaTeX auto-render config in ``widgets/math_view.py`` only registers
# the ``$$…$$``, ``\(…\)`` and ``\[…\]`` delimiter pairs — NOT bare
# ``$…$``. Explanations authored with single-dollar inline math therefore
# render with literal ``$`` signs and raw LaTeX source. ``audit_encoding_issues.py``
# flags these as ``unmatched_dollar``.
#
# ``convert_single_dollar_math`` rewrites balanced single-``$`` math spans
# to ``\(…\)`` while leaving currency dollars ("$48", "$5 and $3") strictly
# alone. The discriminator is a *strong LaTeX-structural* signature: a span
# is only converted when its body contains a backslash command or one of
# ``^ _ { }``. Bare ``=``/``<``/``>``/arithmetic are deliberately NOT
# treated as a sufficient signal — currency-heavy word problems routinely
# put ``=`` between dollar amounts ("Cumulative = $63 + $64 = $127"), and
# pairing across those would corrupt the text. Erring toward false
# negatives (leaving a pure-arithmetic ``$…$`` span untouched, where it at
# worst renders as plain text) is far safer than a false positive that
# mangles a currency amount.

# Placeholder sentinels used while masking spans that must NOT be touched.
# Chosen from the Unicode Private Use Area so they cannot collide with any
# real content.
_DD_MASK = "\uE000"   # $$ display span
_BS_MASK = "\uE001"   # backslash-delimited span
_ESC_MASK = "\uE002"  # escaped dollar (currency inside text or math)

# Strong LaTeX-structural signature: a backslash command or sub/superscript
# or a brace group. Currency runs (digits, commas, periods, $) never carry
# these, so this is the safe discriminator.
_MATH_SIGNATURE_RE = re.compile(r"[\\^_{}]")

# A balanced single-``$`` span whose body has no interior ``$`` and is not
# adjacent to another ``$`` (so ``$$`` display delimiters are excluded —
# though we mask those first anyway).
_SINGLE_DOLLAR_SPAN_RE = re.compile(r"(?<!\$)\$([^$\n]+?)\$(?!\$)")


def convert_single_dollar_math(text: Optional[str]) -> str:
    """Convert balanced single-``$`` inline-math spans to ``\\(…\\)``.

    Safe-by-design:

    * ``$$…$$`` display spans, existing ``\\(…\\)`` / ``\\[…\\]`` spans, and
      escaped ``\\$`` are masked out first, so they are never re-processed
      (idempotent — running twice is a no-op).
    * A ``$…$`` span is converted **only** when its body carries a strong
      LaTeX signature (a backslash command, ``^``, ``_``, ``{`` or ``}``).
      Pure currency (``$48``, ``$5``) and currency arithmetic
      (``$63 + $64 = $127``) carry no such signature and are left exactly
      as written.
    * The span body must not span a newline (``$`` parity across paragraph
      breaks is almost always two unrelated currency amounts, not a math
      span).

    Returns ``text`` unchanged when it is ``None``/empty or contains no
    convertible span.
    """
    if not text:
        return text or ""

    # 1) Mask spans that must be preserved verbatim. We stash the originals
    #    in order and restore them positionally at the end.
    stash: List[str] = []

    def _mask(pattern: "re.Pattern[str]", sentinel: str, s: str) -> str:
        def _repl(m: "re.Match[str]") -> str:
            stash.append(m.group(0))
            return f"{sentinel}{len(stash) - 1}{sentinel}"
        return pattern.sub(_repl, s)

    out = text
    out = _mask(_DD_BLOCK_RE, _DD_MASK, out)          # $$…$$
    out = _mask(_BACKSLASH_DELIM_RE, _BS_MASK, out)   # \(…\) / \[…\]
    out = _mask(_ESCAPED_DOLLAR_RE, _ESC_MASK, out)   # \$

    # 2) Convert qualifying single-$ spans.
    def _convert(m: "re.Match[str]") -> str:
        body = m.group(1)
        if _MATH_SIGNATURE_RE.search(body):
            return f"\\({body}\\)"
        return m.group(0)

    out = _SINGLE_DOLLAR_SPAN_RE.sub(_convert, out)

    # 3) Restore masked spans (innermost sentinels resolve correctly because
    #    each sentinel wraps its own stash index).
    def _unmask(sentinel: str, s: str) -> str:
        def _repl(m: "re.Match[str]") -> str:
            return stash[int(m.group(1))]
        return re.sub(f"{sentinel}(\\d+){sentinel}", _repl, s)

    out = _unmask(_ESC_MASK, out)
    out = _unmask(_BS_MASK, out)
    out = _unmask(_DD_MASK, out)
    return out


def sanitize_html(
    raw: Optional[str],
    *,
    mode: str = "lenient",
) -> str:
    """Sanitise *raw* HTML using the bleach allow-list, plus check it
    for LaTeX-encoding hazards.

    Parameters
    ----------
    raw : str or None
        Untrusted input. ``None`` becomes ``""``.
    mode : ``"strict"`` | ``"lenient"``
        ``"strict"`` raises ``LatexEncodingError`` listing every
        detected hazard. ``"lenient"`` (the default) auto-fixes
        unambiguous issues, logs anything left, and returns the
        bleach-cleaned result.

    Returns
    -------
    str
        Sanitised HTML. Always returns a string; never ``None``.
    """
    if mode not in ("strict", "lenient"):
        raise ValueError(
            f"sanitize_html: mode must be 'strict' or 'lenient', got {mode!r}"
        )

    if not raw:
        return ""

    issues = find_latex_encoding_issues(raw)
    if mode == "strict" and issues:
        types = ", ".join(sorted({i for i, _ in issues}))
        raise LatexEncodingError(
            f"sanitize_html(strict): detected encoding issues — {types}"
        )

    if mode == "lenient" and issues:
        fixed, applied = auto_fix_text(raw)
        unfixed = [
            (i, snip) for (i, snip) in find_latex_encoding_issues(fixed)
        ]
        if applied:
            logger.info(
                "sanitize_html: auto-fixed %s",
                ", ".join(applied),
            )
        if unfixed:
            logger.warning(
                "sanitize_html: %d unresolved encoding hazard(s): %s",
                len(unfixed),
                ", ".join({i for i, _ in unfixed}),
            )
        raw = fixed

    return _safe_html(raw)


__all__ = [
    "ALL_ISSUE_TYPES",
    "ISSUE_MIXED_DELIMITERS",
    "ISSUE_MIXED_MCQ_PREFIX",
    "ISSUE_NBSP_IN_MATH",
    "ISSUE_SMART_QUOTES_IN_MATH",
    "ISSUE_TRIPLE_DOLLAR",
    "ISSUE_UNESCAPED_HTML",
    "ISSUE_UNMATCHED_DOLLAR",
    "ISSUE_UNMATCHED_DOLLAR_DD",
    "LatexEncodingError",
    "auto_fix_text",
    "convert_single_dollar_math",
    "find_latex_encoding_issues",
    "find_mcq_option_issues",
    "sanitize_html",
]
