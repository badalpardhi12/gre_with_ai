"""
Figure-caption cleanup helpers.

Vision-generated alt-text ("A cross-shaped figure composed of 5 equal squares…")
was concatenated into Stimulus.content as visible italic gray caption HTML.
That text belongs to screen-reader / fallback alt metadata, not the passage
body — rendering it inline turns screen-reader hints into visible prose.

These helpers:

  * classify a caption string as alt-text-like vs. a legitimate unit /
    axis-label caption (e.g. "Sales in thousands of dollars") that must
    stay visible.
  * rewrite a stimulus HTML blob to wrap alt-text-like captions inside
    HTML comments (`<!--alt-text-->…<!--/alt-text-->`). `widgets.html_sanitizer.safe_html`
    passes `strip_comments=True` to bleach so the wrapped block is erased
    from the rendered WebView, but the string survives in the DB for
    eventual reuse as an `<img alt="…">` attribute or screen-reader hint.

Keeping the rewrite reversible (no destructive DELETE of text) is
deliberate — if the heuristic misclassifies a legit caption we can
re-audit by un-wrapping the comments later.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# The caption HTML produced by build-time generators:
#   <p style="text-align:center; font-style:italic; color:#a0a0a0; margin-top:6px;">…</p>
# (Spacing / trailing semicolon differs slightly between producers; the
# regex tolerates whitespace and accepts extra style properties.)
_CAPTION_RE = re.compile(
    r'<p\s+style="[^"]*text-align\s*:\s*center[^"]*font-style\s*:\s*italic[^"]*color\s*:\s*#a0a0a0[^"]*">'
    r'(?P<body>[^<]*)'
    r'</p>',
    re.IGNORECASE,
)

# Patterns that identify a caption as a vision-generated alt-text description
# (a shape / figure / graph / chart / illustration that the image shows).
# Unit labels such as "Sales in thousands of dollars" do NOT match any of
# these — they carry semantic data the solver needs.
_ALT_TEXT_PATTERNS: Tuple[re.Pattern, ...] = (
    # "A triangle with…", "An L-shaped figure…", "A circle with center O…"
    re.compile(r'^\s*an?\s+\S+[- ]?(?:shaped\s+)?(?:figure|diagram|graph|chart|'
               r'illustration|plot|image|picture|bar\s+chart|pie\s+chart|'
               r'scatter(?:\s+plot)?|line\s+graph|histogram|table)\b',
               re.IGNORECASE),
    # "A circle with…", "A triangle with…", "A square with…"
    re.compile(r'^\s*an?\s+(?:circle|triangle|square|rectangle|polygon|hexagon|'
               r'pentagon|cross|cube|cylinder|cone|sphere|line\s+segment|'
               r'coordinate\s+plane|number\s+line|grid|cartesian)\b',
               re.IGNORECASE),
    # "The figure shows…", "The graph depicts…"
    re.compile(r'^\s*the\s+(?:figure|graph|chart|diagram|illustration|plot|image)\s+'
               r'(?:shows|depicts|illustrates|displays|represents|contains)\b',
               re.IGNORECASE),
    # "Shown above is a…", "Pictured: a triangle…"
    re.compile(r'^\s*(?:shown|pictured|depicted|illustrated)\b', re.IGNORECASE),
    # Stacked-chart descriptions: "A stacked bar chart titled…"
    re.compile(r'^\s*an?\s+(?:stacked|grouped|horizontal|vertical|clustered)\s+'
               r'(?:bar\s+chart|chart|graph|plot)\b',
               re.IGNORECASE),
)


def is_alt_text_caption(caption_body: str) -> bool:
    """Return True if `caption_body` looks like vision-generated alt-text.

    False for legitimate unit / axis captions (e.g. "Sales in thousands of
    dollars", "Temperatures in °F", "Total monthly budget is $4,800").
    """
    if not caption_body:
        return False
    text = caption_body.strip()
    # Short labels like "Sales in millions" are inherently not alt-text
    # (vision alt-text is always a descriptive sentence).
    if len(text) < 20:
        return False
    return any(p.search(text) for p in _ALT_TEXT_PATTERNS)


def strip_alt_text_captions(html: str) -> Tuple[str, List[str]]:
    """Wrap alt-text-like captions inside HTML comments.

    Returns (rewritten_html, stripped_alt_texts). `stripped_alt_texts` is
    the list of caption bodies that were recognised as alt-text — useful
    for reporting / migration audit.

    Non-alt-text captions (unit labels etc.) are returned untouched.
    """
    if not html:
        return html, []

    stripped: List[str] = []

    def _repl(match: re.Match) -> str:
        body = match.group("body")
        if is_alt_text_caption(body):
            stripped.append(body.strip())
            # Wrap the whole <p>…</p> inside a comment so bleach drops it
            # at render time but the text is still recoverable from the DB.
            return f"<!--alt-text:{body.strip()}-->"
        return match.group(0)

    rewritten = _CAPTION_RE.sub(_repl, html)
    return rewritten, stripped


__all__ = [
    "is_alt_text_caption",
    "strip_alt_text_captions",
]
