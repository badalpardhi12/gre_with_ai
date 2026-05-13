"""
Tests for scripts/generate_quant_items.py — Phase 4 · D6.

All LLM calls are mocked; these tests never touch the network. We
exercise:
  • Stage 2 sympy verifier matches 2+2=4 and rejects 2+2=5.
  • Graceful skip when sympy can't evaluate the expression.
  • Multi-judge gate: 2 accepts → upserted; 1 accept + 1 reject → rejected.
  • Acceptance-rate logging via RunStats.
  • --dry-run doesn't touch the DB.
  • --count 0 init smoke test.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure `scripts.` is importable as a package (scripts/ has no __init__.py).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import generate_quant_items as gqi  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────

def _generator_payload(
    *,
    stem="If x + 2 = 4, what is x?",
    options=("A) 1", "B) 2", "C) 3", "D) 4", "E) 5"),
    correct="B",
    verifier="2 + 0",
    solution="Subtract 2 from both sides.",
):
    return {
        "stem": stem,
        "options": list(options),
        "correct_answer": correct,
        "solution_work": solution,
        "mathematical_expression_for_verifier": verifier,
    }


def _judge_payload(accept=True, score=5, reason="looks good"):
    return {"accept": accept, "score": score, "reason": reason}


def _mock_llm(responses):
    """Build a MagicMock llm whose generate_json yields ``responses`` in
    order and raises when exhausted."""
    llm = MagicMock()
    iterator = iter(responses)

    def _next(*_args, **_kwargs):
        try:
            return next(iterator)
        except StopIteration:
            raise AssertionError("generate_json called more times than expected")

    llm.generate_json.side_effect = _next
    return llm


# ── Stage 2: sympy solver ─────────────────────────────────────────────

def test_solver_accepts_matching_expression():
    """2+2=4 — expression '2+2' against numeric correct_answer 4 should pass."""
    item = gqi.GeneratedItem(
        subtype="numeric_entry",
        difficulty="easy",
        topic="arithmetic",
        stem="What is 2+2?",
        correct_answer=4,
        solution_work="",
        verifier_expr="2 + 2",
        numeric_answer=4,
    )
    result = gqi.verify_with_sympy(item)
    assert result.ok is True
    assert result.reason == "pass"
    assert result.solver_value == pytest.approx(4.0)


def test_solver_rejects_mismatched_expression():
    """2+2=5 — expression '2+2' against numeric correct_answer 5 should reject."""
    item = gqi.GeneratedItem(
        subtype="numeric_entry",
        difficulty="easy",
        topic="arithmetic",
        stem="What is 2+2?",
        correct_answer=5,
        solution_work="",
        verifier_expr="2 + 2",
        numeric_answer=5,
    )
    result = gqi.verify_with_sympy(item)
    assert result.ok is False
    assert "solver disagreement" in result.reason


def test_solver_handles_non_evaluable_expression_gracefully():
    """If sympy can't evaluate the expression we reject with 'solver
    disagreement' rather than raising."""
    item = gqi.GeneratedItem(
        subtype="numeric_entry",
        difficulty="easy",
        topic="arithmetic",
        stem="",
        correct_answer=4,
        solution_work="",
        verifier_expr="unparseable ))) (((",
        numeric_answer=4,
    )
    result = gqi.verify_with_sympy(item)
    assert result.ok is False
    assert "solver disagreement" in result.reason


def test_solver_resolves_mcq_option_numeric_value():
    """For MCQ with correct='B' and options like 'B) 17', the solver
    should compare the expression against 17."""
    item = gqi.GeneratedItem(
        subtype="mcq_single",
        difficulty="medium",
        topic="algebra",
        stem="solve …",
        correct_answer="B",
        solution_work="",
        verifier_expr="10 + 7",
        options=["A) 10", "B) 17", "C) 20", "D) 23", "E) 30"],
    )
    result = gqi.verify_with_sympy(item)
    assert result.ok is True


# ── Stage 3: judges ───────────────────────────────────────────────────

def test_judges_pass_requires_both_accept():
    """Two accepts, scores (5,4) → pass (both ≥ 4)."""
    verdicts = [
        gqi.JudgeVerdict("opus-4.7", True, 5, ""),
        gqi.JudgeVerdict("gemini-3-pro", True, 4, ""),
    ]
    assert gqi.judges_pass(verdicts) is True


