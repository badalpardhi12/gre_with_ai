"""
Math rendering widget using wx.html2.WebView and KaTeX (or MathJax fallback).
Displays formatted math expressions and rich HTML content.

The two pure-Python helpers at the top of this module
(`_normalise_plain_math`, `_newlines_to_html`) are exercised by the
headless test suite on CI, which installs every requirement EXCEPT
wxPython. We therefore guard the wx import chain so the module stays
collectable without wx; the `MathView` class is only defined when wx
is available (it is a wx.Panel subclass — defining the class needs
`wx.Panel` at class-creation time). Out-of-app callers never need the
class, so the guard is a pure no-op for runtime users.
"""
import re
from pathlib import Path

try:  # pragma: no cover — the False branch only runs on headless CI.
    import wx
    import wx.html2
    _WX_AVAILABLE = True
except ModuleNotFoundError:
    wx = None  # type: ignore[assignment]
    _WX_AVAILABLE = False

from config import RESOURCES_DIR, DATA_DIR

# html_sanitizer is pure-Python (bleach only); safe to import
# unconditionally. ui_scale and theme both import wx at module top,
# so they are imported lazily inside the wx-dependent class below.
from widgets.html_sanitizer import safe_html


# Plain-ASCII math notation that imported / older-LLM-generated content
# uses instead of LaTeX (e.g. "sqrt(3)", "x^2", "pi/2"). At render time
# we rewrite each occurrence into a KaTeX-recognised inline span so the
# question matches what a real GRE prep book would show. The substitutions
# are deliberately conservative — they only fire when the text is clearly
# mathematical (a number / variable next to the operator), so prose like
# "the carrot pi" or "his ^th birthday" stays untouched.
_PLAIN_MATH_NORMALISERS = (
    # sqrt(<expr>) → \(\sqrt{<expr>}\)
    (re.compile(r"\bsqrt\s*\(([^()]+)\)"), r"\\(\\sqrt{\1}\\)"),
    # 3^2 / x^n → \(3^{2}\) / \(x^{n}\)  (single token, no space around ^)
    (re.compile(r"(?<![\\\w])([A-Za-z0-9]+)\^(\{?[A-Za-z0-9.+\-]+\}?)(?!\w)"),
     r"\\(\1^{\2}\\)"),
)

# Math blocks we must NOT touch when running the plain-math normalisers
# (otherwise `25^{x}` inside `\(\left(25^{x}\right)\)` gets re-wrapped to
# `\(25^{{x}}\)`, which KaTeX renders as raw text). Splits the input
# into alternating non-math / math segments and only normalises the
# non-math segments.
_MATH_BLOCK_RE = re.compile(
    r"(\\\(.*?\\\)|\\\[.*?\\\]|\$\$.*?\$\$)",
    re.DOTALL,
)


def _normalise_plain_math(html: str) -> str:
    """Best-effort rewrite of common ASCII math into KaTeX-friendly form.

    Skips content already inside `\\(...\\)`, `\\[...\\]`, or `$$...$$`
    so already-LaTeX expressions aren't double-wrapped. Also rewrites
    inline Markdown emphasis (`**bold**`, `*italic*`) into HTML so the
    WebView renders it (fix for GitHub issue #2).
    """
    if not html:
        return html
    parts = _MATH_BLOCK_RE.split(html)
    # Even-indexed parts are non-math (rewrite); odd-indexed are math (leave).
    for i in range(0, len(parts), 2):
        seg = parts[i]
        if not seg:
            continue
        for pattern, repl in _PLAIN_MATH_NORMALISERS:
            seg = pattern.sub(repl, seg)
        seg = _markdown_inline_to_html(seg)
        parts[i] = seg
    return "".join(parts)


# Convert plain-text linebreaks into HTML linebreaks. Question prompts
# stored in the bank use `\n` / `\n\n` separators (e.g. "Quantity A: …\n
# Quantity B: …"); without this conversion the browser collapses them
# into a single line and quantity labels run together. Skips inputs that
# already ship their own line-break markup (`<br>` or `<p>`) so we don't
# double-break author-formatted content. A bare `<div>` wrapper around
# plain-text-with-newlines IS still converted — `<div>` is a layout
# wrapper, not a line-break carrier, and skipping on it caused all 411
# live QC prompts to collapse into a single visual row (GitHub #4, #5).
_PREFORMATTED_BREAK_RE = re.compile(
    r"<(?:br|p)\b",
    re.IGNORECASE,
)


