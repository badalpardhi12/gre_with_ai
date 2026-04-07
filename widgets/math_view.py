"""
Math rendering widget using wx.html2.WebView and KaTeX (or MathJax fallback).
Displays formatted math expressions and rich HTML content.
"""
import wx
import wx.html2

from config import RESOURCES_DIR


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
    font-size: 15px;
    line-height: 1.6;
    color: #333;
    padding: 12px 16px;
    margin: 0;
    background: #fff;
}}
.passage {{
    border-left: 3px solid #2196F3;
    padding-left: 16px;
    margin-bottom: 16px;
    color: #555;
}}
.prompt {{
    font-weight: 500;
    margin-bottom: 12px;
}}
.highlight {{
    background-color: #fff3cd;
    padding: 2px 4px;
    border-radius: 3px;
}}
table {{
    border-collapse: collapse;
    margin: 12px 0;
}}
th, td {{
    border: 1px solid #ccc;
    padding: 6px 12px;
    text-align: center;
}}
th {{
    background: #f0f0f0;
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
                {{left: "$", right: "$", display: false}},
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
        )
        self.webview.SetPage(full_html, "")

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
