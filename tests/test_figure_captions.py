"""
Tests for the figure-caption alt-text cleanup.

Bug 1: vision-generated captions like "A cross-shaped figure composed of
5 equal squares…" were being rendered as visible italic-gray prose below
the image. They belong inside an HTML-comment wrapper so the sanitizer
strips them from display while keeping them recoverable in the DB.
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.figure_captions import (
    is_alt_text_caption,
    strip_alt_text_captions,
)
from widgets.html_sanitizer import safe_html


# ── Classifier ─────────────────────────────────────────────────────────

def test_classifier_flags_shape_descriptions():
    cases = [
        "A cross-shaped figure composed of 5 equal squares arranged in a plus sign pattern.",
        "A square with an inscribed circle; a small shaded region appears in the upper-left corner.",
        "A triangle with angles labeled x°, y°, and z°.",
        "A circle with center O and radius 5.",
        "The figure shows a triangle with three angles marked.",
        "The graph depicts revenue over the years 2000-2010.",
        "Shown above is the path of a particle.",
        "A stacked bar chart titled 'Gross Federal Debt as a percent of GDP'.",
    ]
    for c in cases:
        assert is_alt_text_caption(c), f"should be alt-text: {c!r}"


def test_classifier_spares_unit_labels():
    # Unit / axis labels — legitimate captions carrying data for the solver.
    cases = [
        "Sales in thousands of dollars",
        "Visitors in thousands",
        "Revenue in millions of dollars",
        "Total monthly budget is $4,800; values shown are percentages.",
        "Temperatures in °F",
        "amounts in thousands of dollars",
        "Frequency table of 50 students",
        "Values in hundreds",
    ]
    for c in cases:
        assert not is_alt_text_caption(c), f"should NOT be alt-text: {c!r}"


def test_classifier_ignores_short_labels():
    # Vision-generated descriptions are always >= a sentence; short labels
    # must never trip the detector even when they begin with "A".
    assert not is_alt_text_caption("A")
    assert not is_alt_text_caption("A chart")


# ── Rewriter ───────────────────────────────────────────────────────────

_CAPTION_HTML = (
    '<div>The figure above is composed of 5 squares of equal area.</div>'
    '<div style="text-align: center; padding: 8px;">'
    '<img src="data:image/png;base64,IMAGEDATA" style="max-width:100%;" />'
    '<p style="text-align:center; font-style:italic; color:#a0a0a0; margin-top:6px;">'
    'A cross-shaped figure composed of 5 equal squares arranged in a plus sign pattern.'
    '</p></div>'
)


def test_strip_alt_text_wraps_caption_in_comment():
    new, stripped = strip_alt_text_captions(_CAPTION_HTML)
    # The plain caption text should no longer appear as a <p> element.
    assert '<p style="text-align:center; font-style:italic; color:#a0a0a0' not in new
    # The raw alt-text text must survive as an HTML comment (for potential
    # re-use as an alt attribute or screen-reader hint).
    assert '<!--alt-text:A cross-shaped figure' in new
    # And the migration reports what it stripped.
    assert len(stripped) == 1
    assert stripped[0].startswith("A cross-shaped figure")


def test_strip_alt_text_preserves_unit_labels():
    html = (
        '<div><img src="data:image/png;base64,X" />'
        '<p style="text-align:center; font-style:italic; color:#a0a0a0; margin-top:6px;">'
        'Sales in thousands of dollars</p></div>'
    )
    new, stripped = strip_alt_text_captions(html)
    assert stripped == []
    assert new == html


def test_strip_alt_text_mixed_captions_on_same_stimulus():
    # One alt-text caption + one legitimate unit caption side-by-side.
    html = (
        '<div><img src="data:image/png;base64,X" />'
        '<p style="text-align:center; font-style:italic; color:#a0a0a0;">'
        'A triangle with sides labeled 3, 4, and 5.'
        '</p>'
        '<p style="text-align:center; font-style:italic; color:#a0a0a0;">'
        'Sales in millions of dollars'
        '</p></div>'
    )
    new, stripped = strip_alt_text_captions(html)
    assert len(stripped) == 1
    assert "A triangle with sides labeled" in stripped[0]
    assert "Sales in millions of dollars" in new


def test_strip_alt_text_noop_when_no_caption():
    html = '<p>Plain passage text with no styled caption.</p>'
    new, stripped = strip_alt_text_captions(html)
    assert new == html
    assert stripped == []


def test_strip_alt_text_idempotent():
    # Second pass should find nothing more to strip.
    once, _ = strip_alt_text_captions(_CAPTION_HTML)
    twice, stripped = strip_alt_text_captions(once)
    assert twice == once
    assert stripped == []


# ── End-to-end: rendered output has no caption prose ──────────────────

def test_rendered_output_omits_alt_text_comment():
    """The sanitizer that feeds the WebView must strip alt-text comments
    so users never see them as visible text — bleach's `strip_comments=True`
    does this for us."""
    rewritten, _ = strip_alt_text_captions(_CAPTION_HTML)
    rendered = safe_html(rewritten)
    # The image survives.
    assert '<img' in rendered
    # The alt-text string has been erased from what the WebView would show.
    assert "A cross-shaped figure" not in rendered
    # And the comment marker is gone too (bleach strips it).
    assert "<!--alt-text" not in rendered


def test_rendered_output_keeps_unit_caption():
    html = (
        '<div><img src="data:image/png;base64,X" />'
        '<p style="text-align:center; font-style:italic; color:#a0a0a0;">'
        'Sales in millions of dollars</p></div>'
    )
    rewritten, _ = strip_alt_text_captions(html)
    rendered = safe_html(rewritten)
    assert "Sales in millions of dollars" in rendered
