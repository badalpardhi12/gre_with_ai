"""
Math rendering widget using wx.html2.WebView and KaTeX (or MathJax fallback).
Displays formatted math expressions and rich HTML content.
"""
from pathlib import Path

import wx
import wx.html2

from config import RESOURCES_DIR, DATA_DIR
from widgets import ui_scale
from widgets.html_sanitizer import safe_html
from widgets.theme import Color, hex_str


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
<meta http-equiv="Content-Security-Policy"
      content="default-src 'self' data:;
               script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net;
               style-src  'self' 'unsafe-inline' https://cdn.jsdelivr.net;
               font-src   'self' data: https://cdn.jsdelivr.net;
               img-src    'self' data:;
               connect-src 'none';
               frame-src  'none';
               object-src 'none';">
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


class MathView(wx.Panel):
    """
    Renders HTML content with LaTeX math support via KaTeX.
    """

    def __init__(self, parent, size=(-1, -1)):
        super().__init__(parent, size=size)

        self.webview = wx.html2.WebView.New(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.webview, 1, wx.EXPAND)
        self.SetSizer(sizer)

        self._current_html = ""

    def set_content(self, html_body):
        """Set the HTML content (with optional LaTeX delimiters).

        `html_body` is treated as untrusted (it may originate from
        LLM-generated stimuli or imported ebook HTML) and is sanitized via
        bleach before being inlined into the page template.
        """
        sanitized = safe_html(html_body)
        self._current_html = sanitized
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
