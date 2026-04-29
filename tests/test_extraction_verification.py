"""Tests for the publisher-agnostic LLM extraction-verification module
(:mod:`services.extraction_verification`) plus the Kaplan-specific glue
in :mod:`services.kaplan_verification`.

Network-free: every LLM call goes through a stub client that returns
canned JSON.
"""
import io
import json
import os
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services import extraction_verification as ev


# ── Stubs ────────────────────────────────────────────────────────────


class _FakeClient:
    """Pretends to be a FloodgateClient. Records every call."""

    def __init__(self, scripted_responses: List[str]):
        self.responses = list(scripted_responses)
        self.calls: List[Dict[str, Any]] = []

    def call_anthropic(self, *, model, system, messages, max_tokens):
        self.calls.append({
            "model": model, "system": system,
            "messages": messages, "max_tokens": max_tokens,
        })
        if not self.responses:
            return '{"verified": true}'
        return self.responses.pop(0)


# ── verify_question ─────────────────────────────────────────────────


def test_verify_question_returns_verified_true_for_clean_extraction():
    client = _FakeClient(['{"verified": true}'])
    q = {
        "qst_id": "kaplan_2024:chapter05:set1:q1",
        "subtype": "tc",
        "prompt": "<p>The squid is _____.</p>",
        "options": [{"label": "B", "text": "elusive", "is_correct": True}],
        "correct_label": "B",
    }
    out = ev.verify_question(
        q, render_fn=lambda _q: b"\xff\xd8FAKE_JPEG_BYTES",
        client=client, model="anthropic.claude-sonnet-4-6",
        media_type="image/jpeg",
    )
    assert out["verified"] is True
    assert out.get("defects") == []
    assert "cost_estimate_usd" in out
    assert len(client.calls) == 1


def test_verify_question_skips_when_render_returns_none():
    """When ``fallback_text_only=False`` we get the legacy 'skipped' shape."""
    client = _FakeClient([])
    q = {"qst_id": "x", "subtype": "tc", "prompt": "p", "options": []}
    out = ev.verify_question(q, render_fn=lambda _q: None, client=client,
                             fallback_text_only=False)
    assert out["skipped"] is True
    assert out["verified"] is False
    assert client.calls == []


def test_verify_question_falls_back_to_text_only_when_no_image():
    """Default behaviour: image rendering failure routes to the cheaper
    text-only consistency check rather than producing a skipped verdict
    (otherwise text-only items like SE/RC have ~0% verification coverage)."""
    client = _FakeClient([json.dumps({"verified": True})])
    q = {"qst_id": "x", "subtype": "tc", "prompt": "p", "options": []}
    out = ev.verify_question(q, render_fn=lambda _q: None, client=client,
                             model="anthropic.claude-sonnet-4-6")
    assert out["verified"] is True
    assert out.get("source") == "text_only_check"
    assert len(client.calls) == 1


def test_verify_question_text_only_returns_text_only_source_tag():
    client = _FakeClient([json.dumps({"verified": False,
                                      "defects": ["wrong_option_text"]})])
    q = {"qst_id": "x", "subtype": "se", "prompt": "p", "options": [],
         "correct_label": "A"}
    out = ev.verify_question_text_only(q, client=client,
                                       model="anthropic.claude-sonnet-4-6")
    assert out["verified"] is False
    assert out["source"] == "text_only_check"
    assert "wrong_option_text" in out["defects"]


def test_verify_question_parses_defect_response():
    client = _FakeClient([
        json.dumps({
            "verified": False,
            "defects": ["missing_inline_math", "wrong_option_text"],
            "suggested_correction": {
                "stem": "<p>The value of $$\\frac{1}{3}$$ equals…</p>",
            },
        })
    ])
    q = {"qst_id": "x", "subtype": "tc", "prompt": "p", "options": []}
    out = ev.verify_question(q, render_fn=lambda _q: b"\xff\xd8I",
                             client=client)
    assert out["verified"] is False
    assert "missing_inline_math" in out["defects"]
    assert "stem" in out["suggested_correction"]


def test_verify_question_tolerates_markdown_code_fences():
    client = _FakeClient(['```json\n{"verified": true}\n```'])
    q = {"qst_id": "x", "subtype": "tc", "prompt": "p", "options": []}
    out = ev.verify_question(q, render_fn=lambda _q: b"\xff\xd8I",
                             client=client)
    assert out["verified"] is True


# ── apply_correction ─────────────────────────────────────────────────


def test_apply_correction_marks_verified():
    q = {"prompt": "<p>orig</p>", "options": []}
    v = {"verified": True}
    out = ev.apply_correction(q, v)
    assert out["verification_status"] == "verified"


def test_apply_correction_auto_applies_safe_stem_rewrite():
    q = {
        "prompt": "<p>old stem</p>",
        "options": [{"label": "A", "text": "alpha", "is_correct": True}],
    }
    v = {
        "verified": False,
        "defects": ["missing_inline_math"],
        "suggested_correction": {"stem": "<p>NEW stem</p>"},
    }
    out = ev.apply_correction(q, v)
    assert out["prompt"] == "<p>NEW stem</p>"
    assert out["verification_status"] == "auto_corrected"


def test_apply_correction_escalates_wrong_correct_label_to_draft():
    q = {"prompt": "p", "options": [{"label": "A", "text": "x"}]}
    v = {
        "verified": False,
        "defects": ["wrong_correct_label"],
        "suggested_correction": {"correct_label": "B"},
    }
    out = ev.apply_correction(q, v)
    assert out["verification_status"] == "draft"
    assert "review_notes" in out


