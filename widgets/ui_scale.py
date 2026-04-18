"""
Centralized UI scaling and font helpers.

Scales fonts based on display size so the app looks proportional on
high-resolution screens (Retina, 2K, 4K) — wxPython does not auto-scale
font sizes by DPI on macOS by default.
"""
import wx


# Base font sizes — what we want at "standard" 100% display
BASE_TINY = 9
BASE_SMALL = 11
BASE_NORMAL = 13
BASE_LARGE = 16
BASE_XLARGE = 20
BASE_TITLE = 26


def get_scale_factor():
    """Return a font scaling multiplier based on the display.

    Heuristic:
    - Small displays (<1600 wide): 1.0
    - 2K displays (1600-2400 wide): 1.25
    - 4K displays (>2400 wide): 1.5
    Uses the primary display's logical resolution.
    """
    try:
        display = wx.Display(0)
        rect = display.GetGeometry()
        width = rect.GetWidth()
    except Exception:
        return 1.0

    if width >= 2400:
        return 1.5
    elif width >= 1600:
        return 1.25
    return 1.0


_SCALE = None


def scale():
    """Cached scale factor (computed once)."""
    global _SCALE
    if _SCALE is None:
        _SCALE = get_scale_factor()
    return _SCALE


def font_size(base):
    """Return the scaled font size for a given base size."""
    return max(8, int(round(base * scale())))


# Shorthand sizes
def tiny():
    return font_size(BASE_TINY)


def small():
    return font_size(BASE_SMALL)


def normal():
    return font_size(BASE_NORMAL)


def large():
    return font_size(BASE_LARGE)


def xlarge():
    return font_size(BASE_XLARGE)


def title():
    return font_size(BASE_TITLE)


def make_font(size, weight=wx.FONTWEIGHT_NORMAL,
              style=wx.FONTSTYLE_NORMAL,
              family=wx.FONTFAMILY_DEFAULT):
    """Build a wx.Font with the given size (pre-scaled if you pass through font_size())."""
    return wx.Font(size, family, style, weight)


def get_dashboard_html_font_pt():
    """Font size in points for the MathView HTML rendering (for prompts/lessons)."""
    return font_size(15)
