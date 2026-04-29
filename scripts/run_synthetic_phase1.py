"""
Phase-1 batch driver for the synthetic-question pipeline.

Usage:
    venv/bin/python scripts/run_synthetic_phase1.py \\
        --total 40 --quant 20 --verbal 20 \\
        --difficulty-mix 30:40:30 \\
        --run-id phase1-2026-04-23 \\
        --out synthetic_sample_review.md

What it does:
    1. Builds a coverage matrix of seeds (subtype × subtopic × difficulty
       × persona × scenario) using DiversitySampler. We override the
       sampler for a small "explicit coverage" mode: for the 40-item
       sample run we hand-pick the subtopics so every band is hit.
    2. For each seed, calls the full SyntheticPipeline:
       drafter -> critic -> reviser (<=2 rounds) -> triage judge ->
       senior jury (Sonnet + Gemini-Pro, no Opus self-grade) ->
       adversarial solver -> ambiguity probe -> domain checks.
    3. For seeds that need a figure (geometry / DI), the drafter is
       prompted to emit a `figure_spec` / `chart_spec`. If it does, we
       render it via the figure generators and attach the asset path to
       the draft's `stimulus` slot. If it doesn't, we synthesise a
       minimal default spec from the seed.
    4. RC clusters: a single passage is drafted once and reused across
       the cluster's questions. DI clusters likewise share a chart.
    5. Persists survivors at `status='candidate'` (R5 lifecycle) with
       full provenance JSON.
    6. Writes per-run JSONL audit logs to data/synthetic/runs/<run_id>/.
    7. Calls render_sample_md to produce the human-review markdown.

Concurrency: items run sequentially by default. Pass `--max-parallel N`
to run N items concurrently via threads (the gateway client is
thread-safe; rate limiting is handled by the gateway itself).

The driver is deliberately verbose on stderr so the operator can spot
stalls. JSONL audit captures every per-stage decision.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import random
import shutil
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from models.database import (  # noqa: E402
    NumericAnswer, Question, QuestionOption, Stimulus,
    SyntheticGenerationRun, db, init_db,
)
from models.taxonomy import (  # noqa: E402
    QUANT_TAXONOMY, VERBAL_TAXONOMY, subtopic_display_name,
)
from services.log import get_logger  # noqa: E402
from services.synthetic.ambiguity import AmbiguityChecker  # noqa: E402
from services.synthetic.critic import Critic  # noqa: E402
from services.synthetic.dedup import (  # noqa: E402
    EmbeddingDeduper, JaccardDeduper, make_default_deduper,
)
from services.synthetic.domain_checks import (  # noqa: E402
    DEFAULT_REGISTRY, run_checks,
)
from services.synthetic.figures import (  # noqa: E402
    render_data_interp, render_geometry,
)
from services.synthetic.generator import Generator  # noqa: E402
from services.synthetic.judge import (  # noqa: E402
    RubricJudge, TriageJudge, make_jury,
)
from services.synthetic.llm_client import (  # noqa: E402
    LLMClientFactory,
)
from services.synthetic.persist import persist_draft  # noqa: E402
from services.synthetic.pipeline import (  # noqa: E402
    SyntheticPipeline,
)
from services.synthetic.reviser import Reviser  # noqa: E402
from services.synthetic.seeder import DiversitySampler  # noqa: E402
from services.synthetic.solver import (  # noqa: E402
    AdversarialSolver, apply_se_key_swap, answers_match, reconcile_se_key,
)
from services.synthetic.types import (  # noqa: E402
    DraftItem, DraftOption, RUBRIC_AXES, Seed,
)
from services.expert_review import expert_review  # noqa: E402

logger = get_logger("synthetic.run_phase1")


# ── Coverage plan for the Phase-1 sample run ──────────────────────────


# Quant: 20 items hitting every requested subtopic and subtype.
# Format: (subtype, topic, subtopic, difficulty)
QUANT_COVERAGE: List[Tuple[str, str, str, int]] = [
    # Arithmetic — 4 items
    ("mcq_single", "arithmetic", "ratios_proportions", 2),
    ("mcq_single", "arithmetic", "percents", 3),
    ("qc",         "arithmetic", "exponents_roots", 4),
    ("mcq_single", "arithmetic", "integers_number_properties", 3),
    # Algebra — 4 items
    ("mcq_single", "algebra", "linear_equations_systems", 2),
    ("qc",         "algebra", "quadratics", 4),
    ("mcq_single", "algebra", "inequalities", 3),
    ("mcq_single", "algebra", "functions", 4),
    # Geometry — 4 items, each with a programmatic SVG
    ("mcq_single", "geometry", "triangles", 3),
    ("mcq_single", "geometry", "circles", 3),
    ("qc",         "geometry", "coordinate_geometry", 4),
    ("mcq_multi",  "geometry", "quadrilaterals_polygons", 4),
    # Word problems — 3 items
    ("mcq_single", "algebra", "word_problems", 2),    # rate
    ("mcq_single", "algebra", "word_problems", 3),    # work
    ("mcq_single", "algebra", "word_problems", 4),    # mixture
    # Data analysis — 2 items
    ("numeric_entry", "data_analysis", "descriptive_stats", 3),
    ("mcq_multi",     "data_analysis", "probability", 4),
    # DI cluster — 3 questions sharing one matplotlib chart
    ("data_interp", "data_analysis", "data_interpretation", 2),
    ("data_interp", "data_analysis", "data_interpretation", 3),
    ("data_interp", "data_analysis", "data_interpretation", 4),
]

# Verbal: 20 items
VERBAL_COVERAGE: List[Tuple[str, str, str, int]] = [
    # TC ×6
    ("tc", "text_completion", "tc_1_blank", 2),
    ("tc", "text_completion", "tc_1_blank", 3),
    ("tc", "text_completion", "tc_2_blank", 3),
    ("tc", "text_completion", "tc_2_blank", 4),
    ("tc", "text_completion", "tc_3_blank", 3),
    ("tc", "text_completion", "tc_3_blank", 4),
    # SE ×4
    ("se", "sentence_equivalence", "se_synonyms", 2),
    ("se", "sentence_equivalence", "se_synonyms", 3),
    ("se", "sentence_equivalence", "se_contrast", 3),
    ("se", "sentence_equivalence", "se_contrast", 4),
    # RC short-passage cluster #1 — 1 passage + 2 questions
    ("rc_single", "reading_comprehension", "rc_main_idea", 2),
    ("rc_single", "reading_comprehension", "rc_inference", 3),
    # RC short-passage cluster #2 — 1 passage + 2 questions
    ("rc_single", "reading_comprehension", "rc_detail", 3),
    ("rc_single", "reading_comprehension", "rc_tone_attitude", 4),
    # RC long-passage cluster — 1 passage + 3 questions
    ("rc_single", "reading_comprehension", "rc_main_idea", 4),
    ("rc_single", "reading_comprehension", "rc_inference", 4),
    ("rc_single", "reading_comprehension", "rc_structure_function", 3),
    # RC argument-style (CR-flavoured) — 1 question
    ("rc_single", "critical_reasoning", "cr_assumption", 4),
    # 18 so far; pad with 2 more SE/TC items so we hit exactly 20.
    ("se", "sentence_equivalence", "se_synonyms", 4),
    ("tc", "text_completion", "tc_1_blank", 4),
]


# RC cluster IDs (for grouping in the markdown):
RC_CLUSTERS = {
    "rc_short_a": [10, 11],         # zero-indexed into VERBAL_COVERAGE
    "rc_short_b": [12, 13],
    "rc_long":    [14, 15, 16],
}
DI_CLUSTER_INDICES = [17, 18, 19]   # zero-indexed into QUANT_COVERAGE


# Geometry seeds need a figure_spec template that the drafter can fill in.
# We pre-generate a minimal default so the pipeline never crashes on
# missing geometry params.
GEOMETRY_FALLBACK_SPECS = {
    "triangles": {
        "kind": "triangle",
        "params": {"kind": "right", "right_angle_at": "A",
                    "side_labels": {"AB": "5", "BC": "12", "AC": "13"}},
        "caption": "Figure not drawn to scale.",
    },
    "circles": {
        "kind": "circle",
        "params": {"radius_label": "r", "show_chord": {
            "angle1_deg": 30, "angle2_deg": 150, "label": "chord"}},
        "caption": "Figure not drawn to scale.",
    },
    "coordinate_geometry": {
        "kind": "coordinate",
        "params": {"x_min": -4, "x_max": 6, "y_min": -3, "y_max": 6,
                    "line": {"slope": 1, "intercept": 0},
                    "points": [
                        {"x": 2, "y": 2, "label": "P"},
                        {"x": -1, "y": -1, "label": "Q"},
                    ]},
        "caption": "Figure not drawn to scale.",
    },
    "quadrilaterals_polygons": {
        "kind": "polygon",
        "params": {"n_sides": 6, "regular": True,
                    "interior_angle_label": "120°"},
        "caption": "Regular hexagon.",
    },
    "solids_3d": {
        "kind": "wireframe",
        "params": {"shape": "box",
                    "edge_labels": {"width": "5", "height": "4", "depth": "3"}},
        "caption": "Rectangular solid.",
    },
}


# ── LLM factory wiring ────────────────────────────────────────────────


# Drafter on Opus; reviser also on Opus (voice consistency).
# Critic on Sonnet (different family).
# Triage judge on Haiku (fast filter).
# Senior jury: Sonnet + Gemini-Pro (no Opus self-grade).
# Solvers: Sonnet + Gemini-Pro.
# Ambiguity probe: Sonnet.
PHASE1_ROLES = {
    "drafter":    {"model": "opus",       "temperature": 1.0, "max_tokens": 4000},
    "reviser":    {"model": "opus",       "temperature": 0.4, "max_tokens": 4000},
    "critic":     {"model": "sonnet",     "temperature": 0.3, "max_tokens": 2400},
    "triage":     {"model": "haiku",      "temperature": 0.1, "max_tokens": 1500},
    # Three-judge senior jury — all Anthropic. Two Sonnet readings at
    # different temperatures (0.1 / 0.25) plus one Haiku reading. The
    # no-self-grade rule (refinement plan §8) only forbids the
    # drafter's model alias from appearing in the jury, and the drafter
    # is Opus, so Sonnet+Sonnet+Haiku is allowed and avoids the
    # Gemini-Pro JSON-mode unreliability we hit on the first attempt
    # (40% truncated/non-JSON responses).
    "judge_a":    {"model": "sonnet",     "temperature": 0.1, "max_tokens": 2400},
    "judge_b":    {"model": "sonnet",     "temperature": 0.25, "max_tokens": 2400},
    "judge_c":    {"model": "haiku",      "temperature": 0.1, "max_tokens": 2400},
    "solver_a":   {"model": "sonnet",     "temperature": 0.2, "max_tokens": 2000},
    "solver_b":   {"model": "haiku",      "temperature": 0.2, "max_tokens": 2000},
    "ambiguity":  {"model": "sonnet",     "temperature": 0.2, "max_tokens": 1000},
    # Expert-review jury (Step 3 — final pre-promotion gate). Opus is
    # excluded automatically when drafter_model='opus'. Sonnet + Haiku +
    # Gemini-Pro provides 2-3 surviving judges depending on the drafter.
    "expert_opus":       {"model": "opus",       "temperature": 0.1, "max_tokens": 1500},
    "expert_sonnet":     {"model": "sonnet",     "temperature": 0.1, "max_tokens": 1500},
    "expert_haiku":      {"model": "haiku",      "temperature": 0.1, "max_tokens": 1500},
    "expert_gemini-pro": {"model": "gemini-pro", "temperature": 0.1, "max_tokens": 1500},
}


# Expert-review panel aliases. Opus is the drafter so it's filtered out
# automatically by `expert_review`. Sonnet + Haiku + Gemini-Pro gives us
# three independent readings, two of which are Anthropic-side. Gemini's
# JSON-mode unreliability is mitigated by the module's parse-and-retry.
EXPERT_PANEL_ALIASES = ("opus", "sonnet", "haiku", "gemini-pro")


def _register_local_backend() -> None:
    """Side-effect import of the local-only LLM adapter."""
    try:
        from services.synthetic import _llm_adapter  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            f"local LLM adapter not available: {exc}. "
            "Ensure services/synthetic/_llm_adapter.py exists."
        )


def _build_factory() -> LLMClientFactory:
    return LLMClientFactory(backend="local", roles=PHASE1_ROLES)


# Module-level factory + expert-review helpers. Set in `main()` after
# `_register_local_backend()`.
_EXPERT_FACTORY: Optional[LLMClientFactory] = None


def _build_expert_payload(draft: DraftItem,
                           q_id: Optional[int] = None) -> Dict[str, Any]:
    """Translate a `DraftItem` into the dict the expert-review module wants."""
    options = []
    for o in draft.options:
        options.append({
            "label": o.label, "text": o.text,
            "is_correct": bool(o.is_correct),
        })
    payload: Dict[str, Any] = {
        "stem": draft.stem,
        "options": options,
        "correct_label": draft.correct_label,
        "explanation": draft.explanation,
        "subtype": draft.subtype,
        "difficulty": draft.difficulty_target,
        "source": "ai_synthetic",
    }
    if draft.stimulus:
        payload["stimulus"] = draft.stimulus
    if q_id is not None:
        payload["qid"] = q_id
    return payload


def _update_after_expert_review(qid: int,
                                 expert_result: Dict[str, Any]) -> None:
    """Append expert-review block to provenance and update lifecycle status.

    'live' verdict promotes the row to status='live' (the lifecycle
    migration `_013_*` accepts that transition for synthetic items
    once the additional review-gate has cleared). 'draft' verdict
    downgrades to status='draft' so the SME queue can inspect it.
    """
    q = Question.get_or_none(Question.id == qid)
    if not q:
        return
    try:
        prov = json.loads(q.provenance_json or "{}")
    except (ValueError, TypeError):
        prov = {}
    prov["expert_review"] = expert_result
    q.provenance_json = json.dumps(prov)
    if expert_result.get("verdict") == "live":
        q.status = "live"
    else:
        q.status = "draft"
        notes = q.review_notes or ""
        addition = (
            f"[expert-review draft] "
            + (expert_result.get("reviewer_notes") or "")
        )
        q.review_notes = (notes + ("\n" if notes else "") + addition).strip()
    q.save()


# ── Cluster atomicity helper (Step 2) ────────────────────────────────


def _enforce_cluster_atomicity(
    results: List[Dict[str, Any]],
    audit: "JsonlAudit",
) -> None:
    """Mark a whole cluster 'draft' if any member failed to make it live.

    Treat DI clusters and RC clusters as atomic: a partial cluster is
    user-visible-broken (the chart/passage exists for some questions but
    not others). For a partial cluster:

    1. If any member has `expert_review.verdict='draft'`, downgrade ALL
       members to draft.
    2. If any member never persisted (drafter_failed, judge_reject,
       solver_disagreement, ambiguity_reject), downgrade ALL persisted
       members of that cluster to draft so the SME queue catches the
       inconsistency.

    Idempotent: calling twice is a no-op.
    """
    if not results:
        return
    by_cluster: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        cid = r.get("cluster_id")
        if not cid:
            continue
        by_cluster.setdefault(cid, []).append(r)
    for cid, recs in by_cluster.items():
        # Did anyone in the cluster fail to persist?
        any_unpersisted = any(not r.get("persisted") for r in recs)
        # Did anyone get expert_review='draft'?
        any_draft = any(
            (r.get("expert_review") or {}).get("verdict") == "draft"
            for r in recs
        )
        if not (any_unpersisted or any_draft):
            continue
        # Downgrade every persisted member to status='draft'.
        for r in recs:
            if not r.get("persisted") or not r.get("qid"):
                continue
            q = Question.get_or_none(Question.id == r["qid"])
            if not q:
                continue
            if q.status == "draft":
                continue
            q.status = "draft"
            note = (
                f"[cluster-atomicity] cluster `{cid}` had a member "
                f"that did not promote; downgrading the whole cluster."
            )
            q.review_notes = ((q.review_notes or "") + "\n" + note).strip()
            q.save()
            r["expert_review"] = r.get("expert_review") or {}
            if isinstance(r["expert_review"], dict):
                # Reflect the downgrade in the result dict.
                r["expert_review"]["verdict"] = "draft"
                r["expert_review"]["reviewer_notes"] = (
                    (r["expert_review"].get("reviewer_notes") or "")
                    + " ; cluster atomicity downgrade"
                ).strip()
            audit.emit("cluster_atomicity_downgrade",
                        cluster_id=cid, qid=r["qid"])


# ── Seed expansion ────────────────────────────────────────────────────


def _expand_coverage_to_seeds(
    coverage: List[Tuple[str, str, str, int]],
    measure: str,
    *,
    rng: random.Random,
) -> List[Seed]:
    """Expand the coverage tuples into Seed objects with persona + scenario."""
    seeds: List[Seed] = []
    quant_personas = (
        "lab_experiment", "manufacturing", "agriculture",
        "population_demographics", "weather_climate_neutral",
        "abstract_word",
    )
    verbal_personas = (
        "academic_neutral", "journalistic", "scientific_textbook",
        "policy_brief", "historical_essay",
    )
    quant_scenarios = (
        "abstract", "ratio_word", "geometry_concrete",
        "real_world_data", "lab_experiment", "agriculture",
    )
    verbal_scenarios = (
        "humanities", "biological_sciences", "physical_sciences",
        "social_sciences", "everyday",
    )
    personas = quant_personas if measure == "quant" else verbal_personas
    scenarios = quant_scenarios if measure == "quant" else verbal_scenarios
    for subtype, topic, subtopic, difficulty in coverage:
        seeds.append(Seed(
            measure=measure,
            topic=topic,
            subtopic=subtopic,
            subtype=subtype,
            difficulty_target=int(difficulty),
            extra={
                "scenario_class": rng.choice(scenarios),
                "persona": rng.choice(personas),
                "structural_frame": "default",
            },
        ))
    return seeds


# ── Per-seed extra drafter guidance ──────────────────────────────────


def _drafter_guidance(seed: Seed, cluster_role: Optional[str] = None) -> str:
    """Short subtopic-specific hint plus persona / scenario flavour."""
    persona = (seed.extra or {}).get("persona", "")
    scenario = (seed.extra or {}).get("scenario_class", "")
    bits: List[str] = []
    if persona:
        bits.append(f"Voice / persona: {persona}.")
    if scenario:
        bits.append(f"Scenario class: {scenario}.")

    if seed.subtype in ("rc_single", "rc_multi"):
        if cluster_role == "passage_owner":
            bits.append(
                "RC cluster: Generate a substantive passage (220-310 "
                "words) in the `stimulus` field with `type='passage'`. "
                "The passage MUST stand on its own (no external context "
                "needed). Pose the FIRST question over it. Subsequent "
                "cluster questions will reuse this passage verbatim."
            )
        elif cluster_role == "passage_consumer":
            bits.append(
                "RC cluster (continuation): The passage will be supplied "
                "to you below in the `stimulus` field. DO NOT regenerate "
                "the passage. Write a single new question over the same "
                "passage. Set `stimulus` to null in your response."
            )
    if seed.subtype == "data_interp":
        if cluster_role == "passage_owner":
            bits.append(
                "DI cluster: emit a `figure_spec` with kind "
                "(bar/line/pie/scatter/table) plus a `series` payload. "
                "Choose data values that make at least 3 different "
                "questions answerable. Subsequent cluster questions "
                "will reuse this chart. CRITICAL: the question stem "
                "MUST restate every number the reader needs to compute "
                "the answer (e.g. embed the exact bar heights or table "
                "cells inline). The chart is a visual aid, not the data "
                "source — a reviewer reading only the stem text (no "
                "image) must be able to solve the question."
            )
        elif cluster_role == "passage_consumer":
            bits.append(
                "DI cluster (continuation): the chart will be supplied "
                "below. Write a single MCQ question (5 options, 1 "
                "correct) that is answerable purely from that chart. "
                "Do not propose a new chart. CRITICAL: the question "
                "stem MUST restate every numeric value the question "
                "depends on (bar values, table cells, percentages). "
                "The chart is a visual aid only — the stem text must "
                "stand on its own without the image."
            )
    if seed.subtype == "qc":
        bits.append(
            "QC: 4 fixed options A/B/C/D. Every variable in the stem "
            "MUST appear in `domain_assumptions`."
        )
    if seed.subtype == "se":
        bits.append(
            "SE: exactly 2 correct options out of 6; both must be "
            "synonyms in context."
        )
    if seed.subtype == "numeric_entry":
        bits.append(
            "Numeric entry: provide `correct_value` (decimal) with a "
            "non-zero `tolerance`, OR `numerator`+`denominator`."
        )
    if seed.measure == "quant" and seed.topic == "geometry":
        fallback = GEOMETRY_FALLBACK_SPECS.get(seed.subtopic, {})
        bits.append(
            "Geometry: emit a `figure_spec` with `kind` and `params` "
            f"matching one of these renderers: {sorted(set(['triangle','circle','coordinate','polygon','wireframe']))}. "
            f"If unsure, use this fallback shape: {json.dumps(fallback)}"
        )
    return " ".join(bits)


# ── RC + DI cluster orchestration ────────────────────────────────────


def _attach_rc_passage(seeds: List[Seed], indices: List[int],
                       owner_idx: int) -> None:
    """Tag the cluster's `extra` dict so the orchestrator knows the role."""
    for i in indices:
        seeds[i].extra = {**(seeds[i].extra or {}),
                          "cluster_role": ("passage_owner"
                                            if i == owner_idx
                                            else "passage_consumer"),
                          "cluster_id": f"rc_cluster_{owner_idx}"}


