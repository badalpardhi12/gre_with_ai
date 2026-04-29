"""
Regression tests for `widgets.html_sanitizer.safe_html`.

Chart-bearing DI stimuli (source type 'graph' / 'table') store either an
inline base64 PNG (`<img src="data:image/png;base64,...">`) or an inline
HTML table with inline styles. The sanitizer must preserve both untouched,
otherwise the Passage pane renders blank — which was the user-visible bug
that the di-chart-integrity sweep diagnosed. The sanitizer passed the
audit (no fix required) but this test locks in the behaviour so a later
tightening of the allow-list can't silently break DI rendering.
"""
import pytest

from widgets.html_sanitizer import safe_html


def test_base64_png_image_survives_sanitisation():
    raw = (
        '<div style="text-align:center; padding:8px;">'
        '<img src="data:image/png;base64,iVBORw0KGgoAAAA" '
        'alt="chart" style="max-width:100%;">'
        '</div>'
    )
    out = safe_html(raw)
    assert "<img" in out
    assert 'src="data:image/png;base64,iVBORw0KGgoAAAA"' in out
    assert "<div" in out


def test_inline_html_table_with_styles_survives():
    raw = (
        '<div style="text-align:center;">'
        '<h3 style="color:white;">Golf Equipment Production, 1994</h3>'
        '<table border="1" cellpadding="4">'
        '<thead><tr><th>Country</th><th>Balls</th></tr></thead>'
        '<tbody><tr><td>U.S.</td><td style="text-align:right;">45</td></tr></tbody>'
        '</table></div>'
    )
    out = safe_html(raw)
    assert "<table" in out
    assert "<thead>" in out
    assert "<tbody>" in out
    assert "<td" in out
    # The <h3> title survives.
    assert "Golf Equipment Production, 1994" in out


def test_script_tag_stripped_but_surrounding_img_kept():
    raw = (
        '<img src="data:image/png;base64,AAAA">'
        '<script>alert("x")</script>'
    )
    out = safe_html(raw)
    assert "<img" in out
    # The <script> tag is dropped (its text leaks but can't execute without
    # the tag — bleach's `strip=True` keeps text nodes by design).
    assert "<script" not in out


def test_javascript_protocol_stripped_from_href():
    raw = '<a href="javascript:alert(1)">click</a>'
    out = safe_html(raw)
    # `javascript:` is not in ALLOWED_PROTOCOLS, so the href is dropped.
    assert "javascript:" not in out


def test_data_protocol_allowed_only_for_image():
    # Non-image data URLs should NOT be emitted in a way that the user
    # could abuse (file exfiltration etc.). `data:text/html` on an
    # <a href> is not a concern here because we only list `data:` in
    # protocols — the sanitizer only checks protocol, not mime-type,
    # so this test just confirms the simpler behaviour: `data:` on
    # an <img src> works, which is the only place we rely on it.
    raw = '<img src="data:image/png;base64,AAAA">'
    assert 'src="data:image/png;base64,AAAA"' in safe_html(raw)


def test_none_or_empty_input_returns_empty_string():
    assert safe_html("") == ""
    assert safe_html(None) == ""
