"""Regression tests for DI text-self-containment guidance.

The Data Interpretation (DI) drafter is required to embed every numeric
value the question depends on directly in the stem text, so that a human
reviewer (or a downstream judge that cannot see the chart image) can
solve the question from the stem alone. The chart asset is rendered
separately as a visual aid.

Failure mode this prevents: prior production batches lost DI cluster
members because rubric judges could not extract numbers from the chart
image and graded the items 'underspecified'. The fix is encoded in the
drafter prompt + per-cluster guidance.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.synthetic.prompts.drafter import (  # noqa: E402
    SUBTYPE_GUIDANCE,
    build_drafter_prompt,
)


def _has_self_containment_phrase(text: str) -> bool:
    text_lower = text.lower()
    # Any one of these phrasings is acceptable; we check that the
    # general intent ('every number must appear in the stem') is
    # present rather than pinning a specific sentence.
    candidate_phrases = (
        "every numeric value",
        "every number",
        "must also appear",
        "stem must restate",
        "restate every number",
        "stand on its own",
        "self-contain",
        "self contain",
    )
    return any(p in text_lower for p in candidate_phrases)


def test_data_interp_subtype_guidance_requires_text_self_containment():
    """The static SUBTYPE_GUIDANCE entry for `data_interp` must spell out
    the text-self-containment rule for the drafter."""
    guidance = SUBTYPE_GUIDANCE["data_interp"]
    assert _has_self_containment_phrase(guidance), (
        "data_interp guidance no longer reminds the drafter that the "
        "stem must contain every numeric value the question requires. "
        "This regresses the production-batch DI cluster fix."
    )
    # Also assert it explicitly mentions that the chart is a separate
    # rendered image (so the model understands WHY the constraint).
    g_lower = guidance.lower()
    assert ("rendered" in g_lower or "image" in g_lower
            or "visual aid" in g_lower), (
        "data_interp guidance should explain that the chart will be "
        "rendered as a separate image, motivating the self-containment "
        "constraint."
    )


def test_drafter_prompt_for_data_interp_includes_self_containment():
    """The composed system+user message for a DI seed must carry the
    self-containment rule into the LLM call."""
    prompt = build_drafter_prompt(
        subtype="data_interp",
        subtopic_display="Bar chart — sales over time",
        difficulty=3,
    )
    combined = prompt["system"] + "\n" + prompt["user"]
    assert _has_self_containment_phrase(combined), (
        "Composed drafter prompt for DI omits the text-self-containment "
        "instruction; the production DI cluster fix is no longer being "
        "delivered to the LLM."
    )


def test_di_cluster_extra_guidance_repeats_self_containment_rule():
    """The Phase-1 driver's per-seed `_drafter_guidance` for DI cluster
    members (both owner and consumer roles) must reinforce the
    self-containment rule. The prompt module's static guidance is
    shared across DI items, but cluster members get a richer per-role
    extra_guidance string from the driver — which must NOT silently
    drop the rule.
    """
    from scripts.run_synthetic_phase1 import _drafter_guidance
    from services.synthetic.types import Seed
    seed_owner = Seed(
        measure="quant",
        topic="data_analysis",
        subtopic="data_interpretation",
        subtype="data_interp",
        difficulty_target=3,
        extra={"cluster_role": "passage_owner",
                "cluster_id": "di_cluster_test"},
    )
    g_owner = _drafter_guidance(seed_owner, cluster_role="passage_owner")
    seed_consumer = Seed(
        measure="quant",
        topic="data_analysis",
        subtopic="data_interpretation",
        subtype="data_interp",
        difficulty_target=3,
        extra={"cluster_role": "passage_consumer",
                "cluster_id": "di_cluster_test"},
    )
    g_consumer = _drafter_guidance(seed_consumer,
                                    cluster_role="passage_consumer")
    for label, g in (("owner", g_owner), ("consumer", g_consumer)):
        assert _has_self_containment_phrase(g), (
            f"DI cluster {label} guidance no longer carries the "
            f"text-self-containment rule. Got: {g!r}"
        )