def _tag_di_cluster(seeds: List[Seed], indices: List[int],
                    owner_idx: int) -> None:
    for i in indices:
        seeds[i].extra = {**(seeds[i].extra or {}),
                          "cluster_role": ("passage_owner"
                                            if i == owner_idx
                                            else "passage_consumer"),
                          "cluster_id": f"di_cluster_{owner_idx}"}


# ── Pipeline construction ────────────────────────────────────────────


def _build_pipeline(factory: LLMClientFactory,
                    drafter_alias: str = "opus") -> SyntheticPipeline:
    drafter = Generator(factory.for_role("drafter"))
    critic = Critic(factory.for_role("critic"),
                    drafter_model_alias=drafter_alias)
    reviser = Reviser(factory.for_role("reviser"))
    # Three-judge jury — Sonnet + Sonnet + Gemini-Pro. Two Sonnets at
    # different temperatures supply Anthropic-side coverage and protect
    # the panel against a single Gemini JSON-mode failure (the median
    # of {5, 5, 0} is 5, not 2.5). No-self-grade against the Opus
    # drafter is enforced at panel construction.
    jury = {
        "judge_a": factory.for_role("judge_a"),
        "judge_b": factory.for_role("judge_b"),
        "judge_c": factory.for_role("judge_c"),
    }
    judge = RubricJudge(jury, drafter_model_alias=drafter_alias,
                        shuffle_options=True)
    solver = AdversarialSolver({
        "solver_a": factory.for_role("solver_a"),
        "solver_b": factory.for_role("solver_b"),
    })
    ambiguity = AmbiguityChecker(factory.for_role("ambiguity"))
    return SyntheticPipeline(
        generator=drafter,
        critic=critic,
        reviser=reviser,
        judge=judge,
        solver=solver,
        ambiguity=ambiguity,
        domain_registry=DEFAULT_REGISTRY,
        revise_budget=2,
        drafter_model_alias=drafter_alias,
    )


