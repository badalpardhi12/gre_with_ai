"""Unit tests for scripts/visual_sweep.py — prompt builder, verdict parser,
issue router. No live LLM calls; everything is mocked.

Run:
    venv/bin/python -m pytest tests/test_visual_sweep.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import visual_sweep as vs  # noqa: E402


# ── Prompt builder ──────────────────────────────────────────────────────

def _row(**over):
    base = {
        "id": 1,
        "subtype": "tc",
        "measure": "verbal",
        "source": "princeton_2012",
        "prompt": "The _____ dog barked.",
        "explanation": "Loud = barking.",
        "status": "live",
        "stimulus_id": None,
        "figure_refs_list": "",
        "figure_refs_raw": [],
        "_stim": None,
        "_options": [
            {"option_label": "A", "option_text": "loud", "is_correct": True},
            {"option_label": "B", "option_text": "quiet", "is_correct": False},
        ],
    }
    base.update(over)
    return base


def test_build_prompt_contains_stem_and_options():
    row = _row()
    text = vs.build_prompt(row, row["_options"], None)
    assert "dog barked" in text
    assert "A. loud" in text
    assert "[CORRECT]" in text
    assert "Subtype: tc" in text
    assert "Stimulus: (none" in text


def test_build_prompt_strips_html():
    row = _row(prompt="<p>The <b>red</b> ball.</p>")
    text = vs.build_prompt(row, row["_options"], None)
    assert "<p>" not in text
    assert "<b>" not in text
    assert "The red ball." in text


def test_build_prompt_includes_stimulus_body():
    stim = {"stimulus_type": "passage", "title": "Birds",
            "content": "<p>Some birds migrate.</p>"}
    row = _row(stimulus_id=42, _stim=stim)
    text = vs.build_prompt(row, row["_options"], stim)
    assert "Some birds migrate." in text
    assert "Stimulus type: passage" in text


def test_build_prompt_flags_image_presence():
    row = _row(figure_refs_list="fi065.gif")
    text = vs.build_prompt(row, row["_options"], None)
    assert "fi065.gif" in text
    assert "attached image" in text


# ── Verdict parser ──────────────────────────────────────────────────────

def test_parse_verdict_valid_json():
    raw = '{"coherent": false, "issues": ["missing_options"], ' \
          '"confidence": "high", "reasoning": "only 1 option"}'
    v = vs.parse_verdict(raw)
    assert v["coherent"] is False
    assert v["issues"] == ["missing_options"]
    assert v["confidence"] == "high"


def test_parse_verdict_strips_markdown_fence():
    raw = "```json\n" \
          '{"coherent": true, "issues": [], "confidence": "medium", "reasoning": "ok"}\n' \
          "```"
    v = vs.parse_verdict(raw)
    assert v["coherent"] is True
    assert v["issues"] == []


def test_parse_verdict_unknown_issue_coerced_to_other():
    raw = '{"coherent": false, "issues": ["some-nonsense-tag"], ' \
          '"confidence": "medium", "reasoning": "x"}'
    v = vs.parse_verdict(raw)
    assert "other" in v["issues"]
    assert "some-nonsense-tag" not in v["issues"]


def test_parse_verdict_malformed_returns_low_confidence():
    v = vs.parse_verdict("not json at all")
    assert v["confidence"] == "low"
    assert v["coherent"] is True  # don't demote on parse failure
    assert v.get("_parse_failed") is True


def test_parse_verdict_normalizes_confidence():
    raw = '{"coherent": true, "issues": [], "confidence": "HIGH", "reasoning": "ok"}'
    v = vs.parse_verdict(raw)
    assert v["confidence"] == "high"


# ── Issue router (decide_action) ────────────────────────────────────────

def _verdict(sonnet_coherent, sonnet_issues, sonnet_conf,
             opus_coherent=None, opus_issues=None, opus_conf=None):
    v = {
        "sonnet": {
            "coherent": sonnet_coherent,
            "issues": list(sonnet_issues),
            "confidence": sonnet_conf,
            "reasoning": "-",
        },
        "opus": None,
    }
    if opus_coherent is not None:
        v["opus"] = {
            "coherent": opus_coherent,
            "issues": list(opus_issues or []),
            "confidence": opus_conf or "medium",
            "reasoning": "-",
        }
    return v


def test_decide_action_both_coherent_keeps_live():
    v = _verdict(True, [], "high", True, [], "high")
    action, issues = vs.decide_action(v)
    assert action == "keep_live"
    assert issues == []


def test_decide_action_only_sonnet_no_opus_keeps_live():
    v = _verdict(True, [], "high")
    action, _ = vs.decide_action(v)
    assert action == "keep_live"


def test_decide_action_both_flag_high_confidence_demotes():
    v = _verdict(False, ["missing_options"], "high",
                 False, ["missing_options"], "high")
    action, issues = vs.decide_action(v)
    assert action == "demote"
    assert issues == ["missing_options"]


def test_decide_action_caption_inlined_is_log_only_not_demote():
    # caption_inlined is intentionally NOT in DEMOTE_ISSUES.
    v = _verdict(False, ["caption_inlined"], "high",
                 False, ["caption_inlined"], "high")
    action, issues = vs.decide_action(v)
    assert action == "log_only"
    assert issues == []


def test_decide_action_broken_latex_is_log_only():
    v = _verdict(False, ["broken_stem_latex"], "high",
                 False, ["broken_stem_latex"], "high")
    action, _ = vs.decide_action(v)
    assert action == "log_only"


def test_decide_action_both_low_confidence_log_only():
    v = _verdict(False, ["ambiguous_stem"], "low",
                 False, ["ambiguous_stem"], "low")
    action, _ = vs.decide_action(v)
    assert action == "log_only"


def test_decide_action_judges_disagree_log_only():
    # Sonnet flags not-coherent; Opus disagrees → log only.
    v = _verdict(False, ["wrong_figure"], "high",
                 True, [], "high")
    action, _ = vs.decide_action(v)
    assert action == "log_only"


def test_decide_action_shared_demoteable_issue_high_conf():
    v = _verdict(False, ["stem_truncated", "other"], "high",
                 False, ["stem_truncated"], "medium")
    action, issues = vs.decide_action(v)
    assert action == "demote"
    assert "stem_truncated" in issues


# ── Resolver ────────────────────────────────────────────────────────────

def test_resolve_image_path_returns_none_for_missing():
    assert vs.resolve_image_path("images/definitely_not_here_xxx.gif") is None


# ── Sanity: DEMOTE_ISSUES matches spec routing ──────────────────────────

def test_demote_issue_set_matches_spec():
    # Spec says: blank_stimulus, missing_options, wrong_option_count,
    # stem_truncated, wrong_figure, ambiguous_stem, unrelated_distractors
    # should demote; caption_inlined + broken_stem_latex + empty_explanation
    # + duplicate_options + other should NOT auto-demote.
    expected_demote = {
        "blank_stimulus",
        "missing_options",
        "wrong_option_count",
        "stem_truncated",
        "wrong_figure",
        "ambiguous_stem",
        "unrelated_distractors",
    }
    assert vs.DEMOTE_ISSUES == expected_demote
    assert "caption_inlined" not in vs.DEMOTE_ISSUES
    assert "broken_stem_latex" not in vs.DEMOTE_ISSUES