def _newlines_to_html(text: str) -> str:
    """Map plain-text newlines to HTML line breaks unless the input
    already ships its own line-break markup (``<br>`` or ``<p>``)."""
    if not text or _PREFORMATTED_BREAK_RE.search(text):
        return text
    # Two-or-more consecutive newlines = paragraph break (blank line).
    # A single newline = soft line break.
    return re.sub(r"\n{2,}", "<br><br>", text).replace("\n", "<br>")


# Lightweight Markdown → HTML for inline emphasis and ordered/unordered
# lists. Imported questions and synthetic explanations use Markdown
# (`**bold**`, `*italic*`, numbered steps, bullets) but the WebView only
# renders HTML + KaTeX. Without this conversion a user reported the
# literal `**bold_text**` appearing on screen (GitHub issue #2).
#
# We intentionally keep the grammar *tiny* so the regex path is safe
# against math delimiters — only patterns that cannot occur inside
# `\(...\)` / `\[...\]` / `$$...$$` are rewritten. Bold/italic live in
# the non-math prose segments split by `_MATH_BLOCK_RE` above.
_MD_BOLD_RE = re.compile(r"\*\*([^\s*][^*\n]*?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<![*\w])\*([^\s*][^*\n]*?)\*(?!\w)")
_MD_UNDERSCORE_BOLD_RE = re.compile(r"__([^\s_][^_\n]*?)__")


def _markdown_inline_to_html(text: str) -> str:
    """Rewrite Markdown bold/italic into HTML tags.

    Runs per non-math segment produced by `_MATH_BLOCK_RE`. Order matters:
    `**bold**` before `*italic*` so the inner asterisks of bold aren't
    misread as italic markers.
    """
    if not text or "*" not in text and "_" not in text:
        return text
    text = _MD_BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _MD_UNDERSCORE_BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _MD_ITALIC_RE.sub(r"<em>\1</em>", text)
    return text


# Base URL for the WebView. Restricted to data/images/ so a malicious
# stimulus cannot use file:// to traverse upward into data/llm_config.json
# or other in-tree files.
IMAGES_DIR = DATA_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_BASE_URL = IMAGES_DIR.as_uri() + "/"


# Minimal KaTeX CSS/JS served locally. If KaTeX files are not bundled,
# fall back to CDN (requires internet).
KATEX_DIR = RESOURCES_DIR / "katex"

# Check if local KaTeX is available
if (KATEX_DIR / "katex.min.js").exists():
    KATEX_BASE = KATEX_DIR.as_uri() if hasattr(KATEX_DIR, 'as_uri') else f"file://{KATEX_DIR}"
    KATEX_CSS = f'<link rel="stylesheet" href="{KATEX_BASE}/katex.min.css">'
    KATEX_JS = f'<script src="{KATEX_BASE}/katex.min.js"></script>'
    KATEX_AUTO = f'<script src="{KATEX_BASE}/contrib/auto-render.min.js"></script>'
else:
    # CDN fallback
    KATEX_CSS = '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.css">'
    KATEX_JS = '<script src="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.js"></script>'
    KATEX_AUTO = '<script src="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/contrib/auto-render.min.js"></script>'


HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; font-src 'self' data: https://cdn.jsdelivr.net; img-src 'self' data: blob: file:; connect-src 'none'; frame-src 'none'; object-src 'none';">
{katex_css}
{katex_js}
{katex_auto}
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: {font_size}px;
    line-height: 1.6;
    color: {text_primary};
    padding: 14px 18px;
    margin: 0;
    background: {bg_page};
}}
.passage {{
    border-left: 3px solid {accent};
    padding-left: 16px;
    margin-bottom: 16px;
    color: {text_secondary};
}}
.prompt {{
    font-weight: 500;
    color: {text_primary};
    margin-bottom: 12px;
}}
.highlight {{
    background-color: {warning_bg};
    color: {warning_text};
    padding: 2px 4px;
    border-radius: 3px;
}}
table {{
    border-collapse: collapse;
    margin: 12px 0;
    color: {text_primary};
}}
th, td {{
    border: 1px solid {border};
    padding: 6px 12px;
    text-align: center;
}}
th {{
    background: {bg_surface};
    color: {text_primary};
}}
/* DI plots, geometry figures, and any embedded image must shrink to
 * fit the panel. Without this rule a wide chart pushes its container
 * past the splitter and the user has to drag the sash to see the
 * options. `display: block` + `margin: auto` centers the image; the
 * `max-height` prevents tall figures from dominating a short window. */
