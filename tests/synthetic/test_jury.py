"""
Tests for the R4 multi-judge orchestration:
- Triage judge filters obvious junk before the senior jury runs.
- make_jury composition refuses to seat the drafter family.
- Option-order shuffling produces deterministic permutations and
  remaps correct_label correctly.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from services.synthetic.llm_client import LLMClient, LLMResponse


class _FixedScoreJudge(LLMClient):
    def __init__(self, scores: Dict[str, int], model_alias: str = "stub"):
        self.scores = scores
        self.model_alias = model_alias

    def complete(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    def complete_json(self, *a, **kw):
        payload = {"scores": {a: {"score": s, "justification": ""}
                              for a, s in self.scores.items()}}
        return LLMResponse(text=json.dumps(payload), parsed_json=payload)


# ── Triage judge ────────────────────────────────────────────────────


def test_triage_passes_high_quality_item():
    from services.synthetic.judge import TriageJudge
    triage = TriageJudge(_FixedScoreJudge({
        "content_validity": 5, "construct_alignment": 5,
        "difficulty_plausibility": 5, "distractor_quality": 5,
        "language_clarity": 5, "fairness_bias": 5,
    }))
    out = triage.screen("test-1", {"subtype": "tc", "stem": "x"})
    assert out.proceed_to_jury is True
    assert out.aggregate.mean_overall == 5.0


def test_triage_blocks_obvious_junk():
    from services.synthetic.judge import TriageJudge
    # Mean = 1.5, axis floor 1 — clearly junk.
    triage = TriageJudge(_FixedScoreJudge({
        "content_validity": 1, "construct_alignment": 1,
        "difficulty_plausibility": 2, "distractor_quality": 1,
        "language_clarity": 2, "fairness_bias": 2,
    }))
    out = triage.screen("test-2", {"subtype": "tc", "stem": "x"})
    assert out.proceed_to_jury is False
    assert "triage_reject" in out.reason


def test_triage_passes_marginal_item():
    """Triage should be lenient — anything not obviously junk proceeds."""
    from services.synthetic.judge import TriageJudge
    # Mean = 3.0, all axes >= 2 — should proceed even though the senior
    # jury would later reject it.
    triage = TriageJudge(_FixedScoreJudge({
        "content_validity": 3, "construct_alignment": 3,
        "difficulty_plausibility": 3, "distractor_quality": 3,
        "language_clarity": 3, "fairness_bias": 3,
    }))
    out = triage.screen("test-3", {"subtype": "tc", "stem": "x"})
    assert out.proceed_to_jury is True


# ── Jury composition ────────────────────────────────────────────────


def test_make_jury_excludes_drafter_alias():
    """When the drafter is opus, the jury should pick sonnet + gemini-pro."""
    from services.synthetic.judge import make_jury
    from services.synthetic.llm_client import (
        LLMClientFactory, register_backend,
    )

    # Stub backend that yields per-role _FixedScoreJudges.
    def _factory(role, model, **kw):
        client = _FixedScoreJudge({"content_validity": 5}, model_alias=model)
        return client

    register_backend("test-stub", _factory)
    factory = LLMClientFactory(
        backend="test-stub",
        roles={
            "judge_a": {"model": "sonnet", "temperature": 0.1},
            "judge_b": {"model": "gemini-pro", "temperature": 0.1},
            "judge_c": {"model": "opus", "temperature": 0.1},  # would self-grade
        },
    )
    panel = make_jury(
        factory,
        drafter_model_alias="opus",
        jury_size=2,
        preferred_models=("opus", "sonnet", "gemini-pro"),
    )
    aliases = {client.model_alias for client in panel.values()}
    assert "opus" not in aliases
    assert aliases == {"sonnet", "gemini-pro"}


def test_make_jury_raises_when_too_few_candidates():
    from services.synthetic.judge import make_jury
    from services.synthetic.llm_client import (
        LLMClientFactory, register_backend,
    )

    def _factory(role, model, **kw):
        return _FixedScoreJudge({"content_validity": 5}, model_alias=model)

    register_backend("test-stub-2", _factory)
    factory = LLMClientFactory(
        backend="test-stub-2",
        roles={"judge_a": {"model": "opus"}, "judge_b": {"model": "sonnet"}},
    )
    with pytest.raises(ValueError) as ei:
        make_jury(
            factory,
            drafter_model_alias="opus",
            jury_size=3,
            preferred_models=("opus", "sonnet"),
        )
    assert "candidates" in str(ei.value)


# ── Option-order shuffling ──────────────────────────────────────────


def _five_option_payload():
    return {
        "subtype": "tc",
        "stem": "Test stem with five options.",
        "options": [
            {"label": "A", "text": "alpha", "is_correct": False, "misconception": "x"},
            {"label": "B", "text": "beta", "is_correct": False, "misconception": "y"},
            {"label": "C", "text": "gamma", "is_correct": True, "misconception": ""},
            {"label": "D", "text": "delta", "is_correct": False, "misconception": "z"},
            {"label": "E", "text": "epsilon", "is_correct": False, "misconception": "w"},
        ],
        "correct_label": "C",
    }


def test_shuffle_payload_options_remaps_correct_label():
    from services.synthetic.prompts.judge import shuffle_payload_options
    payload = _five_option_payload()
    shuffled = shuffle_payload_options(payload, seed="seed-1")
    # The texts are preserved; their letters change.
    text_to_new_label = {o["text"]: o["label"] for o in shuffled["options"]}
    assert text_to_new_label["gamma"] == shuffled["correct_label"]


def test_shuffle_payload_options_is_deterministic_per_seed():
    from services.synthetic.prompts.judge import shuffle_payload_options
    payload = _five_option_payload()
    a = shuffle_payload_options(payload, seed="seed-A")
    b = shuffle_payload_options(payload, seed="seed-A")
    assert [o["label"] + o["text"] for o in a["options"]] == \
           [o["label"] + o["text"] for o in b["options"]]


def test_shuffle_payload_options_differs_across_seeds():
    """Two distinct seeds should usually produce different orderings."""
    from services.synthetic.prompts.judge import shuffle_payload_options
    payload = _five_option_payload()
    a = shuffle_payload_options(payload, seed="seed-A")
    b = shuffle_payload_options(payload, seed="seed-B")
    a_order = [o["text"] for o in a["options"]]
    b_order = [o["text"] for o in b["options"]]
    # 5! = 120 permutations; collision probability is low. Assert that
    # AT LEAST one position differs.
    assert a_order != b_order


def test_shuffle_payload_options_no_options_passthrough():
    """Numeric-entry payloads (no options) are returned as-is."""
    from services.synthetic.prompts.judge import shuffle_payload_options
    payload = {"subtype": "numeric_entry", "stem": "x", "correct_value": 42}
    shuffled = shuffle_payload_options(payload, seed="x")
    assert shuffled == payload
    # Don't mutate original.
    assert payload == {"subtype": "numeric_entry", "stem": "x", "correct_value": 42}


def test_judge_prompt_uses_shuffle_when_requested():
    from services.synthetic.prompts.judge import build_judge_prompt
    from services.synthetic.types import RUBRIC_AXIS_DESCRIPTIONS
    payload = _five_option_payload()
    prompt = build_judge_prompt(
        payload, RUBRIC_AXIS_DESCRIPTIONS,
        calibration_anchors=[],
        shuffle_options=True, shuffle_seed="judge_a::item-x",
    )
    # The original options had A=alpha; after shuffle the user prompt
    # should reflect a different mapping. Search for "alpha" and verify
    # it's not preceded by a literal `"label": "A"` block.
    user = prompt["user"]
    # Find both label-blocks for alpha. The text "alpha" appears once.
    assert '"alpha"' in user
    # If shuffled, A=gamma (the most likely permutation under sha256
    # ordering with this seed) — in any case, A is no longer alpha
    # *deterministically*, but we can't assert which letter alpha got.
    # Stronger check: build the same prompt unshuffled and confirm
    # they differ.
    plain = build_judge_prompt(
        payload, RUBRIC_AXIS_DESCRIPTIONS,
        calibration_anchors=[],
        shuffle_options=False,
    )
    assert plain["user"] != prompt["user"]


def test_rubric_judge_with_shuffle_grades_consistently():
    """Shuffled options shouldn't change the *aggregate* score for a
    judge stub that returns the same scores regardless of input."""
    from services.synthetic.judge import RubricJudge
    judge = _FixedScoreJudge({
        "content_validity": 5, "construct_alignment": 5,
        "difficulty_plausibility": 5, "distractor_quality": 5,
        "language_clarity": 5, "fairness_bias": 5,
    })
    rj = RubricJudge({"j1": judge}, shuffle_options=True)
    agg = rj.grade("test-shuffle", _five_option_payload())
    assert agg.mean_overall == 5.0
