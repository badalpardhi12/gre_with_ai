"""
Persist a survivor — stage (g).

Inserts a DraftItem (and its judge/solver/ambiguity audit) into the
Question table with `source='ai_synthetic'` and `status='draft'`. The
SME review queue then promotes draft → live (or retired). All inserts
happen inside one `db.atomic()` block per call; failures roll back so
we never leave half-inserted options.

We never overwrite existing `Question` rows here; if a hash collision
happens, the caller is expected to detect it earlier.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from models.database import (
    db, NumericAnswer, Question, QuestionOption, Stimulus,
)
from services.log import get_logger
from services.question_bank import SYNTHETIC_SOURCE
from services.synthetic.judge import JudgeAggregate
from services.synthetic.types import DraftItem

logger = get_logger("synthetic.persist")


def persist_draft(
    draft: DraftItem,
    *,
    run_id: str,
    judge_aggregate: Optional[JudgeAggregate] = None,
    solver_details: Optional[Dict[str, Any]] = None,
    ambiguity_details: Optional[Dict[str, Any]] = None,
    domain_details: Optional[Dict[str, Any]] = None,
    initial_status: str = "candidate",
) -> int:
    """Persist a survivor and return its new Question.id.

    `initial_status` defaults to 'candidate' (R5 lifecycle migration
    `_013_question_lifecycle_2026_05`). Items never auto-promote to
    'live' from the pipeline. The SME review screen flips
    candidate -> pretest, and only the IRT estimator promotes
    pretest -> live once enough response stats accumulate.
    """
    if not draft.seed:
        raise ValueError("draft.seed must be set before persist")
    seed = draft.seed
    provenance: Dict[str, Any] = {
        "run_id": run_id,
        "prompt_hash": draft.prompt_hash,
        "generated_at": draft.generated_at.isoformat() if draft.generated_at else None,
        "seed": {
            "measure": seed.measure,
            "topic": seed.topic,
            "subtopic": seed.subtopic,
            "subtype": seed.subtype,
            "difficulty_target": seed.difficulty_target,
        },
        "vocab_tier": draft.vocab_tier,
        "domain_assumptions": draft.domain_assumptions,
        "expected_solve_steps": draft.expected_solve_steps,
        "concept_tags": draft.concept_tags,
    }
    if judge_aggregate is not None:
        provenance["judge"] = {
            "medians": judge_aggregate.medians,
            "mean": judge_aggregate.mean_overall,
            "min_axis": judge_aggregate.min_axis_median,
            "failing_axes": judge_aggregate.failing_axes,
            "per_judge": [
                {
                    "name": r.judge_name,
                    "axes": {a.axis: a.score for a in r.axes},
                }
                for r in judge_aggregate.per_judge
            ],
        }
    if solver_details is not None:
        provenance["solver"] = solver_details
    if ambiguity_details is not None:
        provenance["ambiguity"] = ambiguity_details
    if domain_details is not None:
        provenance["domain"] = domain_details

    quality_score = (
        judge_aggregate.mean_overall / 5.0 if judge_aggregate else None
    )

    with db.atomic():
        stim_row = None
        if draft.stimulus:
            # Drafters occasionally use alternate key names for passage
            # text (passage / text / body) instead of the schema's
            # `content`. Normalise so the persisted row always has the
            # passage in `content` for downstream rendering.
            content = (
                draft.stimulus.get("content")
                or draft.stimulus.get("passage")
                or draft.stimulus.get("text")
                or draft.stimulus.get("body")
                or ""
            )
            stim_row = Stimulus.create(
                stimulus_type=draft.stimulus.get("type", "passage"),
                title=draft.stimulus.get("title", ""),
                content=content,
                render_spec=json.dumps(draft.stimulus.get("render_spec") or {}),
            )
        q = Question.create(
            measure=seed.measure,
            subtype=draft.subtype,
            stimulus=stim_row,
            prompt=draft.stem,
            difficulty_target=draft.difficulty_target,
            time_target_seconds=_default_time_target(draft.subtype),
            concept_tags=json.dumps(draft.concept_tags),
            topic=seed.topic,
            subtopic=seed.subtopic,
            source=SYNTHETIC_SOURCE,
            quality_score=quality_score,
            provenance="llm_generated",
            status=initial_status,
            explanation=draft.explanation,
            provenance_json=json.dumps(provenance),
            review_notes="",
            generated_at=draft.generated_at or datetime.now(),
            run_id=run_id,
        )
        for opt in draft.options:
            QuestionOption.create(
                question=q,
                option_label=opt.label,
                option_text=opt.text,
                is_correct=opt.is_correct,
            )
        if draft.subtype == "numeric_entry":
            NumericAnswer.create(
                question=q,
                exact_value=draft.correct_value,
                numerator=draft.numerator,
                denominator=draft.denominator,
                tolerance=draft.tolerance if draft.tolerance is not None else 0.001,
                mode=("fraction" if draft.numerator is not None else "decimal"),
            )
        logger.info(
            "persisted synthetic draft %d (run=%s subtype=%s)",
            q.id, run_id, draft.subtype,
        )
        return q.id


def _default_time_target(subtype: str) -> int:
    return {
        "tc": 75,
        "se": 60,
        "rc_single": 90,
        "rc_multi": 120,
        "rc_select_passage": 90,
        "qc": 75,
        "mcq_single": 90,
        "mcq_multi": 120,
        "numeric_entry": 90,
        "data_interp": 120,
    }.get(subtype, 90)
