"""
HTML sanitizer for content rendered into a JS-enabled wx.html2.WebView.

LLM-generated stimulus, prompt, explanation, and lesson HTML is fundamentally
untrusted text; without sanitization, a single malicious or accidental
`<script>` or `<img onerror=...>` payload would execute inside the WebView
with file://-base privileges, allowing exfiltration of `data/llm_config.json`
and other sensitive resources.

`safe_html(raw)` runs `bleach.clean` with a math/explanation-friendly
allow-list. KaTeX delimiters (`$$ $$`, `\\( \\)`, `\\[ \\]`) survive untouched
because bleach only inspects tag/attribute structure — the dollar signs are
just text from its perspective.

Note on `style` attribute: we allow it (DI charts, lesson tables, KaTeX hints
all set inline styles). bleach without a `css_sanitizer` will keep raw style
values; the worst case is cosmetic mischief, not script execution.
"""
import warnings
from typing import Iterable

import bleach


# Suppress the "style attribute set without css_sanitizer" informational
# warning; the trade-off is documented above.
warnings.filterwarnings(
    "ignore",
    message=".*css_sanitizer.*",
    category=getattr(bleach, "NoCssSanitizerWarning", Warning),
    module=r"bleach\.sanitizer",
)


# Tags safe for question/lesson rendering. KaTeX runs over body text after
# bleach is done, so the rendered output is unaffected.
ALLOWED_TAGS = frozenset({
    "a", "abbr", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre", "small", "span", "strong",
    "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
})

ALLOWED_ATTRS = {
    "*": ["class", "style"],
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "style", "width", "height"],
    "table": ["style", "border", "cellspacing", "cellpadding"],
    "td": ["style", "colspan", "rowspan", "align"],
    "th": ["style", "colspan", "rowspan", "align", "scope"],
}

# Drop file://, javascript:, http: — keep https: + data: (for inline DI charts).
ALLOWED_PROTOCOLS = frozenset({"https", "data", "mailto"})


def safe_html(raw: str) -> str:
    """Return `raw` with disallowed tags/attrs/protocols stripped.

    A non-string input (None, int, etc.) yields an empty string rather than
    raising — keeps callers simple.
    """
    if not raw:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    return bleach.clean(
        raw,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )


__all__ = ["safe_html", "ALLOWED_TAGS", "ALLOWED_ATTRS", "ALLOWED_PROTOCOLS"]
