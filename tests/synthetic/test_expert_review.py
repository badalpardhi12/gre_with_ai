"""
Unit tests for `services.expert_review` — the final pre-promotion gate.

Tests use stub `JudgeCallable` panels so we never hit a real LLM. The
goal is to lock in the verdict logic:

- All-5s on every axis => 'live'.
- One axis below threshold from a single judge => still 'live' if the
  other two judges score >=4.
- Two judges below threshold on any axis => 'draft'.
- Spread > 2 on any axis => 'draft' even if the average is fine.
- Drafter alias is excluded from the panel when supplied.
- Defects from each judge are deduped and surfaced.
- Malformed JSON is parsed defensively (zeros) and triggers a retry.
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

from services.expert_review import (  # noqa: E402
    EXPERT_AXES,
    LIVE_AXIS_THRESHOLD,
    aggregate_expert_panel,
    expert_review,
    JudgeScores,
    parse_expert_response,
)


# ── Fixture: a sample question dict ──────────────────────────────────


def _sample_question(**overrides: Any) -> Dict[str, Any]:
    base = {
        "stem": "If 3x + 5 = 20, what is x?",
        "options": [
            {"label": "A", "text": "3"},
            {"label": "B", "text": "5", "is_correct": True},
            {"label": "C", "text": "7"},
            {"label": "D", "text": "10"},
        ],
        "correct_label": "B",
        "explanation": "Subtract 5 from both sides: 3x = 15, so x = 5.",
        "subtype": "mcq_single",
        "difficulty": 2,
        "source": "synthetic",
    }
    base.update(overrides)
    return base


def _make_panel(per_judge_scores: Dict[str, Dict[str, int]]) -> Dict[str, Any]:
    """Build a stub panel that returns canned JSON per judge alias."""
    panel: Dict[str, Any] = {}
    for label, axis_scores in per_judge_scores.items():
        payload = {
            "scores": {
                axis: {"score": axis_scores.get(axis, 5),
                       "justification": "stub"}
                for axis in EXPERT_AXES
            },
            "defects": [],
        }

        def _factory(p=payload):
            def _call(system: str, user: str) -> str:
                return json.dumps(p)
            return _call
        panel[label] = _factory()
    return panel


# ── Verdict logic ────────────────────────────────────────────────────


def test_all_fives_promotes_to_live():
    panel = _make_panel({
        "opus":   {a: 5 for a in EXPERT_AXES},
        "sonnet": {a: 5 for a in EXPERT_AXES},
        "gemini": {a: 5 for a in EXPERT_AXES},
    })
    result = expert_review(_sample_question(), panel=panel)
    assert result["verdict"] == "live"
    assert all(min(scores) == 5 for scores in result["scores"].values())


def test_one_axis_low_one_judge_still_live():
    # Two judges score >= 4 on every axis => promote.
    panel = _make_panel({
        "opus":   {a: 5 for a in EXPERT_AXES},
        "sonnet": {a: 4 for a in EXPERT_AXES},
        "gemini": {**{a: 5 for a in EXPERT_AXES}, "clarity": 3},
    })
    result = expert_review(_sample_question(), panel=panel)
    assert result["verdict"] == "live"


def test_two_judges_low_routes_to_draft():
    panel = _make_panel({
        "opus":   {a: 3 for a in EXPERT_AXES},          # all below threshold
        "sonnet": {**{a: 5 for a in EXPERT_AXES}, "correctness": 3},
        "gemini": {a: 5 for a in EXPERT_AXES},
    })
    result = expert_review(_sample_question(), panel=panel)
    # correctness has only one judge >= 4 => fail
    assert result["verdict"] == "draft"
    assert "correctness" in result["reviewer_notes"]


def test_spread_over_2_routes_to_draft():
    panel = _make_panel({
        "opus":   {a: 5 for a in EXPERT_AXES},
        "sonnet": {**{a: 5 for a in EXPERT_AXES}, "clarity": 2},  # spread = 3
        "gemini": {a: 5 for a in EXPERT_AXES},
    })
    result = expert_review(_sample_question(), panel=panel)
    assert result["verdict"] == "draft"
    assert "spread" in result["reviewer_notes"].lower() or "Panel disagreement" in result["reviewer_notes"]
    assert result["spread"]["clarity"] == 3


def test_low_threshold_axis_routes_to_draft():
    # Two judges at 3 on distractor_quality => fewer than 2 judges >=4 => fail.
    panel = _make_panel({
        "opus":   {**{a: 5 for a in EXPERT_AXES}, "distractor_quality": 3},
        "sonnet": {**{a: 5 for a in EXPERT_AXES}, "distractor_quality": 3},
        "gemini": {a: 5 for a in EXPERT_AXES},
    })
    result = expert_review(_sample_question(), panel=panel)
    assert result["verdict"] == "draft"
    assert "distractor_quality" in result["reviewer_notes"]


# ── Drafter exclusion ────────────────────────────────────────────────


def test_drafter_excluded_from_panel():
    # Build a panel keyed on aliases. Only 2 judges run because opus
    # was the drafter.
    captured: Dict[str, int] = {}

    def _make_call(label: str):
        def _call(system: str, user: str) -> str:
            captured[label] = captured.get(label, 0) + 1
            return json.dumps({
                "scores": {
                    a: {"score": 5, "justification": "ok"}
                    for a in EXPERT_AXES
                },
                "defects": [],
            })
        return _call

    panel = {
        "sonnet": _make_call("sonnet"),
        "gemini": _make_call("gemini"),
    }
    # Caller passed an already-trimmed panel; expert_review must use it
    # as-is (the trim already happened upstream).
    result = expert_review(_sample_question(), drafter_model="opus",
                           panel=panel)
    assert result["verdict"] == "live"
    assert set(captured.keys()) == {"sonnet", "gemini"}
    # Two judges, both scored >= threshold on all axes.
    assert all(len(scores) == 2 for scores in result["scores"].values())


# ── Defect aggregation ───────────────────────────────────────────────


def test_defects_collected_and_deduped():
    def _make_call(payload: Dict[str, Any]):
        def _call(system: str, user: str) -> str:
            return json.dumps(payload)
        return _call

    panel = {
        "opus": _make_call({
            "scores": {a: {"score": 5, "justification": ""}
                       for a in EXPERT_AXES},
            "defects": ["Stem could mention units", "Distractor D too obvious"],
        }),
        "sonnet": _make_call({
            "scores": {a: {"score": 5, "justification": ""}
                       for a in EXPERT_AXES},
            "defects": ["Distractor D too obvious", "Slight register drift"],
        }),
        "gemini": _make_call({
            "scores": {a: {"score": 5, "justification": ""}
                       for a in EXPERT_AXES},
            "defects": [],
        }),
    }
    result = expert_review(_sample_question(), panel=panel)
    assert result["verdict"] == "live"
    # Three unique defects, in the order they were first seen.
    assert result["defects"] == [
        "Stem could mention units",
        "Distractor D too obvious",
        "Slight register drift",
    ]


# ── Malformed JSON fallbacks ─────────────────────────────────────────


def test_malformed_json_yields_zeros():
    report = parse_expert_response("opus", "this is not json at all")
    for axis in EXPERT_AXES:
        assert report.scores[axis] == 0


def test_json_inside_prose_is_extracted():
    raw = (
        "Sure, here is my evaluation:\n"
        + json.dumps({
            "scores": {
                a: {"score": 4, "justification": "ok"}
                for a in EXPERT_AXES
            },
            "defects": [],
        })
        + "\nLet me know if you want more."
    )
    report = parse_expert_response("opus", raw)
    for axis in EXPERT_AXES:
        assert report.scores[axis] == 4


def test_retry_on_malformed_response():
    """If a judge returns junk first, we retry once with a cleaner prompt."""
    calls: List[int] = [0]

    def _flaky(system: str, user: str) -> str:
        calls[0] += 1
        if calls[0] == 1:
            return "blah blah no JSON here"
        return json.dumps({
            "scores": {a: {"score": 5, "justification": "ok"}
                       for a in EXPERT_AXES},
            "defects": [],
        })

    panel = {
        "opus": _flaky,
        "sonnet": _make_panel({"sonnet": {a: 5 for a in EXPERT_AXES}})["sonnet"],
        "gemini": _make_panel({"gemini": {a: 5 for a in EXPERT_AXES}})["gemini"],
    }
    result = expert_review(_sample_question(), panel=panel)
    assert calls[0] == 2  # one retry happened
    assert result["verdict"] == "live"


# ── Aggregation directly (no LLM) ────────────────────────────────────


def test_aggregate_means_and_spread():
    reports = [
        JudgeScores(judge="opus",   scores={a: 5 for a in EXPERT_AXES}),
        JudgeScores(judge="sonnet", scores={a: 4 for a in EXPERT_AXES}),
        JudgeScores(judge="gemini", scores={a: 4 for a in EXPERT_AXES}),
    ]
    result = aggregate_expert_panel(reports)
    assert result.verdict == "live"
    for axis in EXPERT_AXES:
        assert pytest.approx(result.means[axis]) == (5 + 4 + 4) / 3
        assert result.spread[axis] == 1


def test_zero_judges_returns_draft():
    result = aggregate_expert_panel([])
    assert result.verdict == "draft"
