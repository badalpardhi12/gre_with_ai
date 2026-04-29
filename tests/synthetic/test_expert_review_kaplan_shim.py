"""
Tests for the unified ``services.expert_review`` module — specifically
the Kaplan-compat shim that merged ``expert_review_kaplan`` into the
canonical module during the 2026-04-28 data quality sweep.

These lock in:
- ``RUBRIC_AXES`` aliases to ``EXPERT_AXES``.
- ``JudgeReport`` (Kaplan shape) parses a clean JSON response.
- ``aggregate_verdict`` produces Kaplan-shaped output with axis_min /
  axis_max / axis_mean and sets ``verdict='live'`` when every axis has
  two judges at >=4 and no spread > 2.
- ``aggregate_verdict`` demotes on spread violations.
- ``build_review_user_message`` renders the Kaplan layout.
- ``render_reviewer_notes`` survives a verdict dict.
- ``embed_review_in_explanation`` + ``extract_review_from_explanation``
  round-trip.
- The deprecated ``services.expert_review_kaplan`` module re-exports
  all the public names from the unified module.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.expert_review import (  # noqa: E402
    DEFECT_TAGS,
    EXPERT_AXES,
    JudgeReport,
    RUBRIC_AXES,
    _parse_judge_response,
    aggregate_verdict,
    build_review_user_message,
    embed_review_in_explanation,
    extract_review_from_explanation,
    render_reviewer_notes,
)


def _all_five(score: int) -> dict:
    return {a: score for a in RUBRIC_AXES}


def _report(judge: str, score: int = 5, **axis_overrides) -> JudgeReport:
    scores = _all_five(score)
    scores.update(axis_overrides)
    return JudgeReport(judge=judge, scores=scores)


def test_rubric_axes_aliases_expert_axes():
    assert RUBRIC_AXES == EXPERT_AXES
    assert len(RUBRIC_AXES) == 5


def test_defect_tags_frozen_vocab():
    # Old Kaplan contract; downstream caches rely on the exact strings.
    assert "wrong_correct_answer" in DEFECT_TAGS
    assert "other" in DEFECT_TAGS


def test_parse_judge_response_clean_json():
    raw = json.dumps({
        "scores": {a: 5 for a in RUBRIC_AXES},
        "defects": ["weak_distractor"],
        "notes": "",
    })
    r = _parse_judge_response("opus", raw)
    assert r.error is None
    assert r.scores == {a: 5 for a in RUBRIC_AXES}
    assert r.defects == ["weak_distractor"]


def test_parse_judge_response_prose_wrapper_tolerated():
    raw = (
        "Sure, here you go:\n"
        + json.dumps({
            "scores": {a: 4 for a in RUBRIC_AXES},
            "defects": [],
            "notes": "ok",
        })
        + "\nthanks"
    )
    r = _parse_judge_response("sonnet", raw)
    assert r.error is None
    assert r.scores == {a: 4 for a in RUBRIC_AXES}


def test_parse_judge_response_no_json_returns_error():
    r = _parse_judge_response("opus", "this is definitely not JSON")
    assert r.error == "no_json"


def test_aggregate_verdict_live_path():
    reports = [_report("opus", 5), _report("sonnet", 4), _report("gemini", 5)]
    v = aggregate_verdict(reports)
    assert v["verdict"] == "live"
    assert v["judge_count"] == 3
    assert set(v["axis_mean"].keys()) == set(RUBRIC_AXES)
    for ax in RUBRIC_AXES:
        assert v["axis_min"][ax] == 4
        assert v["axis_max"][ax] == 5


def test_aggregate_verdict_spread_escalation_demotes():
    # clarity spread = 5 - 2 = 3 > DISAGREEMENT_SPREAD (=2).
    reports = [
        _report("opus", 5),
        _report("sonnet", 5, clarity=2),
        _report("gemini", 5),
    ]
    v = aggregate_verdict(reports)
    assert v["verdict"] == "draft"
    assert v["escalated"] is True


def test_aggregate_verdict_axis_fail_demotes():
    # Only 1 judge at >= 4 on correctness ⇒ fail.
    reports = [
        _report("opus", 5, correctness=3),
        _report("sonnet", 4, correctness=3),
        _report("gemini", 5, correctness=5),
    ]
    v = aggregate_verdict(reports)
    assert v["verdict"] == "draft"
    failing_axes = [f["axis"] for f in v["failures"]]
    assert "correctness" in failing_axes


def test_aggregate_verdict_zero_valid_returns_escalated_draft():
    # All judges errored.
    reports = [
        JudgeReport(judge="opus", error="no_json"),
        JudgeReport(judge="sonnet", error="timeout"),
    ]
    v = aggregate_verdict(reports)
    assert v["verdict"] == "draft"
    assert v["judge_count"] == 0
    assert v["escalated"] is True


def test_build_review_user_message_has_kaplan_headers():
    q = {
        "stem": "Solve 2 + 2.",
        "options": [
            {"label": "A", "text": "3"},
            {"label": "B", "text": "4", "is_correct": True},
        ],
        "subtype": "mcq_single",
        "difficulty": 1,
        "source": "kaplan_2024",
        "explanation": "2+2=4.",
    }
    msg = build_review_user_message(q)
    assert "SUBTYPE: mcq_single" in msg
    assert "DECLARED_DIFFICULTY: 1" in msg
    assert "SOURCE: kaplan_2024" in msg
    assert "MARKED_CORRECT_LABEL: B" in msg
    assert "2+2=4" in msg


def test_render_reviewer_notes_mentions_verdict_and_panel():
    reports = [_report("opus", 5), _report("sonnet", 5), _report("gemini", 5)]
    v = aggregate_verdict(reports)
    notes = render_reviewer_notes(v)
    assert "LIVE" in notes
    assert "opus" in notes
    assert "sonnet" in notes


def test_embed_and_extract_review_round_trip():
    reports = [_report("opus", 5), _report("sonnet", 4), _report("gemini", 5)]
    v = aggregate_verdict(reports)
    wrapped = embed_review_in_explanation("Solving: 2x=4 → x=2.", v)
    assert "Solving: 2x=4" in wrapped
    assert "expert_review" in wrapped
    got = extract_review_from_explanation(wrapped)
    assert got is not None
    assert got["verdict"] == "live"


def test_embed_review_is_idempotent_on_replace():
    v1 = aggregate_verdict([_report("opus", 5), _report("sonnet", 5),
                            _report("gemini", 5)])
    v2 = aggregate_verdict([_report("opus", 3), _report("sonnet", 3),
                            _report("gemini", 3)])
    once = embed_review_in_explanation("Body.", v1)
    twice = embed_review_in_explanation(once, v2)
    # Only one block after re-embedding.
    assert twice.count("expert_review:") == 1
    got = extract_review_from_explanation(twice)
    assert got["verdict"] == "draft"


def test_kaplan_shim_reexports_public_api():
    """The deprecated ``expert_review_kaplan`` module should still resolve."""
    from services import expert_review_kaplan as er_k
    from services import expert_review as er

    assert er_k.RUBRIC_AXES is er.RUBRIC_AXES
    assert er_k.aggregate_verdict is er.aggregate_verdict
    assert er_k.build_review_user_message is er.build_review_user_message
    # The old module's ``expert_review`` entrypoint maps to the Kaplan
    # helper on the canonical module.
    assert er_k.expert_review is er.expert_review_kaplan
    assert er_k.JudgeReport is er.JudgeReport
    assert er_k.DEFAULT_PANEL is er.KAPLAN_DEFAULT_PANEL