img {{
    max-width: 100%;
    height: auto;
    max-height: 60vh;
    display: block;
    margin: 8px auto;
}}
.answer-correct {{
    background: {success_bg};
    border-left: 3px solid {success};
    padding: 10px 14px;
    margin: 8px 0 12px 0;
    border-radius: 3px;
    color: {text_primary};
    font-size: 16px;
}}
.answer-correct strong {{
    color: {success};
}}
.explanation {{
    background: {bg_surface};
    border-left: 3px solid {accent};
    padding: 10px 14px;
    margin: 8px 0;
    border-radius: 3px;
    color: {text_primary};
}}
.explanation h3 {{
    margin: 0 0 8px 0;
    font-size: 14px;
    color: {accent};
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}
.explanation p {{
    margin: 6px 0;
}}
/* KaTeX should inherit color */
.katex {{
    color: inherit;
}}
</style>
</head>
<body>
{content}
<script>
document.addEventListener("DOMContentLoaded", function() {{
    if (typeof renderMathInElement !== 'undefined') {{
        renderMathInElement(document.body, {{
            delimiters: [
                {{left: "$$", right: "$$", display: true}},
                {{left: "\\\\(", right: "\\\\)", display: false}},
                {{left: "\\\\[", right: "\\\\]", display: true}}
            ],
            throwOnError: false
        }});
    }}
}});
</script>
</body>
</html>"""


class MathView(wx.Panel if _WX_AVAILABLE else object):
    """
    Renders HTML content with LaTeX math support via KaTeX.

    Requires wxPython. The class still exists on headless CI (so
    pytest collection and `from widgets.math_view import MathView`
    both succeed), but instantiating it without wx raises a clear
    error instead of the cryptic `object() takes no arguments` from
    `super().__init__(parent, size=size)`.
    """

    def __init__(self, parent, size=(-1, -1)):
        if not _WX_AVAILABLE:
            raise RuntimeError(
                "MathView requires wxPython. Install the wxPython "
                "requirement from requirements.txt to use the GUI."
            )
        # Lazy-import wx-dependent widget helpers — they all import
        # `wx` at module top, so importing them on headless CI would
        # blow up. Kept here so the class body stays flat.
        from widgets import ui_scale
        from widgets.theme import Color, hex_str

        super().__init__(parent, size=size)

        self.webview = wx.html2.WebView.New(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.webview, 1, wx.EXPAND)
        self.SetSizer(sizer)

        self._current_html = ""
        # Stash the rendering helpers so `set_content` doesn't need
        # to re-import them on every call.
        self._ui_scale = ui_scale
        self._Color = Color
        self._hex_str = hex_str

    def set_content(self, html_body):
        """Set the HTML content (with optional LaTeX delimiters).

        `html_body` is treated as untrusted (it may originate from
        LLM-generated stimuli or imported ebook HTML) and is sanitized via
        bleach before being inlined into the page template.

        Plain ASCII math (e.g. "sqrt(3)", "x^2") is rewritten into KaTeX
        delimiters before sanitisation so older imported questions
        render correctly even if the source forgot the math markup.

        Plain-text linebreaks are converted to HTML `<br>` so quantity
        labels stored as "Quantity A: …\\nQuantity B: …" render on
        separate lines instead of collapsing to a single visual row.
        """
        normalised = _normalise_plain_math(html_body or "")
        normalised = _newlines_to_html(normalised)
        sanitized = safe_html(normalised)
        self._current_html = sanitized
        Color = self._Color
        hex_str = self._hex_str
        ui_scale = self._ui_scale
        # Pull all colors from the central palette so the WebView matches
        # the native widgets without per-screen overrides.
        full_html = HTML_TEMPLATE.format(
            katex_css=KATEX_CSS,
            katex_js=KATEX_JS,
            katex_auto=KATEX_AUTO,
            content=sanitized,
            font_size=ui_scale.get_dashboard_html_font_pt(),
            bg_page=hex_str(Color.BG_PAGE),
            bg_surface=hex_str(Color.BG_SURFACE),
            text_primary=hex_str(Color.TEXT_PRIMARY),
            text_secondary=hex_str(Color.TEXT_SECONDARY),
            border=hex_str(Color.BORDER),
            accent=hex_str(Color.ACCENT),
            success=hex_str(Color.SUCCESS),
            success_bg="#1b3a1b",
            warning_bg="#4a3f1c",
            warning_text="#ffeaa7",
        )
        self.webview.SetPage(full_html, PROJECT_BASE_URL)

    def set_passage(self, passage_html):
        """Display a reading comprehension passage."""
        self.set_content(f'<div class="passage">{passage_html}</div>')

    def set_prompt(self, prompt_html):
        """Display a question prompt."""
        self.set_content(f'<div class="prompt">{prompt_html}</div>')

    def set_passage_and_prompt(self, passage_html, prompt_html):
        """Display passage and prompt together."""
        content = f'<div class="passage">{passage_html}</div>'
        content += f'<div class="prompt">{prompt_html}</div>'
        self.set_content(content)

    def clear(self):
        self.set_content("")
