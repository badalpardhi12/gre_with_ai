"""
Smoke tests for the synthetic figure generators.

We don't try to validate the rendered pixels — that would couple the
test to matplotlib font metrics. Instead we just confirm the
dispatcher writes a non-empty file of the right format.
"""
from __future__ import annotations

from pathlib import Path

from services.synthetic.figures import (
    render_data_interp,
    render_geometry,
)


def test_geometry_triangle(tmp_path):
    spec = {
        "kind": "triangle",
        "params": {
            "kind": "right",
            "side_labels": {"AB": "5", "BC": "12", "AC": "13"},
            "right_angle_at": "A",
        },
        "caption": "Right triangle ABC",
    }
    out = tmp_path / "tri.svg"
    fig = render_geometry(spec, out)
    assert out.exists() and out.stat().st_size > 100
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<svg")
    assert "</svg>" in text
    assert fig.kind == "triangle"


def test_geometry_circle(tmp_path):
    spec = {
        "kind": "circle",
        "params": {
            "radius_label": "r",
            "show_diameter": False,
            "show_chord": {"angle1_deg": 30, "angle2_deg": 150,
                            "label": "AB"},
        },
    }
    out = tmp_path / "circ.svg"
    fig = render_geometry(spec, out)
    assert out.exists()
    assert fig.kind == "circle"
    assert "<circle" in out.read_text(encoding="utf-8")


def test_geometry_coordinate(tmp_path):
    spec = {
        "kind": "coordinate",
        "params": {
            "x_min": -3, "x_max": 6, "y_min": -2, "y_max": 5,
            "line": {"slope": 0.5, "intercept": 1},
            "points": [
                {"x": 2, "y": 2, "label": "P"},
                {"x": -1, "y": 0.5, "label": "Q"},
            ],
        },
    }
    out = tmp_path / "coord.svg"
    fig = render_geometry(spec, out)
    assert out.exists() and fig.kind == "coordinate"


def test_geometry_unknown_kind_falls_back(tmp_path):
    spec = {"kind": "rhomboid_jewel", "params": {}}
    out = tmp_path / "fallback.svg"
    fig = render_geometry(spec, out)
    assert out.exists()


def test_data_interp_bar(tmp_path):
    spec = {
        "kind": "bar",
        "title": "Sales by Region (Q1-Q4)",
        "x_label": "Quarter",
        "y_label": "Units sold (thousands)",
        "categories": ["Q1", "Q2", "Q3", "Q4"],
        "series": [
            {"label": "Region A", "values": [12, 17, 9, 11]},
            {"label": "Region B", "values": [10, 14, 13, 8]},
        ],
    }
    out = tmp_path / "bar.png"
    fig = render_data_interp(spec, out)
    assert out.exists() and out.stat().st_size > 1000
    assert fig.kind == "bar"


def test_data_interp_pie(tmp_path):
    spec = {
        "kind": "pie",
        "title": "Market share",
        "series": [{"labels": ["A", "B", "C", "D"], "values": [40, 25, 20, 15]}],
    }
    out = tmp_path / "pie.png"
    fig = render_data_interp(spec, out)
    assert out.exists()


def test_data_interp_table(tmp_path):
    spec = {
        "kind": "table",
        "title": "Population by Year",
        "columns": ["Year", "City X", "City Y"],
        "rows": [
            ["2018", 100, 80],
            ["2019", 110, 85],
            ["2020", 95, 90],
        ],
    }
    out = tmp_path / "table.png"
    fig = render_data_interp(spec, out)
    assert out.exists() and fig.kind == "table"


def test_data_interp_scatter(tmp_path):
    spec = {
        "kind": "scatter",
        "title": "Hours studied vs. test score",
        "x_label": "Hours",
        "y_label": "Score",
        "series": [
            {"label": "Class A", "points": [
                {"x": 1, "y": 60}, {"x": 3, "y": 72},
                {"x": 5, "y": 81}, {"x": 7, "y": 88},
            ]},
        ],
    }
    out = tmp_path / "scatter.png"
    fig = render_data_interp(spec, out)
    assert out.exists()


def test_data_interp_line(tmp_path):
    spec = {
        "kind": "line",
        "title": "Monthly Rainfall",
        "categories": ["Jan", "Feb", "Mar", "Apr", "May"],
        "series": [{"label": "City", "values": [3.1, 2.8, 4.0, 3.5, 2.2]}],
    }
    out = tmp_path / "line.png"
    fig = render_data_interp(spec, out)
    assert out.exists() and fig.kind == "line"
