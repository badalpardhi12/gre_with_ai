"""
Tests for the critic / reviser loop and the judge gate-decision rules.

Self-Refine (Madaan et al. 2023) → critic gives axis-localised notes
→ reviser applies smallest-edit revisions → loop caps at 2 cycles.
These tests exercise that loop without an LLM by stubbing both
clients.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from services.synthetic.llm_client import LLMClient, LLMResponse


# ── Stub LLM helpers ────────────────────────────────────────────────


class CannedJSONClient(LLMClient):
    """Returns pre-baked JSON payloads in sequence; cycles after exhausting."""

    def __init__(self, payloads: List[Dict[str, Any]], model_alias: str = "stub"):
        if not payloads:
            raise ValueError("need at least one payload")
        self.payloads = payloads
        self.model_alias = model_alias
        self.calls = 0

    def complete(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    def complete_json(self, *a, **kw):
        self.calls += 1
        payload = self.payloads[(self.calls - 1) % len(self.payloads)]
        return LLMResponse(text=json.dumps(payload), parsed_json=payload)


class StaticTextClient(LLMClient):
    def __init__(self, text: str, model_alias: str = "stub"):
        self.text = text
        self.model_alias = model_alias

    def complete(self, *a, **kw):  # pragma: no cover
        return LLMResponse(text=self.text)

    def complete_json(self, *a, **kw):
        try:
            payload = json.loads(self.text)
        except json.JSONDecodeError:
            return LLMResponse(text=self.text)
        return LLMResponse(text=self.text, parsed_json=payload)


# ── Critic ──────────────────────────────────────────────────────────


def test_critic_parses_localised_notes():
    from services.synthetic.critic import Critic
    payload = {
        "overall_assessment": "Two distractors collide on the same misconception.",
        "notes": [
            {
                "axis": "distractor_quality",
                "target": "options[B]",
                "rationale": "B repeats D's misconception",
                "edit": "Replace B with a wrong-valence near-synonym",
                "severity": "major",
            },
            {
                "axis": "language_clarity",
                "target": "stem cue",
                "rationale": "'although ... but' is redundant",
                "edit": "Drop one of the two contrast markers",
                "severity": "minor",
            },
        ],
    }
    critic = Critic(CannedJSONClient([payload]))
    review = critic.review("test-1", {"subtype": "tc", "stem": "x", "options": []})
    assert len(review.notes) == 2
    assert review.notes[0].axis == "distractor_quality"
    assert review.notes[0].target == "options[B]"
    assert "wrong-valence" in review.notes[0].edit
    assert review.has_blocking_notes is False
    assert "language_clarity" in review.axes_flagged


def test_critic_drops_unknown_axis_notes():
    from services.synthetic.critic import Critic
    payload = {
        "overall_assessment": "ok",
        "notes": [
            {"axis": "made_up_axis", "target": "stem", "rationale": "x",
             "edit": "y", "severity": "major"},
            {"axis": "content_validity", "target": "stem", "rationale": "z",
             "edit": "w", "severity": "minor"},
        ],
    }
    critic = Critic(CannedJSONClient([payload]))
    review = critic.review("test-2", {"subtype": "tc"})
    assert len(review.notes) == 1
    assert review.notes[0].axis == "content_validity"


def test_critic_handles_garbage_response():
    from services.synthetic.critic import Critic
    critic = Critic(StaticTextClient("not json at all"))
    review = critic.review("test-3", {"subtype": "tc"})
    assert review.notes == []


def test_critic_severity_promoted_by_judge_scores():
    """When a judge aggregate is supplied, critic notes whose axis is
    weak in the judge result get bumped to 'blocking'."""
    from services.synthetic.critic import Critic
    from services.synthetic.judge import JudgeAggregate

    payload = {
        "overall_assessment": "needs work",
        "notes": [{
            "axis": "distractor_quality", "target": "options[B]",
            "rationale": "repeat", "edit": "replace",
            "severity": "minor",  # critic said minor
        }],
    }
    judge_agg = JudgeAggregate(
        item_id="i", per_judge=[],
        medians={"distractor_quality": 1.5, "content_validity": 5.0,
                 "construct_alignment": 5.0, "difficulty_plausibility": 5.0,
                 "language_clarity": 5.0, "fairness_bias": 5.0},
        mean_overall=3.9, min_axis_median=1.5, failing_axes=["distractor_quality"],
    )
    critic = Critic(CannedJSONClient([payload]))
    review = critic.review("test-4", {"subtype": "tc"}, judge_aggregate=judge_agg)
    assert review.notes[0].severity == "blocking"


def test_critic_warns_on_same_model_as_drafter():
    import logging
    from services.synthetic.critic import Critic

    # Project logger has propagate=False, so caplog can't see records;
    # attach our own handler to capture WARNINGs from the critic logger.
    captured: List[str] = []

    class _Captor(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _Captor(level=logging.WARNING)
    logger = logging.getLogger("gre_app.synthetic.critic")
    logger.addHandler(handler)
    try:
        payload = {"overall_assessment": "ok", "notes": []}
        client = CannedJSONClient([payload], model_alias="opus")
        Critic(client, drafter_model_alias="opus")
    finally:
        logger.removeHandler(handler)
    assert any("self-preference" in m.lower() for m in captured), captured


# ── Reviser ─────────────────────────────────────────────────────────


def _sample_draft():
    from services.synthetic.types import DraftItem, DraftOption, Seed
    seed = Seed(measure="verbal", topic="text_completion",
                subtopic="tc_1_blank", subtype="tc",
                difficulty_target=3)
    options = [
        DraftOption(label="A", text="cogent", is_correct=False, misconception="filler"),
        DraftOption(label="B", text="lucid", is_correct=False, misconception="filler"),
        DraftOption(label="C", text="opaque", is_correct=True),
        DraftOption(label="D", text="vague", is_correct=False, misconception="filler"),
        DraftOption(label="E", text="brief", is_correct=False, misconception="filler"),
    ]
    return DraftItem(
        subtype="tc",
        stem="The argument was anything but ____, leaving the audience confused.",
        options=options,
        correct_label="C",
        explanation="Test stem.",
        difficulty_target=3,
        vocab_tier="advanced",
        seed=seed,
        prompt_hash="orig-hash",
    )


def test_reviser_idempotent_when_no_notes():
    from services.synthetic.critic import CriticReview
    from services.synthetic.reviser import Reviser
    draft = _sample_draft()
    review = CriticReview(item_id="x", notes=[])
    reviser = Reviser(StaticTextClient("{}"))
    revised = reviser.revise(draft, review)
    assert revised is draft  # no-op short-circuit
    # Verify no LLM call was made by checking the stem is identical
    assert revised.stem == draft.stem


def test_reviser_applies_a_single_edit():
    from services.synthetic.critic import CriticReview, CriticNote
    from services.synthetic.reviser import Reviser
    draft = _sample_draft()
    # Reviser returns a payload with one edited option.
    revised_payload = {
        "subtype": "tc",
        "stem": draft.stem,
        "options": [
            {"label": "A", "text": "cogent", "is_correct": False,
             "misconception": "near_synonym_missing_contrast"},
            {"label": "B", "text": "perspicuous", "is_correct": False,
             "misconception": "wrong_register"},
            {"label": "C", "text": "opaque", "is_correct": True,
             "misconception": ""},
            {"label": "D", "text": "vague", "is_correct": False,
             "misconception": "near_synonym_missing_contrast"},
            {"label": "E", "text": "brief", "is_correct": False,
             "misconception": "context_irrelevant_homonym"},
        ],
        "correct_label": "C",
        "explanation": "Updated.",
        "difficulty_target": 3,
        "vocab_tier": "advanced",
        "domain_assumptions": [],
        "expected_solve_steps": 1,
        "concept_tags": [],
    }
    review = CriticReview(item_id="x", notes=[
        CriticNote(axis="distractor_quality", target="options[B]",
                   rationale="filler", edit="replace with wrong_register"),
    ])
    reviser = Reviser(CannedJSONClient([revised_payload]))
    revised = reviser.revise(draft, review)
    assert revised is not draft
    assert revised.options[1].text == "perspicuous"
    assert revised.options[1].misconception == "wrong_register"
    assert revised.correct_label == "C"  # preserved


def test_reviser_rejects_when_correct_label_changes():
    """The reviser preserves correct_label even if the LLM forgets to."""
    from services.synthetic.critic import CriticReview, CriticNote
    from services.synthetic.reviser import Reviser
    draft = _sample_draft()
    # LLM accidentally moves correct_label to A.
    revised_payload = {
        "subtype": "tc",
        "stem": draft.stem,
        "options": [
            {"label": "A", "text": "cogent", "is_correct": False, "misconception": ""},
            {"label": "B", "text": "lucid", "is_correct": False, "misconception": ""},
            {"label": "C", "text": "opaque", "is_correct": True, "misconception": ""},
            {"label": "D", "text": "vague", "is_correct": False, "misconception": ""},
            {"label": "E", "text": "brief", "is_correct": False, "misconception": ""},
        ],
        "correct_label": "A",  # WRONG — should be preserved as C
        "explanation": "x",
        "difficulty_target": 3,
        "vocab_tier": "advanced",
        "domain_assumptions": [],
        "expected_solve_steps": 1,
        "concept_tags": [],
    }
    review = CriticReview(item_id="x", notes=[
        CriticNote(axis="distractor_quality", target="options[B]",
                   rationale="filler", edit="replace"),
    ])
    reviser = Reviser(CannedJSONClient([revised_payload]))
    revised = reviser.revise(draft, review)
    assert revised.correct_label == "C"  # forced back


def test_reviser_rejects_when_option_count_changes():
    from services.synthetic.critic import CriticReview, CriticNote
    from services.synthetic.reviser import Reviser
    draft = _sample_draft()
    # LLM dropped option E.
    revised_payload = {
        "subtype": "tc",
        "stem": draft.stem,
        "options": [
            {"label": "A", "text": "cogent", "is_correct": False, "misconception": ""},
            {"label": "B", "text": "perspicuous", "is_correct": False,
             "misconception": ""},
            {"label": "C", "text": "opaque", "is_correct": True, "misconception": ""},
            {"label": "D", "text": "vague", "is_correct": False, "misconception": ""},
        ],
        "correct_label": "C",
        "explanation": "x",
        "difficulty_target": 3,
        "vocab_tier": "advanced",
        "domain_assumptions": [],
        "expected_solve_steps": 1,
        "concept_tags": [],
    }
    review = CriticReview(item_id="x", notes=[
        CriticNote(axis="distractor_quality", target="options[B]",
                   rationale="filler", edit="replace"),
    ])
    reviser = Reviser(CannedJSONClient([revised_payload]))
    revised = reviser.revise(draft, review)
    # Option count change → discarded; original draft returned.
    assert len(revised.options) == 5
    assert revised is draft


def test_reviser_rejects_when_stem_drift_exceeds_max():
    """A revision that rewrites the entire stem is discarded."""
    from services.synthetic.critic import CriticReview, CriticNote
    from services.synthetic.reviser import Reviser
    draft = _sample_draft()
    revised_payload = {
        "subtype": "tc",
        "stem": "Completely rewritten stem with no overlap to the original prompt at all here.",
        "options": [
            {"label": o.label, "text": o.text, "is_correct": o.is_correct,
             "misconception": ""}
            for o in draft.options
        ],
        "correct_label": "C",
        "explanation": "x",
        "difficulty_target": 3,
        "vocab_tier": "advanced",
        "domain_assumptions": [],
        "expected_solve_steps": 1,
        "concept_tags": [],
    }
    review = CriticReview(item_id="x", notes=[
        CriticNote(axis="language_clarity", target="stem", rationale="x",
                   edit="x"),
    ])
    reviser = Reviser(CannedJSONClient([revised_payload]), max_stem_drift=0.30)
    revised = reviser.revise(draft, review)
    assert revised.stem == draft.stem  # discarded


def test_reviser_stem_drift_function():
    from services.synthetic.reviser import stem_drift
    assert stem_drift("hello", "hello") == 0.0
    assert stem_drift("hello", "hxllo") == 0.2          # one substitution / 5
    assert stem_drift("", "anything") == 1.0
    assert stem_drift("hello", "") == 1.0


# ── Calibration anchor loader ───────────────────────────────────────


def test_calibration_anchors_load_from_default_path():
    from services.synthetic.types import load_calibration_anchors
    anchors = load_calibration_anchors()
    assert len(anchors) == 6, "expected 3 gold + 3 bad anchor items"
    labels = {a.label for a in anchors}
    assert labels == {"GOLD-1", "GOLD-2", "GOLD-3", "BAD-1", "BAD-2", "BAD-3"}
    # Each anchor must specify expected scores for every axis.
    from services.synthetic.types import RUBRIC_AXES
    for a in anchors:
        for axis in RUBRIC_AXES:
            assert axis in a.expected_scores, \
                f"{a.label} missing expected score for {axis}"
            score = a.expected_scores[axis]
            assert 1 <= score <= 5, f"{a.label}.{axis} out of range: {score}"


def test_calibration_anchors_render_into_judge_prompt():
    """Anchors must appear in the user prompt verbatim so the judge sees them."""
    from services.synthetic.prompts.judge import build_judge_prompt
    from services.synthetic.types import (
        load_calibration_anchors, RUBRIC_AXIS_DESCRIPTIONS,
    )
    anchors = load_calibration_anchors()
    prompt = build_judge_prompt(
        {"subtype": "tc", "stem": "test"},
        RUBRIC_AXIS_DESCRIPTIONS,
        calibration_anchors=anchors,
    )
    assert "CALIBRATION ANCHORS" in prompt["user"]
    assert "GOLD-1" in prompt["user"]
    assert "BAD-3" in prompt["user"]
    # Behavioural-band descriptors must also appear.
    assert "[5]" in prompt["user"]
    assert "[3]" in prompt["user"]
    assert "[1]" in prompt["user"]


def test_judge_prompt_omits_anchor_block_when_none():
    from services.synthetic.prompts.judge import build_judge_prompt
    from services.synthetic.types import RUBRIC_AXIS_DESCRIPTIONS
    prompt = build_judge_prompt(
        {"subtype": "tc", "stem": "test"},
        RUBRIC_AXIS_DESCRIPTIONS,
        calibration_anchors=[],
    )
    assert "CALIBRATION ANCHORS" not in prompt["user"]


# ── Judge gate decisions ────────────────────────────────────────────


def _judge_aggregate(medians: Dict[str, float]):
    from services.synthetic.judge import aggregate_judges
    from services.synthetic.types import JudgeReport, JudgeAxisScore
    # Synthesise a fake single judge whose scores match the requested
    # medians; aggregate then has medians equal to those scores.
    report = JudgeReport(
        judge_name="stub", item_id="i",
        axes=[JudgeAxisScore(axis=a, score=int(round(s)))
              for a, s in medians.items()],
    )
    return aggregate_judges("i", [report])


def test_gate_decision_auto_promote():
    agg = _judge_aggregate({
        "content_validity": 5, "construct_alignment": 5,
        "difficulty_plausibility": 4, "distractor_quality": 5,
        "language_clarity": 5, "fairness_bias": 5,
    })
    assert agg.gate_decision() == "auto_promote"


def test_gate_decision_pass_to_sme():
    agg = _judge_aggregate({
        "content_validity": 4, "construct_alignment": 4,
        "difficulty_plausibility": 3, "distractor_quality": 4,
        "language_clarity": 4, "fairness_bias": 4,
    })
    # Mean = 23/6 = 3.83 → above 3.8, below 4.3 → pass_to_sme.
    assert agg.gate_decision() == "pass_to_sme"


def test_gate_decision_marginal_revise():
    agg = _judge_aggregate({
        "content_validity": 3, "construct_alignment": 3,
        "difficulty_plausibility": 3, "distractor_quality": 3,
        "language_clarity": 4, "fairness_bias": 4,
    })
    # Mean = 20/6 ≈ 3.33 → in [3.5? no, below 3.5] actually = 3.33
    # The only way to get marginal_revise is mean in [3.5, 3.8) and
    # all axes >= 3 — let's redo:
    agg = _judge_aggregate({
        "content_validity": 3, "construct_alignment": 4,
        "difficulty_plausibility": 3, "distractor_quality": 4,
        "language_clarity": 3, "fairness_bias": 4,
    })
    # Mean = 21/6 = 3.5 → marginal_revise, all axes >= 3.
    assert agg.mean_overall == pytest.approx(3.5, abs=0.01)
    assert agg.gate_decision() == "marginal_revise"


def test_gate_decision_reject_on_fairness():
    agg = _judge_aggregate({
        "content_validity": 5, "construct_alignment": 5,
        "difficulty_plausibility": 5, "distractor_quality": 5,
        "language_clarity": 5, "fairness_bias": 3,  # below hard floor
    })
    assert agg.gate_decision() == "reject"


def test_gate_decision_reject_on_low_mean():
    agg = _judge_aggregate({
        "content_validity": 1, "construct_alignment": 1,
        "difficulty_plausibility": 1, "distractor_quality": 1,
        "language_clarity": 1, "fairness_bias": 4,
    })
    assert agg.gate_decision() == "reject"


# ── Per-judge bias offsets ──────────────────────────────────────────


def test_apply_judge_offsets_clamps_to_band():
    from services.synthetic.judge import apply_judge_offsets
    from services.synthetic.types import JudgeReport, JudgeAxisScore
    r = JudgeReport(
        judge_name="lenient", item_id="i",
        axes=[JudgeAxisScore(axis="content_validity", score=5)],
    )
    # Offset of +2: subtract 2 from this judge's score.
    adjusted = apply_judge_offsets([r], {"lenient": {"content_validity": 2.0}})
    assert adjusted[0].axes[0].score == 3
    # Negative offset (judge under-scores) raises the score, clamped at 5.
    adjusted = apply_judge_offsets([r], {"lenient": {"content_validity": -3.0}})
    assert adjusted[0].axes[0].score == 5


def test_rubric_judge_passes_offsets_through_to_aggregate():
    from services.synthetic.judge import RubricJudge
    from tests.synthetic.test_synthetic_pipeline import CannedJudge
    # One lenient judge scoring 5 across the board.
    panel = {"lenient": CannedJudge({
        "content_validity": 5, "construct_alignment": 5,
        "difficulty_plausibility": 5, "distractor_quality": 5,
        "language_clarity": 5, "fairness_bias": 5,
    })}
    offsets = {"lenient": {a: 1.0 for a in (
        "content_validity", "construct_alignment",
        "difficulty_plausibility", "distractor_quality",
        "language_clarity", "fairness_bias",
    )}}
    rj = RubricJudge(panel, judge_offsets=offsets)
    agg = rj.grade("item-x", {"subtype": "tc"})
    # All scores were 5; offset of +1 means subtract 1 → all 4.
    for axis in agg.medians:
        assert agg.medians[axis] == 4.0
    # raw_per_judge keeps the un-adjusted scores for audit.
    assert agg.raw_per_judge
    assert agg.raw_per_judge[0].axes[0].score == 5


# ── No-self-grade enforcement ───────────────────────────────────────


def test_rubric_judge_rejects_drafter_in_panel():
    from services.synthetic.judge import RubricJudge
    from tests.synthetic.test_synthetic_pipeline import CannedJudge
    panel = {"j1": CannedJudge({"content_validity": 5})}
    panel["j1"].model_alias = "opus"  # type: ignore[attr-defined]
    with pytest.raises(ValueError) as ei:
        RubricJudge(panel, drafter_model_alias="opus")
    assert "no-self-grade" in str(ei.value).lower()


def test_rubric_judge_allows_different_alias():
    from services.synthetic.judge import RubricJudge
    from tests.synthetic.test_synthetic_pipeline import CannedJudge
    panel = {"j1": CannedJudge({"content_validity": 5})}
    panel["j1"].model_alias = "sonnet"  # type: ignore[attr-defined]
    # Should not raise.
    rj = RubricJudge(panel, drafter_model_alias="opus")
    assert rj is not None