def test_judges_pass_one_five_one_three():
    """One ≥ 5 and the other ≥ 3 is the minimum split-accept rule."""
    verdicts = [
        gqi.JudgeVerdict("opus-4.7", True, 5, ""),
        gqi.JudgeVerdict("gemini-3-pro", True, 3, ""),
    ]
    assert gqi.judges_pass(verdicts) is True


def test_judges_fail_if_any_rejects():
    """One accept + one reject → fail."""
    verdicts = [
        gqi.JudgeVerdict("opus-4.7", True, 5, ""),
        gqi.JudgeVerdict("gemini-3-pro", False, 2, "ambiguous"),
    ]
    assert gqi.judges_pass(verdicts) is False


def test_judges_fail_both_low_scores():
    """Both accept but scores are (4,3) → 3<4 fails both-≥4 and 4<5 fails
    the split-accept fallback."""
    verdicts = [
        gqi.JudgeVerdict("opus-4.7", True, 4, ""),
        gqi.JudgeVerdict("gemini-3-pro", True, 3, ""),
    ]
    assert gqi.judges_pass(verdicts) is False


# ── End-to-end pipeline (mocked) ──────────────────────────────────────

def test_pipeline_accepts_when_solver_and_judges_pass():
    llm = _mock_llm([
        _generator_payload(verifier="1 + 1", correct="B",
                           options=("A) 1", "B) 2", "C) 3", "D) 4", "E) 5")),
        _judge_payload(accept=True, score=5),
        _judge_payload(accept=True, score=4),
    ])
    res = gqi.run_pipeline_once(llm, "mcq_single", "medium", topic="algebra")
    assert res.accepted is True
    assert res.solver.ok is True
    assert len(res.verdicts) == 2


def test_pipeline_rejects_on_solver_disagreement():
    """Solver mismatch short-circuits — judges are never called."""
    llm = _mock_llm([
        _generator_payload(verifier="1 + 1", correct="B",
                           options=("A) 1", "B) 3", "C) 5",
                                    "D) 7", "E) 9")),
        # no judge payloads queued — we should not consume any
    ])
    res = gqi.run_pipeline_once(llm, "mcq_single", "medium", topic="algebra")
    assert res.accepted is False
    assert res.solver is not None and res.solver.ok is False
    assert "solver disagreement" in res.rejection_reason


def test_pipeline_rejects_on_judge_split():
    llm = _mock_llm([
        _generator_payload(verifier="1 + 1", correct="B",
                           options=("A) 1", "B) 2", "C) 3", "D) 4", "E) 5")),
        _judge_payload(accept=True, score=5),
        _judge_payload(accept=False, score=2, reason="ambiguous"),
    ])
    res = gqi.run_pipeline_once(llm, "mcq_single", "medium", topic="algebra")
    assert res.accepted is False
    assert res.rejection_reason == "judge panel rejected"


# ── RunStats + acceptance rate ────────────────────────────────────────

def test_run_stats_acceptance_rate_logging():
    stats = gqi.RunStats()
    stats.attempted = 5
    stats.accepted = 4
    stats.upserted = 3
    stats.duplicates = 1
    stats.solver_failed = 1
    d = stats.as_dict()
    assert d["attempted"] == 5
    assert d["accepted"] == 4
    assert d["acceptance_rate"] == 0.8


def test_run_batch_count_zero_no_op():
    """count=0 must return an empty RunStats and never call the LLM."""
    llm = MagicMock()
    llm.generate_json.side_effect = AssertionError(
        "generate_json must not be called when count=0")
    stats = gqi.run_batch(llm, count=0, subtype="mcq_single",
                          difficulty="medium", dry_run=True)
    assert stats.attempted == 0
    assert stats.accepted == 0
    assert llm.generate_json.call_count == 0


# ── DB upsert (real sqlite via temp_db fixture) ───────────────────────