# ── Per-seed runner ──────────────────────────────────────────────────


def _attach_geometry_figure(draft: DraftItem, seed: Seed,
                             assets_dir: Path, qid: str) -> Optional[str]:
    """If the seed is geometry, render an SVG and attach the path.

    Returns the relative asset path or None.
    """
    if seed.measure != "quant" or seed.topic != "geometry":
        return None
    spec = draft.figure_spec or GEOMETRY_FALLBACK_SPECS.get(
        seed.subtopic, GEOMETRY_FALLBACK_SPECS["triangles"]
    )
    out_path = assets_dir / f"{qid}.svg"
    fig = render_geometry(spec, out_path)
    # Set stimulus so persist writes a Stimulus row of type 'graph'.
    draft.stimulus = {
        "type": "graph",
        "title": fig.caption,
        "content": fig.caption,
        "render_spec": {
            "kind": "svg_geometry",
            "asset_path": str(out_path.relative_to(assets_dir.parent)),
            "geometry_kind": fig.kind,
            "spec": fig.spec,
        },
    }
    return str(out_path)


def _attach_di_chart(draft: DraftItem, seed: Seed,
                     assets_dir: Path, qid: str) -> Optional[str]:
    """If the seed is DI and owns the chart, render and attach."""
    if seed.subtype != "data_interp":
        return None
    role = (seed.extra or {}).get("cluster_role", "passage_owner")
    if role != "passage_owner":
        return None
    spec = draft.figure_spec or {
        "kind": "bar",
        "title": "Synthetic data",
        "categories": ["Q1", "Q2", "Q3", "Q4"],
        "series": [
            {"label": "A", "values": [10, 12, 15, 9]},
            {"label": "B", "values": [8, 11, 13, 10]},
        ],
    }
    out_path = assets_dir / f"{qid}.png"
    fig = render_data_interp(spec, out_path)
    draft.stimulus = {
        "type": "graph",
        "title": fig.title or fig.caption,
        "content": fig.caption,
        "render_spec": {
            "kind": "matplotlib_chart",
            "asset_path": str(out_path.relative_to(assets_dir.parent)),
            "chart_kind": fig.kind,
            "spec": fig.spec,
        },
    }
    return str(out_path)


