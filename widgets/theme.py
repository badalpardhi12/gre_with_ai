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


def _c(hexstr):
    """`#rrggbb` -> wx.Colour."""
    h = hexstr.lstrip("#")
    return wx.Colour(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


class ExamColor:
    """ETS GRE 'exam mode' light palette (docs/gre_ui_spec_2026_06.md §1).

    A faithful replica of the official ETS GRE test-taking UI: a navy
    header/footer 'sandwich' around a white, serif content area. Distinct from
    the dark study-app ``Color`` palette. Hexes tagged [A]=approx-from-official-
    screenshot, [I]=inferred design language; structure/behavior is confirmed.
    """

    # ── Chrome surfaces ───────────────────────────────────────────────
    HEADER_NAVY        = _c("#16284d")   # [A] header + footer bars
    HEADER_NAVY_HOVER  = _c("#1f3463")   # [I]
    CONTENT_BG         = _c("#ffffff")   # [C] white content area
    CONTENT_BG_ALT     = _c("#f7f7f7")   # [I] passage pane / zebra
    DIRECTIONS_BAND    = _c("#e6e6e6")   # [A] full-width directions band
    DIRECTIONS_TEXT    = _c("#1a1a1a")   # [I]
    DIVIDER            = _c("#cccccc")   # [I] hairline rules

    # ── Text ──────────────────────────────────────────────────────────
    TEXT               = _c("#000000")   # [C] body text on white
    TEXT_MUTED         = _c("#444444")   # [I]
    TEXT_ON_NAVY       = _c("#ffffff")   # [A] glyphs on navy chrome

    # ── Buttons ───────────────────────────────────────────────────────
    BTN_NEXT_BLUE      = _c("#2d8cff")   # [A] primary Next
    BTN_NEXT_BLUE_HOVER= _c("#1f78e6")   # [I]
    BTN_GREY           = _c("#5a5a5a")   # [A] Mark / Back
    BTN_GREY_HOVER     = _c("#6e6e6e")   # [I]
    BTN_TEXT           = _c("#ffffff")   # [I]
    BTN_DISABLED       = _c("#9a9a9a")   # [I] disabled (e.g. Transfer Display off)
    SUBMIT_MAUVE       = _c("#9b8aa3")   # [A] Submit Section
    SUBMIT_MAUVE_HOVER = _c("#ab9bb3")   # [I]

    # ── Answer controls ───────────────────────────────────────────────
    OVAL_BORDER        = _c("#555555")   # [I] radio outline
    OVAL_FILL_SELECTED = _c("#16284d")   # [I] radio selected
    CHECK_BORDER       = _c("#555555")   # [I] checkbox outline
    CHECK_FILL_SELECTED= _c("#16284d")   # [I] checkbox checked
    TC_HIGHLIGHT       = _c("#cfe2ff")   # [I] Text-Completion selected choice
    SELECT_IN_PASSAGE_HL = _c("#fff3b0") # [I] select-in-passage sentence
    ROW_HOVER          = _c("#eef3fb")   # [I]
    ROW_SELECTED       = _c("#dde9fb")   # [I]

    # ── Navigator circle states (footer 1..N) ─────────────────────────
    NAV_CURRENT        = _c("#2d8cff")   # [I] current-question ring
    NAV_ANSWERED       = _c("#16284d")   # [I] answered = filled navy
    NAV_UNANSWERED_BORDER = _c("#aab4c8")# [I] open circle outline (on navy)
    NAV_MARKED_BADGE   = _c("#d98b00")   # [I] marked flag/badge (amber)

    # ── Timer warning (legible on navy footer) ────────────────────────
    TIMER_NORMAL       = _c("#ffffff")   # [I]
    TIMER_WARN         = _c("#ffd24d")   # [I] <= 5:00
    TIMER_CRITICAL     = _c("#ff6b6b")   # [I] <= 1:00

    # ── ETS "Test Preview Tool" scheme (2026-06 revision, from official
    #    POWERPREP/Test-Preview screenshots) ────────────────────────────
    # The real test UI is a CHARCOAL header with a maroon hairline and a
    # top-right tool ribbon, a light-PINK section bar carrying the timer, and a
    # black-bordered white content box floating on a GRAY page.
    PAGE_GRAY          = _c("#b9b9b9")   # [A] gray margin behind the content box
    HEADER_CHARCOAL    = _c("#3b393b")   # [A] top header bar
    HEADER_RULE_MAROON = _c("#6f2233")   # [A] thin maroon line under the header
    SECTION_BAR_PINK   = _c("#f3e2e6")   # [A] section/question + timer bar
    SECTION_BAR_TEXT   = _c("#2a2a2a")   # [A] text on the pink bar
    CONTENT_BORDER     = _c("#000000")   # [A] black frame around the content box

    # Tool-ribbon buttons (label above a small icon, raised bevel).
    TOOL_BTN_FACE      = _c("#dcdcdc")   # [A] gray tool button (Calc/Mark/Review/Help)
    TOOL_BTN_FACE_HOVER= _c("#e8e8e8")   # [I]
    TOOL_BTN_BEVEL_HI  = _c("#fcfcfc")   # [I] top/left bevel highlight
    TOOL_BTN_BEVEL_LO  = _c("#9a9a9a")   # [I] bottom/right bevel shadow
    TOOL_BTN_TEXT      = _c("#1a1a1a")   # [A] dark label/icon on gray
    TOOL_BTN_TEXT_ON_HEADER = _c("#ffffff")  # [A] the label sits above the button on charcoal
    EXIT_PLUM          = _c("#7c4d66")   # [A] Exit Section button (muted plum)
    EXIT_PLUM_HOVER    = _c("#8c5d76")   # [I]
    NAV_BLUE           = _c("#2f6ea5")   # [A] active Back/Next/Continue/Return
    NAV_BLUE_HOVER     = _c("#3a7eb8")   # [I]
    NAV_BLUE_DISABLED  = _c("#3d4a59")   # [A] disabled Back/Next (dark desaturated)
    NAV_BTN_TEXT       = _c("#ffffff")   # [A] white label on blue/plum

    # Content-area accents.
    DIRECTIONS_PILL    = _c("#cfcfcf")   # [A] gray directions pill (bottom-center)
    DIRECTIONS_PILL_TEXT = _c("#1a1a1a") # [A]
    PASSAGE_TITLE_BAR  = _c("#1f3fb0")   # [A] blue "Questions N..M are based on..." bar
    PASSAGE_TITLE_TEXT = _c("#ffffff")   # [A]
    TRANSITION_RULE    = _c("#9a9a9a")   # [I] hairline under transition-screen titles



# Web-safe serif stack for the WebView (item content). The face being serif is
# confirmed for the GRE; Georgia is the on-screen-legible default.
EXAM_SERIF_CSS = 'Georgia, "Times New Roman", Times, serif'
EXAM_SANS_CSS = '-apple-system, "Helvetica Neue", Arial, sans-serif'
# Preferred native serif face name (falls back to platform serif if absent).
EXAM_SERIF_FACE = "Georgia"


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


__all__ = ["Color", "ExamColor", "mastery_color", "hex_str",
           "EXAM_SERIF_CSS", "EXAM_SANS_CSS", "EXAM_SERIF_FACE"]