def test_upsert_writes_accepted_mcq(temp_db):
    from models.database import Question, QuestionOption

    item = gqi.GeneratedItem(
        subtype="mcq_single",
        difficulty="medium",
        topic="algebra",
        stem="If x + 2 = 4, x = ?",
        correct_answer="B",
        solution_work="Subtract 2.",
        verifier_expr="4 - 2",
        options=["A) 1", "B) 2", "C) 3", "D) 4", "E) 5"],
    )
    solver = gqi.SolverResult(True, "pass", solver_value=2.0, expected_value=2.0)
    verdicts = [
        gqi.JudgeVerdict("opus-4.7", True, 5, "clean"),
        gqi.JudgeVerdict("gemini-3-pro", True, 4, "fine"),
    ]
    result = gqi.PipelineResult(item=item, solver=solver, verdicts=verdicts,
                                accepted=True, generator_model="opus-4.7")

    qid = gqi.upsert_accepted(result)
    assert qid is not None

    q = Question.get(Question.id == qid)
    assert q.source == gqi.SOURCE_TAG
    assert q.status == "candidate"
    assert q.provenance == "llm_generated"
    assert q.measure == "quant"
    assert q.subtype == "mcq_single"

    prov = q.get_provenance()
    assert prov["pipeline"] == "quant_gen_v2"
    assert prov["solver_check"] == "pass"
    assert len(prov["judges"]) == 2

    opts = list(QuestionOption.select().where(QuestionOption.question == q))
    assert len(opts) == 5
    correct = [o for o in opts if o.is_correct]
    assert len(correct) == 1
    assert correct[0].option_label == "B"


def test_upsert_is_idempotent(temp_db):
    """Re-calling upsert with the same item returns None the second time."""
    item = gqi.GeneratedItem(
        subtype="mcq_single",
        difficulty="medium",
        topic="algebra",
        stem="Same stem",
        correct_answer="A",
        solution_work="",
        verifier_expr="1",
        options=["A) 1", "B) 2", "C) 3", "D) 4", "E) 5"],
    )
    solver = gqi.SolverResult(True, "pass", solver_value=1.0, expected_value=1.0)
    verdicts = [
        gqi.JudgeVerdict("opus-4.7", True, 5, ""),
        gqi.JudgeVerdict("gemini-3-pro", True, 4, ""),
    ]
    result = gqi.PipelineResult(item=item, solver=solver, verdicts=verdicts,
                                accepted=True, generator_model="opus-4.7")
    first = gqi.upsert_accepted(result)
    second = gqi.upsert_accepted(result)
    assert first is not None
    assert second is None


def test_upsert_writes_numeric_entry(temp_db):
    from models.database import Question, NumericAnswer

    item = gqi.GeneratedItem(
        subtype="numeric_entry",
        difficulty="easy",
        topic="arithmetic",
        stem="2 + 2 = ?",
        correct_answer=4,
        solution_work="",
        verifier_expr="2 + 2",
        numeric_answer=4,
    )
    solver = gqi.SolverResult(True, "pass", solver_value=4.0, expected_value=4.0)
    verdicts = [
        gqi.JudgeVerdict("opus-4.7", True, 5, ""),
        gqi.JudgeVerdict("gemini-3-pro", True, 4, ""),
    ]
    result = gqi.PipelineResult(item=item, solver=solver, verdicts=verdicts,
                                accepted=True, generator_model="opus-4.7")
    qid = gqi.upsert_accepted(result)
    assert qid is not None
    q = Question.get(Question.id == qid)
    na = list(NumericAnswer.select().where(NumericAnswer.question == q))
    assert len(na) == 1
    assert na[0].exact_value == pytest.approx(4.0)


# ── End-to-end run_batch (mocked LLM, real DB) ────────────────────────

def test_run_batch_dry_run_does_not_touch_db(temp_db, monkeypatch):
    """--dry-run path: even accepted items must not land in the DB."""
    from models.database import Question

    # Poison init_db + connect so a dry-run that tries to open the DB
    # would fail loudly — proves we don't go near DB code.
    import models.database as md
    monkeypatch.setattr(md, "init_db",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("init_db must not be called in dry-run")))

    llm = _mock_llm([
        _generator_payload(verifier="1 + 1", correct="B",
                           options=("A) 1", "B) 2", "C) 3", "D) 4", "E) 5")),
        _judge_payload(accept=True, score=5),
        _judge_payload(accept=True, score=4),
    ])
    stats = gqi.run_batch(llm, count=1, subtype="mcq_single",
                          difficulty="medium", dry_run=True)
    assert stats.accepted == 1
    assert stats.upserted == 0
    # DB must still have zero questions
    assert Question.select().count() == 0


