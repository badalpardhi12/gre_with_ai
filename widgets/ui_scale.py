"""
Centralized UI scaling and font helpers.

Scales fonts based on display size so the app looks proportional on
high-resolution screens (Retina, 2K, 4K) — wxPython does not auto-scale
font sizes by DPI on macOS by default.

The user-visible *zoom* multiplier (Cmd-+ / Cmd-- / Cmd-0 from the
View menu) layers on top of the display-DPI auto-scale. It's persisted
in ``llm_config.json`` via ``config.save_user_pref("font_size_multiplier", ...)``.
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
_USER_ZOOM = None


def _get_user_zoom():
    """User-controlled zoom factor (cached). Falls back to 1.0 if config
    is unavailable (e.g. during early import before DATA_DIR exists)."""
    global _USER_ZOOM
    if _USER_ZOOM is None:
        try:
            from config import load_user_prefs, clamp_font_multiplier
            _USER_ZOOM = clamp_font_multiplier(
                load_user_prefs().get("font_size_multiplier", 1.0)
            )
        except Exception:
            _USER_ZOOM = 1.0
    return _USER_ZOOM


def scale():
    """Cached scale factor (display-DPI auto-scale × user zoom)."""
    global _SCALE
    if _SCALE is None:
        _SCALE = get_scale_factor()
    return _SCALE * _get_user_zoom()


def invalidate_scale_cache():
    """Drop the cached display-DPI scale factor.

    Call from the main frame's `wx.EVT_DISPLAY_CHANGED` handler so moving
    the window between displays of different DPI rescales fonts on the next
    layout pass.
    """
    global _SCALE
    _SCALE = None


def invalidate_user_zoom_cache():
    """Drop the cached user-zoom multiplier so the next ``scale()`` call
    re-reads it from disk. Call after ``config.save_user_pref(
    "font_size_multiplier", v)``."""
    global _USER_ZOOM
    _USER_ZOOM = None


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


# ── Semantic typography tokens ──────────────────────────────────────
# Prefer these over the size-named helpers above; new screens should use
# `text_md` not `normal` so design intent reads from the call site.
def text_xs():       return font_size(9)
def text_sm():       return font_size(11)
def text_md():       return font_size(13)
def text_lg():       return font_size(16)
def text_xl():       return font_size(20)
def text_2xl():      return font_size(26)
def text_display():  return font_size(36)


# ── Spacing scale (4px base unit) ───────────────────────────────────
# `space(1) = 4px`, `space(2) = 8px`, `space(3) = 12px`, …
# Unifies the inline-padding constants scattered across screens.
def space(n: int) -> int:
    return font_size(4 * max(0, n))


def make_font(size, weight=wx.FONTWEIGHT_NORMAL,
              style=wx.FONTSTYLE_NORMAL,
              family=wx.FONTFAMILY_DEFAULT):
    """Build a wx.Font with the given size (pre-scaled if you pass through font_size())."""
    return wx.Font(size, family, style, weight)


def get_dashboard_html_font_pt():
    """Font size in points for the MathView HTML rendering (for prompts/lessons).

    Baseline 16pt at scale=1.0 — modestly larger than the previous 15pt
    default in response to user feedback that question text rendered too
    small. Layered with the user's font_size_multiplier (Cmd-+/-/0) and
    the display-DPI auto-scale via ``font_size()``.
    """
    return font_size(16)
