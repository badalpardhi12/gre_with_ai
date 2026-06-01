"""Tests for the geometry figure generator (services/figures/geometry.py)."""

import base64
import re

import pytest

from services.figures.geometry import (
    SUPPORTED_KINDS,
    render_geometry_html,
    render_geometry_png_bytes,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# One representative spec per supported kind (drawn from real seed items).
_SAMPLE_SPECS = {
    "triangle": {
        "kind": "triangle",
        "params": {
            "kind": "right",
            "right_angle_at": "B",
            "side_labels": {"AB": "9", "BC": "12", "AC": "15"},
        },
        "caption": "Triangle ABC (figure not drawn to scale).",
    },
    "circle": {
        "kind": "circle",
        "params": {
            "radius_label": "r",
            "show_chord": {"angle1_deg": 200, "angle2_deg": 340, "label": "24"},
            "show_perpendicular": {"length_label": "5"},
        },
        "caption": "Figure not drawn to scale.",
    },
    "coordinate": {
        "kind": "coordinate",
        "params": {
            "x_min": -6, "x_max": 8, "y_min": -6, "y_max": 6,
            "line": {"slope": 0.5, "intercept": 1},
            "points": [
                {"x": -2, "y": 0, "label": "(-2, k)"},
                {"x": 6, "y": 4, "label": "(6, k+4)"},
            ],
        },
        "caption": "Figure not drawn to scale.",
    },
    "polygon": {
        "kind": "polygon",
        "params": {"n_sides": 9, "regular": True, "interior_angle_label": "140°"},
        "caption": "Regular nonagon.",
    },
}

_DATA_URI_RE = re.compile(
    r'<div style="text-align: center; padding: 10px;">'
    r'<img src="data:image/png;base64,([A-Za-z0-9+/=]+)">'
)


@pytest.mark.parametrize("kind", sorted(SUPPORTED_KINDS))
def test_html_returns_base64_png_div(kind):
    spec = _SAMPLE_SPECS[kind]
    html = render_geometry_html(spec)
    m = _DATA_URI_RE.search(html)
    assert m is not None, "HTML did not match the figure contract: {}".format(html[:120])
    decoded = base64.b64decode(m.group(1))
    assert decoded.startswith(_PNG_MAGIC)
    # Caption should be rendered as an italic line under the image.
    assert "font-style: italic" in html
    assert html.strip().endswith("</div>")


@pytest.mark.parametrize("kind", sorted(SUPPORTED_KINDS))
def test_png_bytes_are_valid(kind):
    data = render_geometry_png_bytes(_SAMPLE_SPECS[kind])
    assert isinstance(data, bytes)
    assert len(data) > 1000
    assert data.startswith(_PNG_MAGIC)


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        render_geometry_png_bytes({"kind": "hyperbola", "params": {}})
    with pytest.raises(ValueError):
        render_geometry_html({"kind": "hyperbola", "params": {}})


def test_non_dict_spec_raises():
    with pytest.raises(ValueError):
        render_geometry_png_bytes("not a dict")


def test_graceful_on_unknown_subkeys():
    # Unknown sub-keys must not crash; we draw what we understand.
    spec = {
        "kind": "circle",
        "params": {
            "radius_label": "r",
            "show_chord": {"angle1_deg": 20, "angle2_deg": 160, "label": "chord"},
            "mystery_key": {"foo": "bar"},
            "show_tangent": True,
            "inscribed": {"n_sides": 4},
        },
        "caption": "ok",
    }
    data = render_geometry_png_bytes(spec)
    assert data.startswith(_PNG_MAGIC)


def test_no_caption_omits_italic_line():
    spec = {"kind": "polygon", "params": {"n_sides": 6, "regular": True}}
    html = render_geometry_html(spec)
    assert "font-style: italic" not in html
    assert _DATA_URI_RE.search(html) is not None
