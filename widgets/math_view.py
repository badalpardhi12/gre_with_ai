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
# already ship their own line-break markup (`<br>`, `<p>`) or block-level
# elements that manage their own internal whitespace (`<table>`, `<tr>`,
# `<td>`, `<th>`, `<li>`, `<ul>`, `<ol>`, `<h1>`–`<h6>`, `<blockquote>`)
# so we don't double-break author-formatted content. A bare `<div>`
# wrapper around plain-text-with-newlines IS still converted — `<div>`
# is a layout wrapper, not a line-break carrier, and skipping on it
# caused all 411 live QC prompts to collapse into a single visual row
# (GitHub #4, #5). The table-tag guard fixes the inverse bug: raw
# `<table>` stimuli with `\n` between `<tr>` and `<th>` had `<br>`
# injected into the table, which browsers foster-parent OUT of the
# table element, pushing the actual table ~1600px down and off the
# visible panel (GitHub #16/#17/#19/#21/#22, Q2283/Q2288/Q2293 + 5
# other Manhattan DI tables).
_PREFORMATTED_BREAK_RE = re.compile(
    r"<(?:br|p|table|tr|td|th|li|ul|ol|h[1-6]|blockquote)\b",
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


# Markdown pipe-tables inside question stimuli. Manhattan-5lb DI items
# occasionally ship a pipe-delimited table alongside the PNG chart
# (``<div>... | head1 | head2 |\n|---|---|\n| r1c1 | r1c2 |\n ...
# </div>``). The WebView has no markdown parser, so without this pass
# the user sees literal pipes and dashes next to the correctly-rendered
# image (GitHub #12, Q3610). The grammar targets the pattern that
# actually appears in the bank:
#
#   header row  : ``| a | b | c |``  — one or more cells between pipes
#   separator   : ``|---|---|---|``  — dashes, optional alignment colons
#   data rows   : repeats of the header shape
#
# Matched blocks are swapped for a styled ``<table>`` that blends with
# the HTML-table stimuli (Q2283, Q2288, etc.) so the two sources look
# identical on screen. Runs BEFORE ``_normalise_plain_math`` so the
# math-block detector doesn't mis-segment on table pipes.
_MD_TABLE_BLOCK_RE = re.compile(
    r"""
    (?:^[ \t]*\|.*\|[ \t]*\n)      # header row
    [ \t]*\|(?:[ \t]*:?-{2,}:?[ \t]*\|)+[ \t]*\n   # separator row
    (?:[ \t]*\|.*\|[ \t]*\n?)+     # >=1 data rows
    """,
    re.MULTILINE | re.VERBOSE,
)


def _split_md_table_row(row: str) -> "list[str]":
    """Split a ``| a | b | c |`` row into its cell text list."""
    stripped = row.strip().strip("|")
    return [c.strip() for c in stripped.split("|")]


def _parse_md_alignment(sep_row: str) -> "list[str]":
    """Return per-column CSS ``text-align`` hints from a ``|:---:|---:|``
    separator row. Empty/missing colons default to ``left``."""
    aligns = []
    for cell in _split_md_table_row(sep_row):
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("right")
        else:
            aligns.append("left")
    return aligns


def _render_md_table(match: "re.Match[str]") -> str:
    block = match.group(0)
    lines = [ln for ln in block.split("\n") if ln.strip()]
    if len(lines) < 2:
        return block
    header = _split_md_table_row(lines[0])
    aligns = _parse_md_alignment(lines[1])
    # Pad alignments to header width so a malformed separator doesn't
    # drop cells.
    if len(aligns) < len(header):
        aligns += ["left"] * (len(header) - len(aligns))
    data_rows = [_split_md_table_row(ln) for ln in lines[2:]]

    def _cell(tag, text, align):
        return (f'<{tag} style="padding:6px 12px; border:1px solid #444; '
                f'text-align:{align}; color:#e8e8e8;">{text}</{tag}>')

    def _row(cells, tag):
        return "<tr>" + "".join(
            _cell(tag, c, aligns[i] if i < len(aligns) else "left")
            for i, c in enumerate(cells)
        ) + "</tr>"

    out = ['<table style="margin:12px auto; border-collapse:collapse;">']
    out.append("<thead>" + _row(header, "th") + "</thead>")
    if data_rows:
        out.append("<tbody>" + "".join(_row(r, "td") for r in data_rows) + "</tbody>")
    out.append("</table>")
    return "".join(out)


def _markdown_tables_to_html(text: str) -> str:
    """Rewrite markdown pipe-tables inside *text* to styled HTML tables.

    No-op if *text* is empty or contains no pipe-table separator row —
    the trigger condition lets the regex skip most inputs cheaply."""
    if not text or "|---" not in text:
        return text
    return _MD_TABLE_BLOCK_RE.sub(_render_md_table, text)


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
    font-family: {body_font};
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
    font-weight: {prompt_weight};
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
/* Data-presentation figures shown in the left stimulus pane should FILL the
 * pane width (a small-intrinsic chart PNG would otherwise render tiny). The
 * pane wraps such content in `.datafig`. */
.datafig img {{
    width: 100%;
    max-width: 640px;
    max-height: 80vh;
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
/* Quantitative Comparison layout. Inline ``style`` attributes are stripped
 * by the HTML sanitizer (bleach empties them without a css_sanitizer), so the
 * QC figure / common-info / two-quantity columns are driven by these classes
 * instead — the only reliable way to make the table span the full content
 * width and center each quantity under its underlined header. */
.qc-fig {{
    text-align: center;
    margin: 4px auto 10px auto;
}}
.qc-common {{
    text-align: center;
    margin: 0 auto 8px auto;
}}
.qc-table {{
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    margin: 16px 0 6px 0;
}}
.qc-table td, .qc-table th {{
    width: 50%;
    border: none;
    text-align: center;
    vertical-align: middle;
    padding: 8px 12px;
}}
.qc-head {{
    font-weight: 600;
    padding-bottom: 10px;
}}
.qc-quantity {{
    font-size: 1.08em;
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

    def __init__(self, parent, size=(-1, -1), exam=False):
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
        # ``exam`` selects the ETS GRE light/serif content theme (white bg,
        # black serif body, navy accents) used by the in-test question screen.
        # Default False keeps the dark study-app theme for dashboard screens.
        self._exam = exam

        self.webview = wx.html2.WebView.New(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.webview, 1, wx.EXPAND)
        self.SetSizer(sizer)

        self._current_html = ""
        # Auto-height state — `set_content_auto_height` sets these, the
        # LOADED handler reads them on the next page-ready event, then
        # clears `_auto_height_active` so subsequent non-auto callers
        # aren't affected.
        self._auto_height_active = False
        self._auto_height_min = 0
        self._auto_height_max = 0
        self.webview.Bind(wx.html2.EVT_WEBVIEW_LOADED, self._on_webview_loaded)
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
        normalised = _markdown_tables_to_html(html_body or "")
        normalised = _normalise_plain_math(normalised)
        normalised = _newlines_to_html(normalised)
        sanitized = safe_html(normalised)
        self._current_html = sanitized
        Color = self._Color
        hex_str = self._hex_str
        ui_scale = self._ui_scale
        if getattr(self, "_exam", False):
            # ETS exam theme: white content, black SERIF body, navy accents.
            from widgets.theme import ExamColor, EXAM_SERIF_CSS
            full_html = HTML_TEMPLATE.format(
                katex_css=KATEX_CSS,
                katex_js=KATEX_JS,
                katex_auto=KATEX_AUTO,
                content=sanitized,
                font_size=ui_scale.get_dashboard_html_font_pt(),
                body_font=EXAM_SERIF_CSS,
                prompt_weight="400",
                bg_page=hex_str(ExamColor.CONTENT_BG),
                bg_surface=hex_str(ExamColor.CONTENT_BG_ALT),
                text_primary=hex_str(ExamColor.TEXT),
                text_secondary=hex_str(ExamColor.TEXT),
                border=hex_str(ExamColor.DIVIDER),
                accent=hex_str(ExamColor.HEADER_NAVY),
                success=hex_str(ExamColor.HEADER_NAVY),
                success_bg="#eef3fb",
                warning_bg=hex_str(ExamColor.SELECT_IN_PASSAGE_HL),
                warning_text="#000000",
            )
            self.webview.SetPage(full_html, PROJECT_BASE_URL)
            return
        # Pull all colors from the central palette so the WebView matches
        # the native widgets without per-screen overrides.
        full_html = HTML_TEMPLATE.format(
            katex_css=KATEX_CSS,
            katex_js=KATEX_JS,
            katex_auto=KATEX_AUTO,
            content=sanitized,
            font_size=ui_scale.get_dashboard_html_font_pt(),
            body_font='-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
            prompt_weight="500",
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

    def set_content_auto_height(self, html_body, min_h=80, max_h=400):
        """Render *html_body* and resize the panel's min-height to match
        the actual content height after the page loads.

        Solves GitHub #10, #11 where a fixed-height prompt view (220px)
        left ~150px of dead whitespace below single-line DI prompts,
        which users read as a "gap between the question and the table".

        * ``min_h``: the floor — the panel never shrinks below this even
          for one-line prompts, so the WebView has room for its own
          body padding.
        * ``max_h``: the ceiling — longer content scrolls inside the
          WebView rather than pushing the answer panel off-screen.
        """
        self._auto_height_active = True
        self._auto_height_min = int(min_h)
        self._auto_height_max = int(max_h)
        self.set_content(html_body)

    def _on_webview_loaded(self, event):
        event.Skip()
        if not self._auto_height_active:
            return
        # One-shot: clear the flag before measuring so a second LOADED
        # event (e.g. if the user re-focuses the page) doesn't re-trigger
        # with stale bounds.
        self._auto_height_active = False
        try:
            ok, out = self.webview.RunScript(
                "(function(){"
                "  var b = document.body;"
                "  var d = document.documentElement;"
                "  return String(Math.max("
                "    b ? b.scrollHeight : 0,"
                "    d ? d.scrollHeight : 0,"
                "    b ? b.offsetHeight : 0,"
                "    d ? d.offsetHeight : 0));"
                "})();"
            )
            measured = int(out) if ok and out and out.isdigit() else 0
        except Exception:
            measured = 0
        if measured <= 0:
            return
        clamped = max(self._auto_height_min, min(measured, self._auto_height_max))
        # Add the body padding that KaTeX/CSS applies (14px top+bottom)
        # so we don't clip the last line — `scrollHeight` includes
        # padding but some WebKit versions return the viewport height
        # until the scrollbar materialises.
        self.SetMinSize((-1, clamped))
        parent = self.GetParent()
        if parent is not None:
            parent.Layout()

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