def test_apply_correction_escalates_when_option_count_changes():
    q = {
        "prompt": "p",
        "options": [
            {"label": "A", "text": "x"},
            {"label": "B", "text": "y"},
        ],
    }
    v = {
        "verified": False,
        "defects": ["wrong_option_text"],
        "suggested_correction": {
            "options": [{"label": "A", "text": "X"}],
        },
    }
    out = ev.apply_correction(q, v)
    assert out["verification_status"] == "draft"


# ── verify_many budget enforcement ───────────────────────────────────


def test_verify_many_stops_when_budget_exhausted():
    # First two return verified, then budget cap kicks in.
    client = _FakeClient([
        '{"verified": true}',
        '{"verified": true}',
        '{"verified": true}',
    ])
    questions = [{"qst_id": str(i), "subtype": "tc",
                  "prompt": "p", "options": []} for i in range(5)]
    out = ev.verify_many(
        questions,
        render_fn=lambda _q: b"\xff\xd8I",
        client=client, apply=False,
        budget_usd=0.012,  # ~2 calls @ ~0.005 each
    )
    assert len(out) == 5
    skipped = sum(1 for v in out if v.get("skipped"))
    assert skipped >= 1


# ── Figure-question alignment ────────────────────────────────────────


def test_check_figure_alignment_returns_match_verdict():
    client = _FakeClient([json.dumps({
        "verdict": "matches",
        "rationale": "Triangle in figure aligns with stem reference.",
        "stem_references": ["the diagram above"],
        "figure_summary": "Triangle with labelled angles.",
    })])
    q = {
        "prompt": "In the diagram above, what is angle a?",
        "subtype": "numeric_entry",
        "options": [],
    }
    v = ev.check_figure_alignment(
        q, b"\xff\xd8FAKE", client=client,
        model="anthropic.claude-sonnet-4-6",
    )
    assert v["verdict"] == "matches"
    assert "Triangle" in v["figure_summary"]


def test_check_figure_alignment_returns_mismatch_verdict():
    client = _FakeClient([json.dumps({
        "verdict": "mismatch",
        "rationale": "Stem talks about a triangle but figure shows a bar chart.",
        "stem_references": ["the diagram"],
        "figure_summary": "Bar chart of unrelated economic data.",
    })])
    q = {"prompt": "In the diagram, what is the value of a?",
         "subtype": "numeric_entry", "options": []}
    v = ev.check_figure_alignment(q, b"\xff\xd8FAKE", client=client)
    assert v["verdict"] == "mismatch"


def test_check_figure_alignment_handles_unsure():
    client = _FakeClient([json.dumps({
        "verdict": "unsure",
        "rationale": "Stem doesn't reference a specific figure.",
        "stem_references": [],
        "figure_summary": "Generic geometric shape.",
    })])
    q = {"prompt": "Compute the value of x.",
         "subtype": "numeric_entry", "options": []}
    v = ev.check_figure_alignment(q, b"\xff\xd8FAKE", client=client)
    assert v["verdict"] == "unsure"


# ── Multi-model cross-check ──────────────────────────────────────────


def test_cross_check_agrees_when_both_models_verify():
    client = _FakeClient([
        '{"verified": true}',
        '{"verified": true}',
    ])
    q = {"qst_id": "x", "subtype": "tc", "prompt": "p", "options": []}
    out = ev.cross_check(
        q, render_fn=lambda _q: b"\xff\xd8I",
        client=client,
        primary_model="m1", secondary_model="m2",
    )
    assert out["verified"] is True
    assert out["agreement"] == "agree"


def test_cross_check_flags_disagreement():
    client = _FakeClient([
        '{"verified": true}',
        json.dumps({"verified": False, "defects": ["missing_inline_math"]}),
    ])
    q = {"qst_id": "x", "subtype": "tc", "prompt": "p", "options": []}
    out = ev.cross_check(
        q, render_fn=lambda _q: b"\xff\xd8I",
        client=client,
        primary_model="m1", secondary_model="m2",
    )
    assert out["verified"] is False
    assert out["agreement"] == "disagree"


# ── Kaplan-specific glue ─────────────────────────────────────────────


def test_render_kaplan_question_prefers_figure_over_glyphs():
    """Glue function should prefer figure_image bytes when available."""
    from services import kaplan_verification as kv
    import zipfile
    # Build an in-memory EPUB.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("OEBPS/images/p1c.jpg", b"\xff\xd8GLYPH_BYTES")
        z.writestr("OEBPS/images/325b.jpg", b"\xff\xd8FIGURE_BYTES")
    buf.seek(0)
    z = zipfile.ZipFile(buf)
    item = {
        "figure_image": "325b.jpg",
        "inline_glyph_files": ["p1c.jpg"],
    }
    out = kv.render_kaplan_question(item, epub=z)
    assert out == b"\xff\xd8FIGURE_BYTES"


def test_render_kaplan_question_falls_back_to_glyph_when_no_figure():
    from services import kaplan_verification as kv
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("OEBPS/images/p65a.jpg", b"\xff\xd8OPTION_TABLE_BYTES")
    buf.seek(0)
    z = zipfile.ZipFile(buf)
    item = {
        "figure_image": None,
        "inline_glyph_files": ["p65a.jpg"],
    }
    out = kv.render_kaplan_question(item, epub=z)
    assert out == b"\xff\xd8OPTION_TABLE_BYTES"


def test_stem_references_figure_detects_diagram_reference():
    from services import kaplan_verification as kv
    assert kv._stem_references_figure(
        {"prompt": "In the diagram above, what is x?"}
    )
    assert kv._stem_references_figure(
        {"prompt": "Refer to the chart shown below."}
    )
    assert not kv._stem_references_figure(
        {"prompt": "Compute the value of 6 - 4 * 2."}
    )