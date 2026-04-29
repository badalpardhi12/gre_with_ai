"""
Tests for the synthetic-pipeline scaffolding.

Covered here:
- migration `_012_synthetic_provenance_2026_04` adds the four columns
  on `question` and the `syntheticgenerationrun` table is reachable.
- `_exclude_synthetic_clause` hides AI items when toggle is off and
  passes them through when on, across all four selector entry points.
- The rubric judge correctly aggregates per-axis medians, gates on the
  thresholds, and computes inter-judge agreement — all with a stub
  LLM so no network is hit.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from services.synthetic.llm_client import LLMClient, LLMResponse


# ── Stub LLM for judge tests ─────────────────────────────────────────


class CannedJudge(LLMClient):
    """Stub judge that returns a pre-baked dict of scores."""

    def __init__(self, scores: Dict[str, int], justification: str = "ok"):
        self.scores = scores
        self.justification = justification
        self.calls = 0

    def complete(self, *args, **kwargs):  # pragma: no cover — unused
        raise NotImplementedError

    def complete_json(self, messages, system=None, max_tokens=2048,
                       temperature=None, retries=1) -> LLMResponse:
        self.calls += 1
        payload = {
            "scores": {
                axis: {"score": score, "justification": self.justification}
                for axis, score in self.scores.items()
            }
        }
        return LLMResponse(text=json.dumps(payload), parsed_json=payload)


# ── Schema migration ────────────────────────────────────────────────


def test_migration_012_adds_question_columns(temp_db):
    """The Question table must have the four new columns after init_db()."""
    from models.database import db, Question
    rows = db.execute_sql("PRAGMA table_info(question)").fetchall()
    cols = {r[1] for r in rows}
    for expected in ("provenance_json", "review_notes", "generated_at", "run_id"):
        assert expected in cols, f"missing column: {expected}"

    # ORM-level smoke: build a row that uses every new field.
    q = Question.create(
        measure="verbal", subtype="tc", prompt="stub",
        source="ai_synthetic", subtopic="tc_1_blank",
        provenance_json=json.dumps({"x": 1}),
        review_notes="needs SME review",
        run_id="test-run-001",
    )
    assert q.run_id == "test-run-001"
    assert q.get_provenance() == {"x": 1}


def test_migration_012_creates_synthetic_run_table(temp_db):
    from models.database import db, SyntheticGenerationRun
    rows = db.execute_sql(
        "PRAGMA table_info(syntheticgenerationrun)"
    ).fetchall()
    cols = {r[1] for r in rows}
    for expected in (
        "run_id", "started_at", "finished_at", "seeded_count",
        "drafted_count", "survived_solver", "survived_judge",
        "survived_domain", "persisted_count", "config_json",
        "cost_estimate_usd",
    ):
        assert expected in cols, f"missing column: {expected}"

    run = SyntheticGenerationRun.create(
        run_id="r-1", seeded_count=10, drafted_count=8,
        survived_judge=6, persisted_count=4,
    )
    assert run.id


def test_migration_012_run_id_indexed(temp_db):
    """Confirm the explicit index from migration 012 is present."""
    from models.database import db
    rows = db.execute_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='question'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_question_run_id" in names, names


def test_migration_013_adds_pretest_columns(temp_db):
    """R5 lifecycle migration must add 7 pretest/IRT columns to question."""
    from models.database import db, Question
    rows = db.execute_sql("PRAGMA table_info(question)").fetchall()
    cols = {r[1] for r in rows}
    expected = (
        "pretest_started_at", "pretest_n_responses", "pretest_p_correct",
        "pretest_disc_proxy", "irt_b_estimate", "irt_a_estimate",
        "promotion_at",
    )
    for col in expected:
        assert col in cols, f"missing column: {col}"

    # ORM round-trip: insert a row at status='candidate' (R5 default) and
    # confirm the new fields read back as None / 0 by default.
    q = Question.create(
        measure="quant", subtype="mcq_single", prompt="2+2=?",
        source="ai_synthetic", subtopic="integers_number_properties",
        topic="arithmetic", status="candidate",
    )
    assert q.status == "candidate"
    assert q.pretest_n_responses == 0
    assert q.pretest_p_correct is None
    assert q.irt_b_estimate is None

    # Promotion path simulation: candidate -> pretest with a started_at.
    from datetime import datetime
    q.status = "pretest"
    q.pretest_started_at = datetime.now()
    q.pretest_n_responses = 5
    q.pretest_p_correct = 0.6
    q.save()
    refreshed = Question.get(Question.id == q.id)
    assert refreshed.status == "pretest"
    assert refreshed.pretest_n_responses == 5
    assert abs((refreshed.pretest_p_correct or 0) - 0.6) < 1e-9


def test_migration_013_creates_pretest_index(temp_db):
    """Partial index over status='pretest' lets the pretester find the
    next slot in O(log n) instead of scanning every live row."""
    from models.database import db
    rows = db.execute_sql(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='question'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_question_status_pretest" in names, names


def test_migration_013_idempotent(temp_db):
    """Re-running the migration must be a no-op (no duplicate-column error)."""
    from models.database import db
    from models.migrations import _013_question_lifecycle_2026_05
    # Run a second time directly; the apply_pending_migrations machinery
    # already ran it once during temp_db init.
    _013_question_lifecycle_2026_05()
    rows = db.execute_sql("PRAGMA table_info(question)").fetchall()
    cols = [r[1] for r in rows]
    # No duplicates.
    assert len(cols) == len(set(cols))


# ── Persist defaults ────────────────────────────────────────────────


def test_persist_default_status_is_candidate(temp_db):
    """R5: persist_draft must default to status='candidate', not 'draft'."""
    from datetime import datetime
    from services.synthetic.persist import persist_draft
    from services.synthetic.types import DraftItem, DraftOption, Seed
    from models.database import Question

    seed = Seed(measure="quant", topic="arithmetic",
                subtopic="integers_number_properties", subtype="mcq_single",
                difficulty_target=3)
    draft = DraftItem(
        subtype="mcq_single",
        stem="If $n$ is a positive integer divisible by 3, what is $n+3$ mod 3?",
        options=[
            DraftOption(label="A", text="0", is_correct=True, misconception=""),
            DraftOption(label="B", text="1", misconception="off_by_one"),
            DraftOption(label="C", text="2", misconception="reversed_modulo"),
            DraftOption(label="D", text="3", misconception="forgets_modulo"),
            DraftOption(label="E", text="cannot be determined",
                        misconception="hedge_default"),
        ],
        correct_label="A",
        explanation="n+3 is divisible by 3, so the remainder is 0.",
        difficulty_target=2,
        seed=seed,
        prompt_hash="abc123",
        generated_at=datetime.now(),
    )
    qid = persist_draft(draft, run_id="test-r5-default")
    q = Question.get(Question.id == qid)
    assert q.status == "candidate"
    assert q.run_id == "test-r5-default"


def test_persist_can_override_status(temp_db):
    """Caller can still ask for 'pretest' / 'live' explicitly when needed."""
    from datetime import datetime
    from services.synthetic.persist import persist_draft
    from services.synthetic.types import DraftItem, DraftOption, Seed
    from models.database import Question

    seed = Seed(measure="verbal", topic="text_completion",
                subtopic="tc_1_blank", subtype="tc", difficulty_target=3)
    draft = DraftItem(
        subtype="tc",
        stem="The senator was so ____ that her speech put the audience to sleep.",
        options=[
            DraftOption(label="A", text="soporific", is_correct=True),
            DraftOption(label="B", text="effervescent",
                        misconception="opposite_valence"),
            DraftOption(label="C", text="ostentatious",
                        misconception="adjacent_meaning"),
            DraftOption(label="D", text="insipid",
                        misconception="related_but_wrong"),
            DraftOption(label="E", text="mendacious",
                        misconception="unrelated_negative"),
        ],
        correct_label="A",
        explanation="Soporific = sleep-inducing.",
        difficulty_target=3,
        seed=seed,
        prompt_hash="def456",
        generated_at=datetime.now(),
    )
    qid = persist_draft(draft, run_id="test-r5-override",
                        initial_status="pretest")
    q = Question.get(Question.id == qid)
    assert q.status == "pretest"


# ── Toggle filter ───────────────────────────────────────────────────


@pytest.fixture
def two_questions(temp_db):
    """Insert one synthetic + one human item in the same subtopic/measure."""
    from models.database import Question
    human = Question.create(
        measure="verbal", subtype="tc", prompt="Human stem",
        source="manhattan_5lb_2018", subtopic="tc_1_blank",
        topic="text_completion", status="live",
    )
    synth = Question.create(
        measure="verbal", subtype="tc", prompt="Synth stem",
        source="ai_synthetic", subtopic="tc_1_blank",
        topic="text_completion", status="live",
    )
    return human.id, synth.id


def _set_toggle(value: bool):
    from config import save_user_pref
    save_user_pref("include_ai_synthetic", value)


def test_toggle_on_includes_synthetic(two_questions):
    human_id, synth_id = two_questions
    _set_toggle(True)
    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    pool = qb._pool_for_subtype("verbal", "tc", "medium", set())
    assert human_id in pool
    assert synth_id in pool


def test_toggle_off_excludes_synthetic_in_pool(two_questions):
    human_id, synth_id = two_questions
    _set_toggle(False)
    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    pool = qb._pool_for_subtype("verbal", "tc", "medium", set())
    assert human_id in pool
    assert synth_id not in pool


def test_toggle_off_excludes_synthetic_in_select_questions(two_questions):
    human_id, synth_id = two_questions
    _set_toggle(False)
    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    ids = qb.select_questions("verbal", count=10, difficulty_band="medium")
    assert synth_id not in ids


def test_toggle_off_excludes_synthetic_in_drill_smart(two_questions):
    human_id, synth_id = two_questions
    _set_toggle(False)
    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    ids = qb.select_drill_smart("tc_1_blank", count=10)
    assert synth_id not in ids
    assert human_id in ids


def test_toggle_off_excludes_synthetic_in_composed(two_questions):
    """select_questions_composed must respect the toggle through both
    its primary _pool_for_subtype path and its fallback path."""
    human_id, synth_id = two_questions
    _set_toggle(False)
    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    ids = qb.select_questions_composed("verbal", count=10)
    assert synth_id not in ids


# ── Rubric judge ────────────────────────────────────────────────────


def test_judge_aggregates_medians_and_gates_pass():
    from services.synthetic.judge import RubricJudge, MIN_AXIS_THRESHOLD
    # Three judges, all scoring 5/5 across the board → must pass.
    high_scores = {
        "content_validity": 5, "construct_alignment": 5,
        "difficulty_plausibility": 5, "distractor_quality": 5,
        "language_clarity": 5, "fairness_bias": 5,
    }
    panel = {f"j{i}": CannedJudge(high_scores) for i in range(3)}
    rj = RubricJudge(panel)
    payload = {"stem": "anything", "options": [], "subtype": "tc"}
    agg = rj.grade("item-1", payload)
    assert agg.mean_overall == 5.0
    assert agg.min_axis_median == 5.0
    res = rj.gate(agg)
    assert res.passed is True
    assert "per_judge" in res.details
    assert all(c.calls == 1 for c in panel.values())


def test_judge_fails_on_axis_below_threshold():
    from services.synthetic.judge import RubricJudge
    # All 5s except one axis where two judges scored 2 → median 2 < 3 → fail
    # under the refined (post-anchor) thresholds.
    high = {
        "content_validity": 5, "construct_alignment": 5,
        "difficulty_plausibility": 5, "distractor_quality": 5,
        "language_clarity": 5, "fairness_bias": 5,
    }
    low = dict(high)
    low["distractor_quality"] = 2
    panel = {"j1": CannedJudge(low), "j2": CannedJudge(low),
             "j3": CannedJudge(high)}
    rj = RubricJudge(panel)
    payload = {"stem": "anything", "options": [], "subtype": "tc"}
    agg = rj.grade("item-2", payload)
    assert agg.medians["distractor_quality"] == 2
    res = rj.gate(agg)
    assert res.passed is False
    assert "distractor_quality" in agg.failing_axes


def test_judge_fails_when_mean_below_threshold():
    from services.synthetic.judge import RubricJudge
    # Every axis at 3 across three judges → median 3 (passes the
    # min_axis=3 floor) but mean 3.0 < 3.8 → must fail mean threshold.
    threes = {
        "content_validity": 3, "construct_alignment": 3,
        "difficulty_plausibility": 3, "distractor_quality": 3,
        "language_clarity": 3, "fairness_bias": 4,  # fairness must clear hard floor
    }
    panel = {f"j{i}": CannedJudge(threes) for i in range(3)}
    rj = RubricJudge(panel)
    agg = rj.grade("item-3", {"stem": "x", "options": [], "subtype": "tc"})
    assert agg.min_axis_median == 3.0
    # mean = (3+3+3+3+3+4)/6 ≈ 3.17 < 3.8
    assert agg.mean_overall < 3.8
    assert rj.gate(agg).passed is False


def test_judge_fairness_hard_floor_blocks_pass():
    """Fairness < 4 must reject the item even if mean is high."""
    from services.synthetic.judge import RubricJudge
    scores = {
        "content_validity": 5, "construct_alignment": 5,
        "difficulty_plausibility": 5, "distractor_quality": 5,
        "language_clarity": 5, "fairness_bias": 3,  # below hard floor
    }
    panel = {f"j{i}": CannedJudge(scores) for i in range(3)}
    rj = RubricJudge(panel)
    agg = rj.grade("item-fair", {"stem": "x", "options": [], "subtype": "tc"})
    assert agg.medians["fairness_bias"] == 3
    res = rj.gate(agg)
    # Mean is 4.67 — would pass mean threshold but fairness fails hard.
    assert agg.mean_overall > 4.0
    assert res.passed is False


def test_judge_handles_malformed_response():
    """A judge that returns garbage should yield 0 scores, item must fail."""
    from services.synthetic.judge import RubricJudge

    class BrokenJudge(LLMClient):
        def complete(self, *a, **kw):  # pragma: no cover
            raise NotImplementedError

        def complete_json(self, *a, **kw):
            return LLMResponse(text="not json at all")

    rj = RubricJudge({"broken": BrokenJudge()})
    agg = rj.grade("item-4", {"stem": "x", "options": [], "subtype": "tc"})
    # Every axis 0 → mean 0 → fail.
    assert agg.mean_overall == 0.0
    assert rj.gate(agg).passed is False


def test_judge_agreement_rate():
    from services.synthetic.judge import (
        parse_judge_response, judge_agreement_rate,
    )
    payload_a = {
        "scores": {axis: {"score": 5, "justification": ""} for axis in (
            "content_validity", "construct_alignment",
            "difficulty_plausibility", "distractor_quality",
            "language_clarity", "fairness_bias",
        )}
    }
    payload_b = json.loads(json.dumps(payload_a))
    payload_b["scores"]["distractor_quality"]["score"] = 1  # outlier
    reports = [
        parse_judge_response("a", "i-1", json.dumps(payload_a)),
        parse_judge_response("b", "i-1", json.dumps(payload_b)),
    ]
    rates = judge_agreement_rate(reports)
    assert rates["content_validity"] == 1.0
    # 5 vs 1 differs by more than 1 → 0% agreement on that axis.
    assert rates["distractor_quality"] == 0.0


# ── Domain checks (sanity) ──────────────────────────────────────────


def test_domain_checks_qc_canonical_options():
    from services.synthetic.domain_checks import DEFAULT_REGISTRY, run_checks
    bad_qc = {
        "subtype": "qc",
        "stem": "Quantity A: x; Quantity B: 0",
        "options": [
            {"label": "A", "text": "Greater", "is_correct": False},
            {"label": "B", "text": "Less", "is_correct": True},
            {"label": "C", "text": "Equal", "is_correct": False},
            # Missing D
        ],
        "domain_assumptions": ["x is real"],
    }
    res = run_checks("bad-qc", bad_qc, DEFAULT_REGISTRY)
    assert res.passed is False
    failed = {f["check"] for f in res.details["failures"]}
    assert "qc_canonical_options" in failed


def test_domain_checks_qc_undeclared_variable():
    from services.synthetic.domain_checks import DEFAULT_REGISTRY, run_checks
    bad_qc = {
        "subtype": "qc",
        "stem": "Quantity A: x + y; Quantity B: x*y",
        "options": [
            {"label": "A", "text": "A", "is_correct": False},
            {"label": "B", "text": "B", "is_correct": False},
            {"label": "C", "text": "Equal", "is_correct": False},
            {"label": "D", "text": "Cannot be determined", "is_correct": True},
        ],
        "domain_assumptions": ["x is positive"],  # y missing
    }
    res = run_checks("undecl", bad_qc, DEFAULT_REGISTRY)
    assert res.passed is False
    reasons = " ".join(f["reason"] for f in res.details["failures"])
    assert "y" in reasons


def test_domain_checks_se_two_correct():
    from services.synthetic.domain_checks import DEFAULT_REGISTRY, run_checks
    bad_se = {
        "subtype": "se",
        "stem": "He was ___.",
        "options": [
            {"label": chr(65 + i), "text": f"opt{i}",
             "is_correct": (i == 0)}
            for i in range(6)
        ],
    }
    res = run_checks("bad-se", bad_se, DEFAULT_REGISTRY)
    assert res.passed is False


def test_domain_checks_self_reference():
    from services.synthetic.domain_checks import DEFAULT_REGISTRY, run_checks
    item = {
        "subtype": "mcq_single",
        "stem": "As an AI, I would say the answer is 5.",
        "options": [
            {"label": "A", "text": "5", "is_correct": True},
            {"label": "B", "text": "6", "is_correct": False},
            {"label": "C", "text": "7", "is_correct": False},
            {"label": "D", "text": "8", "is_correct": False},
            {"label": "E", "text": "9", "is_correct": False},
        ],
    }
    res = run_checks("self-ref", item, DEFAULT_REGISTRY)
    assert res.passed is False