def test_run_batch_real_upsert_and_acceptance_logging(temp_db):
    """One pipeline iteration, accepted, upserted, stats reflect it."""
    from models.database import Question

    llm = _mock_llm([
        _generator_payload(
            stem="If x + 2 = 4, x = ?",
            verifier="4 - 2",
            correct="B",
            options=("A) 1", "B) 2", "C) 3", "D) 4", "E) 5"),
        ),
        _judge_payload(accept=True, score=5),
        _judge_payload(accept=True, score=4),
    ])
    stats = gqi.run_batch(llm, count=1, subtype="mcq_single",
                          difficulty="medium", dry_run=False)
    assert stats.attempted == 1
    assert stats.accepted == 1
    assert stats.upserted == 1
    assert stats.acceptance_rate() == 1.0
    assert Question.select().where(
        Question.source == gqi.SOURCE_TAG).count() == 1


def test_run_batch_mixed_outcomes_tracked(temp_db):
    """Three iterations: accept, solver-fail, judge-reject. Stats should
    reflect each bucket and only 1 gets upserted."""
    from models.database import Question

    llm = _mock_llm([
        # Iter 1 — accepted
        _generator_payload(verifier="1 + 1", correct="B",
                           options=("A) 1", "B) 2", "C) 3", "D) 4", "E) 5")),
        _judge_payload(accept=True, score=5),
        _judge_payload(accept=True, score=4),
        # Iter 2 — solver mismatch (expression evaluates to 2, but correct=C=3)
        _generator_payload(verifier="1 + 1", correct="C",
                           options=("A) 1", "B) 2", "C) 3", "D) 4", "E) 5")),
        # Iter 3 — solver passes, judges split
        _generator_payload(
            stem="Different stem — judge split",
            verifier="1 + 1", correct="B",
            options=("A) 1", "B) 2", "C) 3", "D) 4", "E) 5"),
        ),
        _judge_payload(accept=True, score=5),
        _judge_payload(accept=False, score=2, reason="off"),
    ])
    stats = gqi.run_batch(llm, count=3, subtype="mcq_single",
                          difficulty="medium", dry_run=False)
    assert stats.attempted == 3
    assert stats.accepted == 1
    assert stats.solver_failed == 1
    assert stats.judge_rejected == 1
    assert stats.upserted == 1
    assert Question.select().where(
        Question.source == gqi.SOURCE_TAG).count() == 1
    assert stats.acceptance_rate() == pytest.approx(1 / 3)


# ── CLI surface ───────────────────────────────────────────────────────

def test_cli_count_zero_returns_ok_without_db_or_llm(capsys, monkeypatch):
    """`--count 0 --dry-run` is the init smoke-test; must exit 0 without
    touching the LLM facade or the DB."""
    # If the script tries to import llm_service, fail loudly.
    import services.llm_service as lls
    monkeypatch.setattr(lls, "llm_service", MagicMock(side_effect=AssertionError(
        "llm_service must not be used in --count 0 path")))

    rc = gqi.main(["--count", "0", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"count": 0' in out
    assert '"ok": true' in out


def test_cli_refuses_in_ci(monkeypatch):
    """The script must refuse to run against a real LLM inside CI.
    (--count 0 still works — it never reaches the CI check.)"""
    monkeypatch.setenv("CI", "1")
    with pytest.raises(SystemExit) as exc:
        gqi.main(["--count", "3", "--dry-run"])
    # SystemExit value is the refusal message (a string), not 0.
    assert "refuses to run in CI" in str(exc.value)


def test_cli_judges_parse():
    """--judges parser accepts comma-separated lists with ≥ 2 entries."""
    tup = gqi._parse_judges_arg("opus-4.7, gemini-3-pro")
    assert tup == ("opus-4.7", "gemini-3-pro")

    import argparse as _ap
    with pytest.raises(_ap.ArgumentTypeError):
        gqi._parse_judges_arg("only-one")