# ── Audit logging ────────────────────────────────────────────────────


class JsonlAudit:
    """Per-run JSONL writer; one line per pipeline event."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def emit(self, event: str, **payload):
        rec = {"ts": datetime.now().isoformat(),
               "event": event, **payload}
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


# ── Main runner ──────────────────────────────────────────────────────


def _run_one_seed(
    seed_idx: int,
    seed: Seed,
    pipeline: SyntheticPipeline,
    *,
    run_id: str,
    assets_dir: Path,
    audit: JsonlAudit,
    cluster_state: Dict[str, Any],
    deduper: Any,
) -> Optional[Dict[str, Any]]:
    """Run one seed end-to-end and return a result record (or None)."""
    item_id = f"{run_id}-{seed_idx:03d}"
    audit.emit("seed_start", item_id=item_id,
                subtype=seed.subtype, subtopic=seed.subtopic,
                difficulty=seed.difficulty_target,
                cluster_id=(seed.extra or {}).get("cluster_id"),
                cluster_role=(seed.extra or {}).get("cluster_role"))
    started = time.time()
    cluster_role = (seed.extra or {}).get("cluster_role", "")
    cluster_id = (seed.extra or {}).get("cluster_id", "")

    # Cluster passage_consumer: replay the owner's stimulus into this
    # seed's drafter prompt as `extra_guidance` content.
    extra_payload = ""
    shared_stimulus: Optional[Dict[str, Any]] = None
    if cluster_role == "passage_consumer" and cluster_id in cluster_state:
        owner_record = cluster_state[cluster_id]
        if owner_record.get("kind") == "rc":
            shared_stimulus = owner_record.get("stimulus")
            if shared_stimulus:
                extra_payload = (
                    "Existing passage to use verbatim:\n"
                    + json.dumps(shared_stimulus, ensure_ascii=False)
                )
        elif owner_record.get("kind") == "di":
            shared_stimulus = owner_record.get("stimulus")
            if shared_stimulus:
                extra_payload = (
                    "Existing chart spec to reference (do NOT redraw, "
                    "just write a question over the chart values):\n"
                    + json.dumps(shared_stimulus.get("render_spec", {}).get("spec", {}),
                                 ensure_ascii=False)
                )

    extra_guidance = _drafter_guidance(seed, cluster_role=cluster_role or None)
    if extra_payload:
        extra_guidance = extra_guidance + "\n\n" + extra_payload

    # Inject the extra guidance into the generator via a custom call.
    try:
        draft = pipeline.generator.draft(
            seed,
            subtopic_display(seed.subtopic),
            extra_guidance=extra_guidance,
        )
    except Exception as exc:
        audit.emit("drafter_failed", item_id=item_id, error=str(exc),
                    traceback=traceback.format_exc())
        return None

    # If consumer, force the shared stimulus onto the draft
    if cluster_role == "passage_consumer" and shared_stimulus:
        draft.stimulus = shared_stimulus

    # Programmatic figure for geometry / DI owner.
    asset_path = (
        _attach_geometry_figure(draft, seed, assets_dir, item_id)
        or _attach_di_chart(draft, seed, assets_dir, item_id)
    )

    # Dedup against the running batch.
    is_dup, sim = deduper.is_duplicate(draft.stem, subtopic=seed.subtopic)
    if is_dup:
        audit.emit("dedup_reject", item_id=item_id, similarity=sim)
        return None

    # Drive the pipeline (judge, critic, reviser, solver, ambiguity).
    # We do NOT call run_one verbatim because we want to inject the
    # already-drafted item rather than redraft. So we replicate the
    # critic/judge/solver/ambiguity dance here.
    from services.synthetic.pipeline import (
        PipelineOutcome, _draft_to_payload,
    )
    outcome = PipelineOutcome(
        item_id=item_id, seed=seed,
        final_status="rejected", decision="",
        final_draft=draft,
    )

    # Domain check first.
    domain_result = run_checks(item_id, _draft_to_payload(draft),
                                pipeline.domain_registry)
    outcome.domain_result = domain_result
    if not domain_result.passed:
        audit.emit("domain_failed", item_id=item_id,
                    reason=domain_result.reason)
        outcome.decision = "domain_failed"
        outcome.reject_reason = domain_result.reason
        return _result_dict(outcome, item_id, asset_path, started, audit,
                            persisted=False, qid=None)

    # Critic / revise loop.
    for cycle in range(pipeline.revise_budget + 1):
        payload = _draft_to_payload(draft)
        try:
            judge_agg = pipeline.judge.grade(item_id, payload)
        except Exception as exc:
            audit.emit("judge_failed", item_id=item_id, error=str(exc))
            outcome.decision = "judge_failed"
            return _result_dict(outcome, item_id, asset_path, started, audit,
                                persisted=False, qid=None)
        outcome.final_judge = judge_agg
        decision = judge_agg.gate_decision()
        outcome.decision = decision
        audit.emit("judge_round", item_id=item_id, cycle=cycle,
                    decision=decision, mean=judge_agg.mean_overall,
                    min_axis=judge_agg.min_axis_median,
                    medians=judge_agg.medians,
                    failing_axes=judge_agg.failing_axes)
        if decision == "auto_promote":
            outcome.final_status = "persisted_pending"
            break
        if decision == "reject":
            outcome.final_status = "rejected"
            outcome.reject_reason = "judge_reject"
            return _result_dict(outcome, item_id, asset_path, started,
                                audit, persisted=False, qid=None)
        if cycle >= pipeline.revise_budget:
            outcome.final_status = (
                "persisted_pending" if decision == "pass_to_sme"
                else "marginal_revise"
            )
            break
        try:
            review = pipeline.critic.review(item_id, payload,
                                             judge_aggregate=judge_agg)
        except Exception as exc:
            audit.emit("critic_failed", item_id=item_id, error=str(exc))
            outcome.final_status = "marginal_revise"
            break
        outcome.critic_history.append(review)
        if not review.notes:
            outcome.final_status = (
                "persisted_pending" if decision == "pass_to_sme"
                else "marginal_revise"
            )
            break
        try:
            new_draft = pipeline.reviser.revise(draft, review)
        except Exception as exc:
            audit.emit("reviser_failed", item_id=item_id, error=str(exc))
            outcome.final_status = "marginal_revise"
            break
        if new_draft is draft:
            outcome.final_status = (
                "persisted_pending" if decision == "pass_to_sme"
                else "marginal_revise"
            )
            break
        # SHAPE-PRESERVATION: confirm the reviser didn't change correct_label
        # or option count.
        if (new_draft.correct_label != draft.correct_label
                or len(new_draft.options) != len(draft.options)):
            audit.emit("reviser_drift", item_id=item_id,
                        old_label=draft.correct_label,
                        new_label=new_draft.correct_label,
                        old_n_opts=len(draft.options),
                        new_n_opts=len(new_draft.options))
            # Fall back to the original draft. The reviser shouldn't
            # break the shape; if it does we keep the unrevised version.
            break
        draft = new_draft
        outcome.revise_rounds += 1
        outcome.final_draft = draft
        # Re-render figure in case the spec changed.
        if seed.measure == "quant" and seed.topic == "geometry":
            asset_path = _attach_geometry_figure(draft, seed, assets_dir,
                                                 item_id) or asset_path
        # If consumer in cluster, restore stimulus.
        if cluster_role == "passage_consumer" and shared_stimulus:
            draft.stimulus = shared_stimulus

    if outcome.final_status not in {"persisted_pending", "marginal_revise"}:
        return _result_dict(outcome, item_id, asset_path, started, audit,
                            persisted=False, qid=None)

    # Solver (skip for SE multi: each correct letter must be parsed; we
    # already enforce that at the prompt level. Solvers will return
    # comma-separated letters and solver._normalize handles it.)
    try:
        attempts = pipeline.solver.attempt(_draft_to_payload(draft),
                                            draft.correct_label)
    except Exception as exc:
        audit.emit("solver_failed", item_id=item_id, error=str(exc))
        attempts = []
    solver_res = pipeline.solver.gate(item_id, attempts) if attempts else None
    outcome.solver_result = solver_res
    if solver_res and not solver_res.passed:
        # SE reconciliation before we hard-reject. Drafter's single
        # worst failure for SE is labelling the grammatically-fitting
        # pair as correct while the true synonym pair sits among the
        # distractors. When both cold solvers agree on a pair that is
        # not the drafter's key, swap the key to their agreed pair and
        # re-gate — cold agreement among independent solvers on a
        # different pair is strong evidence that pair is the intended
        # synonym, not a second ambiguous reading.
        reconcile = reconcile_se_key(
            _draft_to_payload(draft), draft.correct_label, attempts,
        )
        if reconcile.should_swap:
            audit.emit("se_key_swap", item_id=item_id,
                        old_label=draft.correct_label,
                        new_label=reconcile.new_label,
                        solver_agreed=reconcile.solver_agreed_pair,
                        reason=reconcile.reason)
            apply_se_key_swap(draft, reconcile.new_label)
            outcome.final_draft = draft
            for att in attempts:
                att.matches_key = answers_match(
                    reconcile.new_label, att.chosen
                )
            solver_res = pipeline.solver.gate(item_id, attempts)
            outcome.solver_result = solver_res
        if solver_res and not solver_res.passed:
            audit.emit("solver_disagreement", item_id=item_id,
                        reason=solver_res.reason)
            outcome.final_status = "rejected"
            outcome.decision = "solver_disagreement"
            outcome.reject_reason = solver_res.reason
            return _result_dict(outcome, item_id, asset_path, started, audit,
                                persisted=False, qid=None)

    # Ambiguity probe (only for MCQ-style with options).
    if pipeline.ambiguity and draft.options:
        try:
            probes = pipeline.ambiguity.probe(_draft_to_payload(draft),
                                               draft.correct_label)
            amb_res = pipeline.ambiguity.gate(item_id, probes,
                                                draft.correct_label)
            outcome.ambiguity_result = amb_res
            if not amb_res.passed:
                audit.emit("ambiguity_reject", item_id=item_id,
                            reason=amb_res.reason)
                outcome.final_status = "rejected"
                outcome.decision = "ambiguous"
                outcome.reject_reason = amb_res.reason
                return _result_dict(outcome, item_id, asset_path, started,
                                    audit, persisted=False, qid=None)
        except Exception as exc:
            audit.emit("ambiguity_failed", item_id=item_id, error=str(exc))

    # Persist.
    try:
        with db.atomic():
            qid = persist_draft(
                draft,
                run_id=run_id,
                judge_aggregate=outcome.final_judge,
                solver_details={
                    "attempts": [
                        {"solver": a.solver_name, "chose": a.chosen,
                          "matches_key": a.matches_key}
                        for a in attempts
                    ],
                } if attempts else None,
                ambiguity_details=(
                    outcome.ambiguity_result.details
                    if outcome.ambiguity_result else None
                ),
                domain_details=outcome.domain_result.details
                    if outcome.domain_result else None,
                initial_status="candidate",
            )
        deduper.register(draft.stem, subtopic=seed.subtopic)
        # Cluster owner: stash the stimulus so consumers can pick it up.
        if cluster_role == "passage_owner" and cluster_id:
            cluster_state[cluster_id] = {
                "kind": "rc" if seed.subtype.startswith("rc") else "di",
                "stimulus": draft.stimulus,
                "owner_qid": qid,
                "asset_path": asset_path,
            }
        audit.emit("persisted", item_id=item_id, qid=qid,
                    decision=outcome.decision,
                    revise_rounds=outcome.revise_rounds)
        # Expert review (Step 4) — runs AFTER persist so the SyntheticGenerationRun
        # row is intact even if the review crashes. The module is
        # non-fatal: if it errors we leave status='candidate' and surface
        # the failure in the audit.
        exp_payload = _build_expert_payload(draft, q_id=qid)
        exp_result = None
        try:
            exp_result = expert_review(
                exp_payload,
                drafter_model="opus",
                factory=_EXPERT_FACTORY,
                panel_aliases=EXPERT_PANEL_ALIASES,
            )
            audit.emit("expert_review", item_id=item_id, qid=qid,
                        verdict=exp_result.get("verdict"),
                        means=exp_result.get("means"),
                        spread=exp_result.get("spread"),
                        excluded_drafter=exp_result.get("excluded_drafter"),
                        defects=exp_result.get("defects"))
            # Update DB row's status + append to provenance.
            _update_after_expert_review(qid, exp_result)
        except Exception as exc:
            audit.emit("expert_review_failed", item_id=item_id, qid=qid,
                        error=str(exc), traceback=traceback.format_exc())
        return _result_dict(outcome, item_id, asset_path, started, audit,
                            persisted=True, qid=qid, expert_review=exp_result)
    except Exception as exc:
        audit.emit("persist_failed", item_id=item_id, error=str(exc),
                    traceback=traceback.format_exc())
        return _result_dict(outcome, item_id, asset_path, started, audit,
                            persisted=False, qid=None)


def _result_dict(outcome, item_id, asset_path, started, audit,
                 *, persisted: bool, qid: Optional[int],
                 expert_review: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    elapsed = time.time() - started
    rec = {
        "item_id": item_id,
        "qid": qid,
        "persisted": persisted,
        "decision": outcome.decision,
        "final_status": outcome.final_status,
        "revise_rounds": outcome.revise_rounds,
        "reject_reason": outcome.reject_reason,
        "asset_path": asset_path,
        "elapsed_s": round(elapsed, 1),
        "subtype": outcome.seed.subtype,
        "subtopic": outcome.seed.subtopic,
        "topic": outcome.seed.topic,
        "measure": outcome.seed.measure,
        "difficulty_target": outcome.seed.difficulty_target,
        "cluster_role": (outcome.seed.extra or {}).get("cluster_role"),
        "cluster_id": (outcome.seed.extra or {}).get("cluster_id"),
    }
    if outcome.final_judge is not None:
        rec["medians"] = outcome.final_judge.medians
        rec["mean"] = outcome.final_judge.mean_overall
        rec["failing_axes"] = outcome.final_judge.failing_axes
    if expert_review is not None:
        rec["expert_review"] = expert_review
    audit.emit("seed_done", **rec)
    return rec


def subtopic_display(sub: str) -> str:
    return subtopic_display_name(sub)


# ── CLI / orchestrator ───────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--total", type=int, default=40)
    p.add_argument("--quant", type=int, default=20)
    p.add_argument("--verbal", type=int, default=20)
    p.add_argument("--difficulty-mix", type=str, default="30:40:30",
                    help="Easy:Medium:Hard ratio (informational only)")
    p.add_argument("--subtopics", type=str, default="auto")
    p.add_argument("--run-id", type=str,
                    default=f"phase1-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    p.add_argument("--out", type=str,
                    default=str(ROOT.parent / "synthetic_sample_review.md"))
    p.add_argument("--max-parallel", type=int, default=1,
                    help="Concurrent items. SQLite is single-writer; "
                         "leave at 1 unless you've vetted thread safety.")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--limit", type=int, default=0,
                    help="If >0, cap total seeds (for smoke runs).")
    p.add_argument("--start-at", type=int, default=0,
                    help="Skip the first N seeds (for resuming a "
                         "crashed run; consults the DB for already-persisted "
                         "items in the same run_id).")
    p.add_argument("--end-at", type=int, default=0,
                    help="If >0, stop after seed index N (exclusive).")
    args = p.parse_args()

    init_db()
    _register_local_backend()

    factory = _build_factory()
    pipeline = _build_pipeline(factory, drafter_alias="opus")
    # Expose the factory to `_run_one_seed` for the expert-review jury.
    global _EXPERT_FACTORY
    _EXPERT_FACTORY = factory

    rng = random.Random(args.seed)
    quant_seeds = _expand_coverage_to_seeds(QUANT_COVERAGE, "quant", rng=rng)
    verbal_seeds = _expand_coverage_to_seeds(VERBAL_COVERAGE, "verbal", rng=rng)
    seeds: List[Seed] = quant_seeds + verbal_seeds

    # Tag clusters. Quant DI cluster is at QUANT_COVERAGE indices [17,18,19];
    # we offset by 0 since quant_seeds is the first slice.
    di_indices = [i for i in DI_CLUSTER_INDICES]
    _tag_di_cluster(seeds, di_indices, owner_idx=di_indices[0])

    verbal_offset = len(quant_seeds)
    for cluster_name, idxs in RC_CLUSTERS.items():
        offset_idxs = [i + verbal_offset for i in idxs]
        _attach_rc_passage(seeds, offset_idxs, owner_idx=offset_idxs[0])

    if args.limit:
        seeds = seeds[: args.limit]

    # Resume support: --start-at and --end-at slice the seed list before
    # cluster tagging is consulted. Previously-persisted items in the
    # same run_id are loaded into the results list so the markdown
    # render reflects the full batch (not just this resume slice).
    keep_lo = args.start_at
    keep_hi = args.end_at if args.end_at > 0 else len(seeds)

    audit_dir = ROOT / "data" / "synthetic" / "runs" / args.run_id
    audit = JsonlAudit(audit_dir / "audit.jsonl")
    audit.emit("run_start", run_id=args.run_id, n_seeds=len(seeds),
                roles=PHASE1_ROLES)

    assets_dir = audit_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    # Per-run row in SyntheticGenerationRun (idempotent on resume)
    run_row, _created = SyntheticGenerationRun.get_or_create(
        run_id=args.run_id,
        defaults={
            "seeded_count": len(seeds),
            "config_json": json.dumps({"roles": PHASE1_ROLES,
                                          "coverage_quant": QUANT_COVERAGE,
                                          "coverage_verbal": VERBAL_COVERAGE}),
        },
    )

    # Use embedding deduper if available (we just installed
    # sentence-transformers).
    deduper = make_default_deduper()
    audit.emit("deduper_chosen", kind=type(deduper).__name__)

    # Cluster state shared across seeds (owner -> consumer handoff).
    cluster_state: Dict[str, Any] = {}

    # IMPORTANT: cluster owners MUST run BEFORE consumers. Sort seeds so
    # owners come first within each cluster.
    def seed_sort_key(idx_seed: Tuple[int, Seed]) -> Tuple[int, int]:
        idx, s = idx_seed
        role = (s.extra or {}).get("cluster_role", "")
        # owners first (0), consumers last (1)
        owner_rank = 0 if role == "passage_owner" else (1 if role == "passage_consumer" else 0)
        return (owner_rank, idx)

    indexed_seeds = sorted(enumerate(seeds), key=seed_sort_key)

    # Two-phase execution: owners first (sequentially so consumers see
    # their stimulus); then consumers in parallel.
    owners = [(i, s) for i, s in indexed_seeds
              if (s.extra or {}).get("cluster_role") in (None, "", "passage_owner")
              and keep_lo <= i < keep_hi]
    consumers = [(i, s) for i, s in indexed_seeds
                  if (s.extra or {}).get("cluster_role") == "passage_consumer"
                  and keep_lo <= i < keep_hi]

    # Pre-load any owner stimuli already persisted in the DB (so resume
    # can still hand them to consumers in this slice).
    if keep_lo > 0:
        for q in Question.select().where(Question.run_id == args.run_id):
            try:
                prov = json.loads(q.provenance_json or "{}")
            except Exception:
                continue
            # The cluster_id was attached to the seed.extra dict and we
            # don't currently persist it; rely on the stimulus rendered
            # asset being on disk already (cluster owners ran in the
            # earlier slice).
            if q.stimulus_id:
                stim = Stimulus.get_or_none(Stimulus.id == q.stimulus_id)
                if stim:
                    try:
                        rs = json.loads(stim.render_spec or "{}")
                    except Exception:
                        rs = {}
                    # Heuristic: stash by topic+subtopic so DI consumers
                    # find the chart from any prior owner.
                    cs_key = f"{q.topic}::{q.subtopic}"
                    cluster_state.setdefault(cs_key, {
                        "kind": "rc" if q.subtype.startswith("rc") else "di",
                        "stimulus": {"type": stim.stimulus_type,
                                       "title": stim.title,
                                       "content": stim.content,
                                       "render_spec": rs},
                        "owner_qid": q.id,
                    })

    results: List[Dict[str, Any]] = []
    print(f"[run] {args.run_id}: {len(seeds)} seeds "
          f"({len(owners)} owners + standalone, {len(consumers)} consumers)",
          file=sys.stderr, flush=True)

    # Phase A: owners + standalone, in parallel up to max_parallel
    if args.max_parallel > 1:
        with ThreadPoolExecutor(max_workers=args.max_parallel) as pool:
            futs = {
                pool.submit(_run_one_seed, idx, s, pipeline,
                             run_id=args.run_id, assets_dir=assets_dir,
                             audit=audit, cluster_state=cluster_state,
                             deduper=deduper): (idx, s)
                for idx, s in owners
            }
            for fut in as_completed(futs):
                idx, s = futs[fut]
                try:
                    rec = fut.result()
                except Exception as exc:
                    print(f"[err] seed {idx}: {exc}", file=sys.stderr)
                    rec = None
                if rec:
                    results.append(rec)
                    print(f"  [{idx:02d}] {rec['decision']:<14s} "
                          f"{rec['subtype']:<14s} {rec['subtopic']:<28s} "
                          f"persisted={rec['persisted']} "
                          f"({rec['elapsed_s']}s)", file=sys.stderr,
                          flush=True)
    else:
        for idx, s in owners:
            rec = _run_one_seed(idx, s, pipeline, run_id=args.run_id,
                                  assets_dir=assets_dir, audit=audit,
                                  cluster_state=cluster_state,
                                  deduper=deduper)
            if rec:
                results.append(rec)
                print(f"  [{idx:02d}] {rec['decision']:<14s} "
                      f"{rec['subtype']:<14s} {rec['subtopic']:<28s} "
                      f"persisted={rec['persisted']} ({rec['elapsed_s']}s)",
                      file=sys.stderr, flush=True)

    # Phase B: consumers (depend on owner state)
    for idx, s in consumers:
        rec = _run_one_seed(idx, s, pipeline, run_id=args.run_id,
                              assets_dir=assets_dir, audit=audit,
                              cluster_state=cluster_state,
                              deduper=deduper)
        if rec:
            results.append(rec)
            print(f"  [{idx:02d}] {rec['decision']:<14s} "
                  f"{rec['subtype']:<14s} {rec['subtopic']:<28s} "
                  f"persisted={rec['persisted']} ({rec['elapsed_s']}s)",
                  file=sys.stderr, flush=True)

    # Cluster atomicity (Step 2): if any cluster member failed to
    # promote, downgrade the rest of the cluster too.
    _enforce_cluster_atomicity(results, audit)

    # Update run summary
    persisted = [r for r in results if r["persisted"]]
    drafted = len(results)
    run_row.drafted_count = drafted
    run_row.persisted_count = len(persisted)
    run_row.finished_at = datetime.now()
    run_row.save()

    audit.emit("run_done", run_id=args.run_id,
                drafted=drafted, persisted=len(persisted))
    audit.close()

    # Append earlier-persisted items from any prior crashed run with the
    # same run_id, so the markdown reflects the full batch.
    seen_qids = {r["qid"] for r in results if r.get("qid")}
    for q in Question.select().where(Question.run_id == args.run_id):
        if q.id in seen_qids:
            continue
        try:
            prov = json.loads(q.provenance_json or "{}")
        except Exception:
            prov = {}
        results.append({
            "item_id": f"resumed-{q.id}",
            "qid": q.id,
            "persisted": True,
            "decision": (prov.get("judge", {}).get("medians") and
                          "auto_promote") or "resumed",
            "subtype": q.subtype,
            "subtopic": q.subtopic,
            "topic": q.topic,
            "measure": q.measure,
            "difficulty_target": q.difficulty_target,
            "cluster_role": None,
            "cluster_id": None,
            "asset_path": None,
            "medians": prov.get("judge", {}).get("medians"),
            "mean": prov.get("judge", {}).get("mean"),
            "elapsed_s": 0,
            "revise_rounds": 0,
            "expert_review": prov.get("expert_review"),
        })

    # Render the markdown sample.
    from scripts.render_synthetic_sample import render_sample_md
    out_md = Path(args.out)
    render_sample_md(
        run_id=args.run_id,
        results=results,
        cluster_state=cluster_state,
        assets_src_dir=assets_dir,
        out_md=out_md,
        roles=PHASE1_ROLES,
    )

    print(f"\n[done] persisted {len(persisted)}/{drafted} ({len(persisted)/max(1,drafted)*100:.1f}%)",
          file=sys.stderr)
    print(f"[done] wrote {out_md}", file=sys.stderr)


if __name__ == "__main__":
    main()
