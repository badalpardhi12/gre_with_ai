"""
Smoke tests for `scripts.render_synthetic_sample`.

These don't run the full pipeline — they synthesize a `(Question,
Stimulus, QuestionOption)` set covering geometry, DI, and RC, then
invoke `render_sample_md` and assert:

- Geometry SVG path appears as an image embed in the markdown.
- DI cluster shows a single `![chart]` line above its 3 questions.
- RC cluster shows the passage above its questions, with all questions
  underneath.
- Cluster integrity: a cluster of 3 stays a cluster of 3 in the output.
- Expert-review verdict line appears when provenance includes one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _persist_question(
    *, run_id, measure, subtype, topic, subtopic, difficulty,
    stem, options, correct_label, explanation,
    stimulus=None, provenance=None,
):
    """Insert a Question (and dependencies) directly into the test DB."""
    from models.database import (
        Question, QuestionOption, Stimulus,
    )
    stim_row = None
    if stimulus is not None:
        stim_row = Stimulus.create(
            stimulus_type=stimulus.get("type", "passage"),
            title=stimulus.get("title", ""),
            content=stimulus.get("content", ""),
            render_spec=json.dumps(stimulus.get("render_spec") or {}),
        )
    q = Question.create(
        measure=measure, subtype=subtype, stimulus=stim_row,
        prompt=stem, difficulty_target=difficulty,
        time_target_seconds=90,
        concept_tags=json.dumps([]),
        topic=topic, subtopic=subtopic,
        source="ai_synthetic", quality_score=0.9,
        provenance="llm_generated", status="candidate",
        explanation=explanation,
        provenance_json=json.dumps(provenance or {}),
        review_notes="", run_id=run_id,
    )
    for opt in options:
        QuestionOption.create(
            question=q,
            option_label=opt["label"],
            option_text=opt["text"],
            is_correct=opt.get("is_correct", False),
        )
    return q


def _make_provenance(*, judge_mean=4.5, expert_verdict="live"):
    """Build a minimal provenance dict with judge + expert review blocks."""
    medians = {
        "content_validity": 5, "construct_alignment": 5,
        "difficulty_plausibility": 4, "distractor_quality": 4,
        "language_clarity": 5, "fairness_bias": 5,
    }
    judge = {
        "medians": medians, "mean": judge_mean,
        "min_axis": 4, "failing_axes": [],
        "per_judge": [
            {"name": "judge_a", "axes": medians},
        ],
    }
    expert = {
        "verdict": expert_verdict,
        "scores": {
            "correctness": [5, 5, 5],
            "clarity": [5, 4, 5],
            "distractor_quality": [4, 4, 5],
            "difficulty_match": [5, 4, 4],
            "gre_authenticity": [5, 5, 4],
        },
        "means": {
            "correctness": 5.0, "clarity": 4.67,
            "distractor_quality": 4.33, "difficulty_match": 4.33,
            "gre_authenticity": 4.67,
        },
        "spread": {
            "correctness": 0, "clarity": 1, "distractor_quality": 1,
            "difficulty_match": 1, "gre_authenticity": 1,
        },
        "defects": ["Distractor B is borderline"],
        "reviewer_notes": "All axes passed the live-promotion gate.",
        "excluded_drafter": "opus",
    }
    return {
        "run_id": "test-run", "seed": {}, "judge": judge,
        "solver": {"attempts": [
            {"solver": "solver_a", "chose": "B", "matches_key": True},
            {"solver": "solver_b", "chose": "B", "matches_key": True},
        ]},
        "expert_review": expert,
    }


def test_render_full_sample_with_clusters_and_assets(temp_db, tmp_path):
    """End-to-end smoke: geometry SVG, RC passage, DI 3-Q cluster, expert review."""
    from scripts.render_synthetic_sample import render_sample_md

    run_id = "smoke-test-render"

    # 1. Geometry standalone item with an SVG asset.
    svg_asset = tmp_path / "geom.svg"
    svg_asset.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<rect width="100" height="100" fill="white"/></svg>',
        encoding="utf-8",
    )
    geom_q = _persist_question(
        run_id=run_id, measure="quant", subtype="mcq_single",
        topic="geometry", subtopic="triangles", difficulty=3,
        stem="In the figure, triangle ABC has...",
        options=[
            {"label": "A", "text": "5"},
            {"label": "B", "text": "12"},
            {"label": "C", "text": "13", "is_correct": True},
            {"label": "D", "text": "15"},
            {"label": "E", "text": "17"},
        ],
        correct_label="C",
        explanation="By the Pythagorean theorem, c=13.",
        stimulus={
            "type": "graph", "title": "Right triangle",
            "content": "Right triangle ABC",
            "render_spec": {"asset_path": str(svg_asset)},
        },
        provenance=_make_provenance(),
    )
    geom_rec = {
        "item_id": "geom-1", "qid": geom_q.id, "persisted": True,
        "decision": "auto_promote", "subtype": "mcq_single",
        "subtopic": "triangles", "topic": "geometry", "measure": "quant",
        "difficulty_target": 3, "cluster_role": None, "cluster_id": None,
        "asset_path": str(svg_asset),
        "medians": {"content_validity": 5}, "mean": 4.5,
        "revise_rounds": 0, "elapsed_s": 1.0,
        "expert_review": _make_provenance()["expert_review"],
    }

    # 2. RC cluster: 1 owner with passage + 2 consumers sharing it.
    passage_text = (
        "Recent investigations into honeybee navigation have refined the "
        "classical account of the waggle dance, demonstrating that "
        "olfactory cues serve as essential supplements to the spatial "
        "information conveyed by the dance itself."
    )
    rc_owner = _persist_question(
        run_id=run_id, measure="verbal", subtype="rc_single",
        topic="reading_comprehension", subtopic="rc_main_idea",
        difficulty=3,
        stem="The primary purpose of the passage is to:",
        options=[
            {"label": "A", "text": "Discredit the waggle dance theory"},
            {"label": "B", "text": "Refine the classical account",
             "is_correct": True},
            {"label": "C", "text": "Compare honeybee species"},
            {"label": "D", "text": "Defend a single-channel model"},
            {"label": "E", "text": "Propose a new model"},
        ],
        correct_label="B",
        explanation="Refines, not discredits.",
        stimulus={
            "type": "passage", "title": "Honeybee navigation",
            "content": passage_text,
            "render_spec": {},
        },
        provenance=_make_provenance(expert_verdict="live"),
    )
    rc_consumer = _persist_question(
        run_id=run_id, measure="verbal", subtype="rc_single",
        topic="reading_comprehension", subtopic="rc_inference",
        difficulty=3,
        stem="The passage suggests that without olfactory cues:",
        options=[
            {"label": "A", "text": "Bees cannot leave the hive"},
            {"label": "B", "text": "Bees would still navigate efficiently"},
            {"label": "C", "text": "Bees would have greater difficulty",
             "is_correct": True},
            {"label": "D", "text": "Bees would use solar navigation"},
            {"label": "E", "text": "Bees would misread the angle"},
        ],
        correct_label="C",
        explanation="Olfactory cues are essential supplements.",
        provenance=_make_provenance(expert_verdict="draft"),
    )
    rc_owner_rec = {
        "item_id": "rc-owner", "qid": rc_owner.id, "persisted": True,
        "decision": "auto_promote", "subtype": "rc_single",
        "subtopic": "rc_main_idea", "topic": "reading_comprehension",
        "measure": "verbal", "difficulty_target": 3,
        "cluster_role": "passage_owner", "cluster_id": "rc_cluster_test",
        "asset_path": None, "medians": {}, "mean": 4.5,
        "revise_rounds": 0, "elapsed_s": 1.0,
        "expert_review": _make_provenance()["expert_review"],
    }
    rc_consumer_rec = {
        "item_id": "rc-cons", "qid": rc_consumer.id, "persisted": True,
        "decision": "auto_promote", "subtype": "rc_single",
        "subtopic": "rc_inference", "topic": "reading_comprehension",
        "measure": "verbal", "difficulty_target": 3,
        "cluster_role": "passage_consumer", "cluster_id": "rc_cluster_test",
        "asset_path": None, "medians": {}, "mean": 4.4,
        "revise_rounds": 0, "elapsed_s": 1.0,
        "expert_review": _make_provenance(expert_verdict="draft")["expert_review"],
    }

    # 3. DI cluster: 1 owner with chart + 2 consumers (3 total).
    chart_asset = tmp_path / "chart.png"
    # Minimal valid PNG (1x1 white pixel).
    chart_asset.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
        b"\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\x99c"
        b"\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa3K\x14\x9a\x00\x00\x00\x00"
        b"IEND\xaeB`\x82"
    )
    di_owner = _persist_question(
        run_id=run_id, measure="quant", subtype="data_interp",
        topic="data_analysis", subtopic="data_interpretation", difficulty=2,
        stem="In 2022 the ratio of B to A was greatest. What is C/total?",
        options=[
            {"label": "A", "text": "1:5"},
            {"label": "B", "text": "1:4"},
            {"label": "C", "text": "1:3", "is_correct": True},
            {"label": "D", "text": "2:5"},
            {"label": "E", "text": "1:2"},
        ],
        correct_label="C", explanation="Total=90, C=30, so 1/3.",
        stimulus={
            "type": "graph", "title": "Stacked bar chart",
            "content": "Annual revenue 2018-2022 by imprint.",
            "render_spec": {"asset_path": str(chart_asset),
                            "kind": "matplotlib_chart"},
        },
        provenance=_make_provenance(),
    )
    di_q2 = _persist_question(
        run_id=run_id, measure="quant", subtype="data_interp",
        topic="data_analysis", subtopic="data_interpretation", difficulty=3,
        stem="What was A's revenue in 2020?",
        options=[
            {"label": "A", "text": "20"}, {"label": "B", "text": "22"},
            {"label": "C", "text": "25", "is_correct": True},
            {"label": "D", "text": "28"}, {"label": "E", "text": "30"},
        ],
        correct_label="C", explanation="Read off the bar.",
        provenance=_make_provenance(),
    )
    di_q3 = _persist_question(
        run_id=run_id, measure="quant", subtype="data_interp",
        topic="data_analysis", subtopic="data_interpretation", difficulty=4,
        stem="From 2018 to 2022, by what % did B grow?",
        options=[
            {"label": "A", "text": "11%"}, {"label": "B", "text": "55%"},
            {"label": "C", "text": "100%"},
            {"label": "D", "text": "122%", "is_correct": True},
            {"label": "E", "text": "200%"},
        ],
        correct_label="D", explanation="40/18 - 1 = 122%.",
        provenance=_make_provenance(),
    )
    di_owner_rec = {
        "item_id": "di-owner", "qid": di_owner.id, "persisted": True,
        "decision": "auto_promote", "subtype": "data_interp",
        "subtopic": "data_interpretation", "topic": "data_analysis",
        "measure": "quant", "difficulty_target": 2,
        "cluster_role": "passage_owner", "cluster_id": "di_cluster_test",
        "asset_path": str(chart_asset),
        "medians": {}, "mean": 4.5,
        "revise_rounds": 0, "elapsed_s": 1.0,
        "expert_review": _make_provenance()["expert_review"],
    }
    di_q2_rec = {
        "item_id": "di-2", "qid": di_q2.id, "persisted": True,
        "decision": "auto_promote", "subtype": "data_interp",
        "subtopic": "data_interpretation", "topic": "data_analysis",
        "measure": "quant", "difficulty_target": 3,
        "cluster_role": "passage_consumer", "cluster_id": "di_cluster_test",
        "asset_path": None, "medians": {}, "mean": 4.4,
        "revise_rounds": 0, "elapsed_s": 1.0,
        "expert_review": _make_provenance()["expert_review"],
    }
    di_q3_rec = {
        "item_id": "di-3", "qid": di_q3.id, "persisted": True,
        "decision": "auto_promote", "subtype": "data_interp",
        "subtopic": "data_interpretation", "topic": "data_analysis",
        "measure": "quant", "difficulty_target": 4,
        "cluster_role": "passage_consumer", "cluster_id": "di_cluster_test",
        "asset_path": None, "medians": {}, "mean": 4.3,
        "revise_rounds": 0, "elapsed_s": 1.0,
        "expert_review": _make_provenance()["expert_review"],
    }

    results = [
        geom_rec, rc_owner_rec, rc_consumer_rec,
        di_owner_rec, di_q2_rec, di_q3_rec,
    ]
    out_md = tmp_path / "review.md"
    rendered = render_sample_md(
        run_id=run_id, results=results, cluster_state={},
        assets_src_dir=tmp_path, out_md=out_md, roles={},
    )
    text = rendered.read_text(encoding="utf-8")

    # ── Assertions ──

    # Geometry: SVG asset embedded inline as an image.
    assert "geom.svg" in text, "geometry SVG should be referenced"
    assert "![figure](geom.svg)" in text or "![figure](geom" in text

    # RC: passage shown above questions, only ONCE.
    assert passage_text[:60] in text, "passage text should appear"
    # Cluster header should announce 2 questions.
    assert "RC cluster `rc_cluster_test` (2 questions)" in text
    # Both consumer + owner stems present.
    assert "primary purpose of the passage" in text
    assert "without olfactory cues" in text
    # Passage shouldn't appear twice (count occurrences of distinctive phrase).
    assert text.count("Recent investigations into honeybee navigation") == 1

    # DI: chart embedded ONCE above 3 questions.
    assert "DI cluster `di_cluster_test` (3 questions)" in text
    assert "![chart](chart.png)" in text
    assert text.count("![chart](chart.png)") == 1
    # All three DI stems present.
    assert "ratio of B to A was greatest" in text
    assert "A's revenue in 2020" in text
    assert "by what % did B grow" in text

    # Expert review verdict surfaces.
    assert "**Expert Review:**" in text
    assert "verdict=`live`" in text
    assert "verdict=`draft`" in text  # the consumer was drafted
    assert "drafter `opus` excluded" in text

    # Verification line surfaces the marked correct option.
    assert "**Verification:**" in text


def test_di_chart_recovered_from_persisted_render_spec(temp_db, tmp_path):
    """If `result.asset_path` is missing, renderer falls back to the DB row."""
    from scripts.render_synthetic_sample import render_sample_md

    run_id = "smoke-test-recover-asset"
    chart_asset = tmp_path / "fallback_chart.png"
    chart_asset.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
        b"\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\x99c"
        b"\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa3K\x14\x9a\x00\x00\x00\x00"
        b"IEND\xaeB`\x82"
    )
    di = _persist_question(
        run_id=run_id, measure="quant", subtype="data_interp",
        topic="data_analysis", subtopic="data_interpretation", difficulty=3,
        stem="Question over chart",
        options=[
            {"label": "A", "text": "1"},
            {"label": "B", "text": "2", "is_correct": True},
            {"label": "C", "text": "3"}, {"label": "D", "text": "4"},
            {"label": "E", "text": "5"},
        ],
        correct_label="B", explanation="Just because.",
        stimulus={
            "type": "graph", "title": "x", "content": "x",
            "render_spec": {"asset_path": str(chart_asset)},
        },
        provenance=_make_provenance(),
    )
    rec = {
        "item_id": "x", "qid": di.id, "persisted": True,
        "decision": "auto_promote", "subtype": "data_interp",
        "subtopic": "data_interpretation", "topic": "data_analysis",
        "measure": "quant", "difficulty_target": 3,
        "cluster_role": "passage_owner", "cluster_id": "di_recover",
        "asset_path": None,  # MISSING — must be recovered from the DB row.
        "medians": {}, "mean": 4.5, "revise_rounds": 0, "elapsed_s": 1.0,
    }
    out_md = tmp_path / "review.md"
    rendered = render_sample_md(
        run_id=run_id, results=[rec], cluster_state={},
        assets_src_dir=tmp_path, out_md=out_md, roles={},
    )
    text = rendered.read_text(encoding="utf-8")
    assert "fallback_chart.png" in text


def test_renderer_does_not_crash_when_passage_missing(temp_db, tmp_path):
    """RC cluster with empty passage falls back to a placeholder."""
    from scripts.render_synthetic_sample import render_sample_md

    run_id = "smoke-test-missing-passage"
    rc = _persist_question(
        run_id=run_id, measure="verbal", subtype="rc_single",
        topic="reading_comprehension", subtopic="rc_inference", difficulty=3,
        stem="From the passage, infer that...",
        options=[
            {"label": "A", "text": "x"},
            {"label": "B", "text": "y", "is_correct": True},
            {"label": "C", "text": "z"}, {"label": "D", "text": "w"},
            {"label": "E", "text": "v"},
        ],
        correct_label="B", explanation="...",
        stimulus={"type": "passage", "title": "", "content": "",
                  "render_spec": {}},
        provenance={},
    )
    rec = {
        "item_id": "rc-orphan", "qid": rc.id, "persisted": True,
        "decision": "auto_promote", "subtype": "rc_single",
        "subtopic": "rc_inference", "topic": "reading_comprehension",
        "measure": "verbal", "difficulty_target": 3,
        "cluster_role": "passage_consumer", "cluster_id": "rc_orphan",
        "asset_path": None, "medians": {}, "mean": 0,
        "revise_rounds": 0, "elapsed_s": 0,
    }
    out_md = tmp_path / "review.md"
    rendered = render_sample_md(
        run_id=run_id, results=[rec], cluster_state={},
        assets_src_dir=tmp_path, out_md=out_md, roles={},
    )
    text = rendered.read_text(encoding="utf-8")
    assert "passage missing" in text
