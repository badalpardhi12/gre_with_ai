"""
Math rendering widget using wx.html2.WebView and KaTeX (or MathJax fallback).
Displays formatted math expressions and rich HTML content.
"""
from pathlib import Path

import wx
import wx.html2

from config import RESOURCES_DIR
from widgets import ui_scale


# Base URL for the WebView so file:// images (e.g. DI charts in data/images/)
# resolve when SetPage loads inline HTML.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_BASE_URL = PROJECT_ROOT.as_uri() + "/"


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
{katex_css}
{katex_js}
{katex_auto}
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: {font_size}px;
    line-height: 1.6;
    color: #e8e8e8;
    padding: 14px 18px;
    margin: 0;
    background: #1e1e1e;
}}
.passage {{
    border-left: 3px solid #4FC3F7;
    padding-left: 16px;
    margin-bottom: 16px;
    color: #c0c0c0;
}}
.prompt {{
    font-weight: 500;
    color: #ffffff;
    margin-bottom: 12px;
}}
.highlight {{
    background-color: #4a3f1c;
    color: #ffeaa7;
    padding: 2px 4px;
    border-radius: 3px;
}}
table {{
    border-collapse: collapse;
    margin: 12px 0;
    color: #e8e8e8;
}}
th, td {{
    border: 1px solid #444;
    padding: 6px 12px;
    text-align: center;
}}
th {{
    background: #2a2a2a;
    color: #ffffff;
}}
.answer-correct {{
    background: #1b3a1b;
    border-left: 3px solid #4caf50;
    padding: 10px 14px;
    margin: 8px 0 12px 0;
    border-radius: 3px;
    color: #c8e6c9;
    font-size: 16px;
}}
.answer-correct strong {{
    color: #81c784;
}}
.explanation {{
    background: #252525;
    border-left: 3px solid #4FC3F7;
    padding: 10px 14px;
    margin: 8px 0;
    border-radius: 3px;
    color: #d4d4d4;
}}
.explanation h3 {{
    margin: 0 0 8px 0;
    font-size: 14px;
    color: #4FC3F7;
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
        """Set the HTML content (with optional LaTeX delimiters)."""
        self._current_html = html_body
        full_html = HTML_TEMPLATE.format(
            katex_css=KATEX_CSS,
            katex_js=KATEX_JS,
            katex_auto=KATEX_AUTO,
            content=html_body,
            font_size=ui_scale.get_dashboard_html_font_pt(),
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
