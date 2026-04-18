"""
Centralized design tokens for the GRE prep app.

A single dark palette — every screen and widget reads colors from here so the
app feels coherent and a future palette swap touches one file. The audit
flagged half the screens as light-themed and half as dark-themed; this module
is the contract that ends that drift.

Usage:
    from widgets.theme import Color, mastery_color
    panel.SetBackgroundColour(Color.BG_SURFACE)
    label.SetForegroundColour(Color.TEXT_PRIMARY)
"""
import wx


class Color:
    """Named color tokens. Derive everything visual from these."""

    # ── Surfaces ──────────────────────────────────────────────────────
    BG_PAGE       = wx.Colour(0x1e, 0x1e, 0x1e)   # frame background
    BG_SURFACE    = wx.Colour(0x2a, 0x2a, 0x2a)   # cards, sidebar
    BG_ELEVATED   = wx.Colour(0x35, 0x35, 0x35)   # selected nav item, hover surfaces
    BG_HOVER      = wx.Colour(0x3f, 0x3f, 0x3f)   # button hover
    BG_INPUT      = wx.Colour(0x1a, 0x1a, 0x1a)   # text input background
    BORDER        = wx.Colour(0x44, 0x44, 0x44)   # subtle dividers
    BORDER_STRONG = wx.Colour(0xaa, 0xaa, 0xaa)   # focus rings, prominent borders

    # ── Text ──────────────────────────────────────────────────────────
    TEXT_PRIMARY    = wx.Colour(0xff, 0xff, 0xff)
    TEXT_SECONDARY  = wx.Colour(0xb0, 0xb0, 0xb0)
    TEXT_TERTIARY   = wx.Colour(0x70, 0x70, 0x70)
    TEXT_INVERSE    = wx.Colour(0x1e, 0x1e, 0x1e)   # text on accent surfaces

    # ── Accents (reserve for meaningful state, not decoration) ────────
    ACCENT          = wx.Colour(0x4f, 0xc3, 0xf7)   # info blue (matches existing)
    ACCENT_DARK     = wx.Colour(0x29, 0x99, 0xd1)
    SUCCESS         = wx.Colour(0x66, 0xbb, 0x6a)   # mastery, correct
    WARNING         = wx.Colour(0xff, 0xa7, 0x26)   # weak, marked
    DANGER          = wx.Colour(0xef, 0x53, 0x50)   # incorrect, abandoned
    STREAK          = wx.Colour(0xff, 0x70, 0x43)   # streak fire

    # ── Mastery heatmap bands (5-stop gradient) ───────────────────────
    MASTERY = [
        wx.Colour(0x33, 0x33, 0x33),   # 0 — never attempted
        wx.Colour(0x6b, 0x3a, 0x3a),   # <0.4 — weak
        wx.Colour(0x6b, 0x5a, 0x3a),   # 0.4–0.6 — improving
        wx.Colour(0x4a, 0x6b, 0x4f),   # 0.6–0.8 — strong
        wx.Colour(0x66, 0xbb, 0x6a),   # >=0.8 — mastered
    ]


def mastery_color(score: float, attempts: int) -> wx.Colour:
    """Heatmap cell color for a mastery score in [0, 1]."""
    if attempts == 0:
        return Color.MASTERY[0]
    if score < 0.4:
        return Color.MASTERY[1]
    if score < 0.6:
        return Color.MASTERY[2]
    if score < 0.8:
        return Color.MASTERY[3]
    return Color.MASTERY[4]


def hex_str(c: wx.Colour) -> str:
    """Format a wx.Colour as `#rrggbb` for use inside HTML/CSS strings.

    Used by `widgets/math_view.py` so the WebView template stays in lock-step
    with the native widget palette.
    """
    return f"#{c.Red():02x}{c.Green():02x}{c.Blue():02x}"


__all__ = ["Color", "mastery_color", "hex_str"]
