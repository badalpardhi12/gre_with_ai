"""Tests for WS-B figure wiring: render_spec geometry resolution into the
canonical question-dict path, and migration 040 (retire-unrenderable).
"""
from __future__ import annotations

import json
import sys

import pytest


def test_resolve_passthrough_when_content_has_figure():
    from services.question_bank import _resolve_stimulus_content
    html = '<div><img src="data:image/png;base64,AAAA"></div>'
    assert _resolve_stimulus_content(html, None) == html
    # render_spec is ignored when content already shows a figure
    spec = json.dumps({"spec": {"kind": "triangle", "params": {"kind": "right"}}})
    assert _resolve_stimulus_content(html, spec) == html


def test_resolve_passthrough_when_no_render_spec():
    from services.question_bank import _resolve_stimulus_content
    assert _resolve_stimulus_content("Figure not drawn to scale.", None) == \
        "Figure not drawn to scale."
    assert _resolve_stimulus_content("Figure not drawn to scale.", "") == \
        "Figure not drawn to scale."


def test_resolve_renders_geometry_from_render_spec():
    from services.question_bank import _resolve_stimulus_content
    spec = json.dumps({
        "kind": "svg_geometry",
        "spec": {"kind": "triangle",
                 "params": {"kind": "right", "right_angle_at": "B",
                            "side_labels": {"AB": "9", "BC": "12", "AC": "15"}},
                 "caption": "Figure not drawn to scale."},
    })
    out = _resolve_stimulus_content("Figure not drawn to scale.", spec)
    assert "data:image/png;base64," in out
    assert "<img" in out


def test_resolve_unknown_kind_falls_back_to_caption():
    from services.question_bank import _resolve_stimulus_content
    spec = json.dumps({"spec": {"kind": "fractal", "params": {}}})
    # unsupported kind -> original caption, never raises
    assert _resolve_stimulus_content("caption only", spec) == "caption only"


def test_resolve_is_cached(monkeypatch):
    import services.question_bank as qb
    spec = json.dumps({"spec": {"kind": "circle", "params": {"radius_label": "r"}}})
    qb._FIGURE_HTML_CACHE.clear()
    first = qb._resolve_stimulus_content("cap", spec)
    assert spec in qb._FIGURE_HTML_CACHE
    # second call: monkeypatch the renderer to blow up; cache must serve it
    import services.figures.geometry as g
    monkeypatch.setattr(g, "render_geometry_html",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    second = qb._resolve_stimulus_content("cap", spec)
    assert second == first


@pytest.fixture(autouse=True)
def _evict_migrations_module():
    for mod in [m for m in list(sys.modules)
                if m == "models.migrations" or m.startswith("models.migrations.")]:
        del sys.modules[mod]
    yield


def test_040_registered_in_ledger(temp_db):
    import models.migrations as m
    applied = {row.name for row in m.SchemaMigration.select(m.SchemaMigration.name)}
    assert "040_retire_unrenderable_figure_2026_06_01" in applied


def test_040_retires_target_and_is_idempotent(temp_db):
    from models.database import Question
    import models.migrations as m
    Question.create(id=4252, measure="quant", subtype="mcq_multi",
                    prompt="points on circle Q", explanation="needs figure",
                    source="princeton_2012", status="live")
    m._040_retire_unrenderable_figure_2026_06_01()
    assert Question.get_by_id(4252).status == "retired"
    m._040_retire_unrenderable_figure_2026_06_01()  # idempotent
    assert Question.get_by_id(4252).status == "retired"
