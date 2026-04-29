"""Tests for ``scripts.backfill_princeton_subtopics``.

These tests lock in the deterministic pieces:
- Allowlist contains every taxonomy subtopic plus ``unclassified``.
- ``parse_response`` handles clean JSON, prose-wrapped JSON, code
  fences, and missing content.
- ``classify_batch`` routes OOV subtopics to ``unclassified``.
- ``build_prompt`` filters the catalog to a single measure and never
  exposes verbal subtopics to a quant batch (or vice versa).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backfill_princeton_subtopics import (  # noqa: E402
    UNCLASSIFIED,
    build_prompt,
    classify_batch,
    load_allowlist,
    parse_response,
)


def test_allowlist_contains_core_quant_subtopics():
    allow = load_allowlist()
    # A few load-bearing ids from models/taxonomy.
    for sid in ("triangles", "linear_equations_systems", "percents",
                "probability", "rc_main_idea", "tc_1_blank"):
        assert sid in allow, f"missing subtopic {sid}"
    assert UNCLASSIFIED in allow


def test_allowlist_measure_tagged_correctly():
    allow = load_allowlist()
    assert allow["triangles"]["measure"] == "quant"
    assert allow["rc_main_idea"]["measure"] == "verbal"
    assert allow[UNCLASSIFIED]["measure"] == "any"


def test_parse_response_clean_json_array():
    raw = json.dumps([
        {"qid": 1, "subtopic": "triangles"},
        {"qid": 2, "subtopic": "percents"},
    ])
    parsed = parse_response(raw)
    assert len(parsed) == 2
    assert parsed[0]["qid"] == 1


def test_parse_response_strips_markdown_fences():
    raw = "```json\n" + json.dumps(
        [{"qid": 1, "subtopic": "triangles"}]
    ) + "\n```"
    parsed = parse_response(raw)
    assert parsed == [{"qid": 1, "subtopic": "triangles"}]


def test_parse_response_extracts_from_prose():
    raw = ("Sure, here are the classifications:\n"
           + json.dumps([{"qid": 5, "subtopic": "probability"}])
           + "\nHope this helps!")
    parsed = parse_response(raw)
    assert parsed == [{"qid": 5, "subtopic": "probability"}]


def test_parse_response_items_key_fallback():
    raw = json.dumps({"items": [{"qid": 1, "subtopic": "triangles"}]})
    parsed = parse_response(raw)
    assert parsed == [{"qid": 1, "subtopic": "triangles"}]


def test_parse_response_garbage_returns_empty():
    assert parse_response("no JSON at all here") == []


def test_build_prompt_quant_excludes_verbal_subtopics():
    allow = load_allowlist()
    batch = [{"qid": 1, "subtype": "mcq_single", "stem": "2 + 2 ?"}]
    prompt = build_prompt(batch, allow, "quant")
    assert "triangles" in prompt
    # Verbal subtopic should NOT appear in a quant prompt.
    assert "rc_main_idea" not in prompt


def test_build_prompt_verbal_excludes_quant_subtopics():
    allow = load_allowlist()
    batch = [{"qid": 2, "subtype": "rc_single", "stem": "The author…"}]
    prompt = build_prompt(batch, allow, "verbal")
    assert "rc_main_idea" in prompt
    assert "triangles" not in prompt


class _FakeClient:
    """Stand-in for ``FloodgateClient`` used in classify_batch."""
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls: List[Dict[str, Any]] = []

    def call_anthropic(self, *, model, system, messages, max_tokens,
                       max_retries):
        self.calls.append({"model": model, "messages": messages})
        return self.response_text


def test_classify_batch_maps_valid_ids():
    allow = load_allowlist()
    batch = [
        {"qid": 10, "subtype": "mcq_single", "stem": "Equilateral…"},
        {"qid": 11, "subtype": "mcq_single", "stem": "Given 3x+5=11…"},
    ]
    fake = _FakeClient(json.dumps([
        {"qid": 10, "subtopic": "triangles"},
        {"qid": 11, "subtopic": "linear_equations_systems"},
    ]))
    result = classify_batch(fake, batch, allow, "quant")
    assert result == {10: "triangles", 11: "linear_equations_systems"}


def test_classify_batch_routes_oov_to_unclassified():
    allow = load_allowlist()
    batch = [{"qid": 20, "subtype": "mcq_single", "stem": "?"}]
    # Haiku hallucinates a subtopic that isn't in the allowlist.
    fake = _FakeClient(json.dumps([
        {"qid": 20, "subtopic": "quantum_topology"}
    ]))
    result = classify_batch(fake, batch, allow, "quant")
    assert result == {20: UNCLASSIFIED}


def test_classify_batch_missing_qid_defaults_to_unclassified():
    allow = load_allowlist()
    batch = [
        {"qid": 30, "subtype": "mcq_single", "stem": "a"},
        {"qid": 31, "subtype": "mcq_single", "stem": "b"},
    ]
    # Haiku only returns the first item.
    fake = _FakeClient(json.dumps([
        {"qid": 30, "subtopic": "percents"}
    ]))
    result = classify_batch(fake, batch, allow, "quant")
    assert result[30] == "percents"
    assert result[31] == UNCLASSIFIED


def test_classify_batch_cross_measure_subtopic_rejected():
    """A quant batch returning 'rc_main_idea' must map to unclassified."""
    allow = load_allowlist()
    batch = [{"qid": 40, "subtype": "mcq_single", "stem": "x + 1 ?"}]
    fake = _FakeClient(json.dumps([
        {"qid": 40, "subtopic": "rc_main_idea"},  # verbal leak
    ]))
    result = classify_batch(fake, batch, allow, "quant")
    assert result == {40: UNCLASSIFIED}
