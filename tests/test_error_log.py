"""
P2.E1 — Error-log screen tests.

Covers:
  * classify_single across all four categories.
  * list_errors returns rows newest-first with working subtype / date filters.
  * error_category_distribution aggregates by subtype.
  * Empty-data safe (no rows, no crash).
  * ErrorLogScreen constructor can import and instantiate without wx errors
    (imports only; we don't run the wx main loop in pytest).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


# ── Helpers ──────────────────────────────────────────────────────────


def _make_question(measure="quant", subtype="mcq_single", subtopic="algebra",
                   time_target=90, correct_label="A"):
    from models.database import Question, QuestionOption
    q = Question.create(
        measure=measure,
        subtype=subtype,
        prompt=("This is a sample prompt. " * 10)[:300],
        difficulty_target=3,
        time_target_seconds=time_target,
        concept_tags="[]",
        topic="algebra_basics",
        subtopic=subtopic,
        explanation="An explanation.",
        status="live",
    )
    # Two options, one correct.
    QuestionOption.create(question=q, option_label="A",
                          option_text="right answer",
                          is_correct=(correct_label == "A"))
    QuestionOption.create(question=q, option_label="B",
                          option_text="wrong answer",
                          is_correct=(correct_label == "B"))
    return q


def _make_session_and_section():
    from models.database import Session, SectionResult
    s = Session.create(
        test_type="drill", mode="learning", state="completed",
        started_at=datetime.now(), section_order="[]",
    )
    sr = SectionResult.create(
        session=s, section_name="verbal_s1", measure="verbal",
        section_index=1, difficulty_band="medium",
        time_limit_seconds=600, time_used_seconds=300,
    )
    return s, sr


def _make_response(question, is_correct=False, time_ms=10_000,
                   created_at=None, selected="B"):
    from models.database import Response
    s, sr = _make_session_and_section()
    r = Response.create(
        session=s, section_result=sr, question=question,
        response_payload=f'{{"selected": ["{selected}"]}}',
        is_correct=is_correct,
        time_spent_seconds=int(time_ms / 1000),
        time_to_answer_ms=time_ms,
        created_at=created_at or datetime.now(),
    )
    return r


# ── classify_single ─────────────────────────────────────────────────


def test_classify_single_timing(temp_db):
    """time_to_answer_ms > 1.5 × target ⇒ timing."""
    from services.mistake_coach import classify_single
    q = _make_question(time_target=60)  # 1.5× = 90 000 ms
    r = _make_response(q, time_ms=120_000)  # well over
    assert classify_single(r) == "timing"


def test_classify_single_vocab_gap(temp_db):
    """Verbal TC/SE wrong answer in normal time ⇒ vocab_gap."""
    from services.mistake_coach import classify_single
    q = _make_question(measure="verbal", subtype="text_completion",
                       subtopic="rc_inference", time_target=60)
    r = _make_response(q, time_ms=45_000)  # within time budget
    assert classify_single(r) == "vocab_gap"

    q2 = _make_question(measure="verbal", subtype="sentence_equiv",
                        subtopic="se_basics", time_target=60)
    r2 = _make_response(q2, time_ms=50_000)
    assert classify_single(r2) == "vocab_gap"


def test_classify_single_careless_by_mastery(temp_db):
    """High mastery on the subtopic ⇒ careless."""
    from services.mistake_coach import classify_single
    from services.mastery import update_mastery
    q = _make_question(subtopic="ratio_proportion", time_target=90)
    # Boost mastery to > 0.7 via a few easy correct answers.
    for _ in range(6):
        update_mastery("ratio_proportion", True, 2)
    r = _make_response(q, time_ms=60_000)  # reasonable window
    assert classify_single(r) == "careless"


def test_classify_single_careless_by_fast_click(temp_db):
    """Answered in <5s ⇒ careless (distracted click)."""
    from services.mistake_coach import classify_single
    q = _make_question(time_target=120)
    r = _make_response(q, time_ms=3_000)
    assert classify_single(r) == "careless"


def test_classify_single_conceptual_default(temp_db):
    """Reasonable time, low mastery, quant MCQ ⇒ conceptual."""
    from services.mistake_coach import classify_single
    q = _make_question(measure="quant", subtype="mcq_single",
                       subtopic="geometry_novel", time_target=90)
    r = _make_response(q, time_ms=80_000)
    assert classify_single(r) == "conceptual"


def test_classify_single_missing_question_safe(temp_db):
    """A Response with no question attribute must not raise."""
    from services.mistake_coach import classify_single

    class Stub:
        question = None
        time_to_answer_ms = None
        time_spent_seconds = 0
    assert classify_single(Stub()) == "conceptual"


# ── list_errors ─────────────────────────────────────────────────────


def test_list_errors_sorted_newest_first(temp_db):
    from services.mistake_coach import list_errors
    q = _make_question(subtype="mcq_single")
    old = _make_response(q, time_ms=50_000,
                         created_at=datetime.now() - timedelta(days=5))
    new = _make_response(q, time_ms=50_000,
                         created_at=datetime.now() - timedelta(hours=1))

    rows = list_errors()
    assert len(rows) == 2
    assert rows[0]["response_id"] == new.id
    assert rows[1]["response_id"] == old.id


def test_list_errors_filter_by_subtype(temp_db):
    from services.mistake_coach import list_errors
    q_mcq = _make_question(subtype="mcq_single")
    q_tc = _make_question(measure="verbal", subtype="text_completion",
                          subtopic="tc_basics")
    _make_response(q_mcq)
    _make_response(q_tc)
    _make_response(q_tc)

    tc_only = list_errors(subtype="text_completion")
    assert len(tc_only) == 2
    assert all(r["subtype"] == "text_completion" for r in tc_only)

    mcq_only = list_errors(subtype="mcq_single")
    assert len(mcq_only) == 1


def test_list_errors_filter_by_date(temp_db):
    from services.mistake_coach import list_errors
    q = _make_question()
    _make_response(q, created_at=datetime.now() - timedelta(days=40))
    _make_response(q, created_at=datetime.now() - timedelta(days=5))
    _make_response(q, created_at=datetime.now() - timedelta(hours=2))

    last_7 = list_errors(since_days=7)
    assert len(last_7) == 2

    last_30 = list_errors(since_days=30)
    assert len(last_30) == 2

    all_time = list_errors()
    assert len(all_time) == 3


def test_list_errors_empty_safe(temp_db):
    from services.mistake_coach import list_errors
    # No questions, no responses.
    assert list_errors() == []
    assert list_errors(subtype="mcq_single", since_days=7) == []


def test_list_errors_skips_correct(temp_db):
    """Only wrong answers appear in the error log."""
    from services.mistake_coach import list_errors
    q = _make_question()
    _make_response(q, is_correct=True, time_ms=50_000)
    _make_response(q, is_correct=False, time_ms=50_000)
    rows = list_errors()
    assert len(rows) == 1
    assert rows[0]["category"] in ("conceptual", "careless", "timing",
                                   "vocab_gap")


# ── distribution ────────────────────────────────────────────────────


def test_error_category_distribution(temp_db):
    from services.mistake_coach import error_category_distribution
    q_tc = _make_question(measure="verbal", subtype="text_completion",
                          subtopic="tc_basics", time_target=60)
    q_mcq = _make_question(subtype="mcq_single", subtopic="geometry_misc",
                           time_target=60)
    # Two vocab_gap (TC errors answered within time).
    _make_response(q_tc, time_ms=40_000)
    _make_response(q_tc, time_ms=30_000)
    # One timing (>1.5× 60s).
    _make_response(q_mcq, time_ms=120_000)
    # One conceptual (reasonable time, low mastery).
    _make_response(q_mcq, time_ms=45_000)

    dist = error_category_distribution()
    assert dist["text_completion"]["vocab_gap"] == 2
    assert dist["mcq_single"]["timing"] == 1
    assert dist["mcq_single"]["conceptual"] == 1


def test_error_category_distribution_empty(temp_db):
    from services.mistake_coach import error_category_distribution
    assert error_category_distribution() == {}


# ── Screen import / construct ───────────────────────────────────────


def test_error_log_screen_imports(temp_db):
    """Can import and reference ErrorLogScreen without instantiating wx."""
    from screens import error_log_screen
    assert hasattr(error_log_screen, "ErrorLogScreen")
    assert hasattr(error_log_screen.ErrorLogScreen, "refresh")
    assert hasattr(error_log_screen.ErrorLogScreen, "set_handlers")
