"""Unit tests for the Kaplan validation gates V1-V14
(``validators.kaplan``)."""
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from validators import kaplan as kv


def _item(**overrides):
    base = {
        "chapter_id": "chapter05",
        "section_title": "Practice",
        "measure": "verbal",
        "subtype": "tc",
        "q_number": 1,
        "prompt": "<p>The squid's body is _________ .</p>",
        "options": [
            {"label": "A", "text": "meaningful", "is_correct": False},
            {"label": "B", "text": "elusive", "is_correct": True},
            {"label": "C", "text": "popular", "is_correct": False},
            {"label": "D", "text": "expensive", "is_correct": False},
            {"label": "E", "text": "profitable", "is_correct": False},
        ],
        "explanation": (
            "<p class='tx1-1'><b>1. B</b></p><p>The squid is hard to find. "
            "Choice (B) elusive matches the prediction.</p>"
        ),
        "correct_label": "B",
        "explanation_label": "B",
        "difficulty_band": None,
        "has_figure": False,
        "figure_image": None,
        "rc_group_key": None,
        "numeric_value": None,
        "inline_glyph_files": [],
        "source_ref": "kaplan_2024:chapter05:set1:q1",
    }
    base.update(overrides)
    return base


def _kinds(issues):
    return {i["kind"] for i in issues}


def test_clean_item_passes_all_gates():
    issues = kv.validate(_item())
    blockers = [i for i in issues if i["severity"] == "block"]
    assert blockers == [], blockers


def test_v1_blocks_wrong_option_count():
    item = _item(options=[{"label": "A", "text": "x", "is_correct": True}])
    issues = kv.validate(item)
    assert "option_count_correct" in _kinds(issues)


def test_v2_blocks_duplicate_option_text():
    options = [
        {"label": "A", "text": "elusive", "is_correct": True},
        {"label": "B", "text": "Elusive", "is_correct": False},  # dup
        {"label": "C", "text": "popular", "is_correct": False},
        {"label": "D", "text": "expensive", "is_correct": False},
        {"label": "E", "text": "profitable", "is_correct": False},
    ]
    item = _item(options=options)
    issues = kv.validate(item)
    assert "distractor_uniqueness" in _kinds(issues)


def test_v3_blocks_zero_correct():
    options = [
        {"label": "A", "text": "a", "is_correct": False},
        {"label": "B", "text": "b", "is_correct": False},
        {"label": "C", "text": "c", "is_correct": False},
        {"label": "D", "text": "d", "is_correct": False},
        {"label": "E", "text": "e", "is_correct": False},
    ]
    item = _item(options=options)
    issues = kv.validate(item)
    assert "at_least_one_correct_marked" in _kinds(issues)


def test_v4_blocks_two_correct_for_single_subtype():
    options = [
        {"label": "A", "text": "a", "is_correct": False},
        {"label": "B", "text": "b", "is_correct": True},
        {"label": "C", "text": "c", "is_correct": True},  # extra
        {"label": "D", "text": "d", "is_correct": False},
        {"label": "E", "text": "e", "is_correct": False},
    ]
    item = _item(subtype="rc_single", options=options)
    issues = kv.validate(item)
    assert "single_correct_for_single_subtypes" in _kinds(issues)


def test_v5_warns_on_answer_key_drift():
    item = _item(correct_label="B", explanation_label="C")
    issues = kv.validate(item)
    drift = [i for i in issues if i["kind"] == "answer_key_cross_ref"]
    assert drift and drift[0]["severity"] == "warn"


def test_v6_blocks_unbalanced_paren_in_prompt():
    item = _item(prompt="<p>The value of \\(x^2 is two.</p>")
    issues = kv.validate(item)
    latex_issues = [i for i in issues if i["kind"] == "latex_well_formed"]
    assert any(i["severity"] == "block" for i in latex_issues)


def test_v7_warns_on_singleton_rc():
    item = _item(subtype="rc_single", rc_group_key=None)
    issues = kv.validate(item)
    rc_issues = [i for i in issues if i["kind"] == "rc_cluster_coherence"]
    assert rc_issues and rc_issues[0]["severity"] == "warn"


def test_v8_blocks_unparseable_numeric_value():
    item = _item(
        subtype="numeric_entry", options=[],
        prompt="<p>What is the missing value?</p>",
        numeric_value="not a number",
        correct_label="not a number",
        explanation_label="not a number",
    )
    issues = kv.validate(item)
    assert "numeric_answer_parseable" in _kinds(issues)


def test_v8_warns_on_glyph_only_numeric_answer():
    item = _item(
        subtype="numeric_entry", options=[],
        prompt="<p>Compute the result.</p>",
        numeric_value="@@GLYPH:228b.jpg@@",
        correct_label="@@GLYPH:228b.jpg@@",
        explanation_label="",
    )
    issues = kv.validate(item)
    np_issues = [i for i in issues if i["kind"] == "numeric_answer_parseable"]
    assert np_issues and np_issues[0]["severity"] == "warn"


def test_v9_warns_on_short_explanation():
    item = _item(explanation="<p>too short</p>")
    issues = kv.validate(item)
    assert "explanation_present" in _kinds(issues)


def test_v10_blocks_when_figure_flag_set_without_image():
    item = _item(measure="quant", subtype="mcq_single",
                 options=[
                     {"label": "A", "text": "a", "is_correct": True},
                     {"label": "B", "text": "b", "is_correct": False},
                     {"label": "C", "text": "c", "is_correct": False},
                     {"label": "D", "text": "d", "is_correct": False},
                     {"label": "E", "text": "e", "is_correct": False},
                 ],
                 has_figure=True, figure_image=None)
    issues = kv.validate(item)
    assert "figure_attached_when_referenced" in _kinds(issues)


def test_v11_blocks_qc_missing_quantity_labels():
    item = _item(measure="quant", subtype="qc",
                 prompt="<p>Compare x and y.</p>",
                 options=[
                     {"label": "A", "text": "Quantity A is greater.",
                      "is_correct": True},
                     {"label": "B", "text": "Quantity B is greater.",
                      "is_correct": False},
                     {"label": "C", "text": "Equal.", "is_correct": False},
                     {"label": "D", "text": "Cannot be determined.",
                      "is_correct": False},
                 ])
    issues = kv.validate(item)
    assert "qc_quantity_labels_present" in _kinds(issues)


def test_v12_blocks_money_artefact():
    item = _item(prompt="<p>You owe $$50$ to the store.</p>")
    issues = kv.validate(item)
    assert "money_clean" in _kinds(issues)


def test_v13_warns_on_orphan_glyph_in_prompt():
    item = _item(prompt='<p>Compute <img class="inline" '
                        'src="images/p1c.jpg"/>.</p>')
    issues = kv.validate(item)
    glyph = [i for i in issues if i["kind"] == "no_orphan_glyph"]
    assert glyph and glyph[0]["severity"] == "warn"


def test_v14_blocks_empty_prompt():
    item = _item(prompt="<p> </p>")
    issues = kv.validate(item)
    assert "prompt_nonempty" in _kinds(issues)


def test_summarise_returns_aggregated_counts():
    items = [
        _item(),
        _item(prompt="<p>x</p>"),  # V14 block
    ]
    per_item, gate_counts, severity_counts = kv.summarise(items)
    assert len(per_item) == 2
    assert gate_counts.get("prompt_nonempty", 0) >= 1
    assert severity_counts.get("block", 0) >= 1
