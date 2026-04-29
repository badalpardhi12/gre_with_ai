"""
Synthetic GRE question generation pipeline (Phase 0+).

A multi-stage gauntlet that turns subtopic seeds into vetted draft items:

    seed -> generator -> adversarial solver -> ambiguity probe ->
            rubric judge -> domain checks -> persist (status='draft') ->
            human review

Phase 0 ships only the rubric grader and the toggle plumbing — no items
are generated yet. The pipeline modules below are scaffolding stubs that
implement the abstract interfaces; later phases plug in concrete LLM
backends.
"""

from services.synthetic.types import (
    DraftItem,
    DraftOption,
    JudgeAxisScore,
    JudgeReport,
    PipelineResult,
    PipelineStage,
    Seed,
)

__all__ = [
    "DraftItem",
    "DraftOption",
    "JudgeAxisScore",
    "JudgeReport",
    "PipelineResult",
    "PipelineStage",
    "Seed",
]
