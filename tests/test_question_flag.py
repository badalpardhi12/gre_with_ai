"""
Tests for QuestionFlag persistence + auto-retire threshold.
"""
import pytest


@pytest.fixture
def sample_question(temp_db):
    """Insert a single live question we can flag."""
    from models.database import Question
    q = Question.create(
        measure="verbal",
        subtype="mcq_single",
        prompt="Sample prompt for testing.",
        explanation="",
        status="live",
    )
    return q.id


def test_flag_question_creates_row(temp_db, sample_question):
    from services.question_bank import flag_question
    from models.database import QuestionFlag

    ok = flag_question(sample_question, "wrong_answer", note="answer is C not B")
    assert ok is True

    rows = list(QuestionFlag.select().where(QuestionFlag.question_id == sample_question))
    assert len(rows) == 1
    assert rows[0].reason == "wrong_answer"
    assert rows[0].note == "answer is C not B"
    assert rows[0].user_id == "local"


def test_flag_question_idempotent_per_reason(temp_db, sample_question):
    """Re-clicking the same reason doesn't duplicate a row."""
    from services.question_bank import flag_question
    from models.database import QuestionFlag

    flag_question(sample_question, "wrong_answer")
    flag_question(sample_question, "wrong_answer")
    flag_question(sample_question, "wrong_answer", note="now with detail")

    rows = list(QuestionFlag.select().where(QuestionFlag.question_id == sample_question))
    assert len(rows) == 1
    # Note got updated on the third call
    assert rows[0].note == "now with detail"


def test_flag_question_distinct_reasons_each_get_a_row(temp_db, sample_question):
    from services.question_bank import flag_question
    from models.database import QuestionFlag

    flag_question(sample_question, "wrong_answer")
    flag_question(sample_question, "wrong_explanation")
    flag_question(sample_question, "doesnt_make_sense")

    assert QuestionFlag.select().where(
        QuestionFlag.question_id == sample_question
    ).count() == 3


def test_flag_question_invalid_reason_returns_false(temp_db, sample_question):
    from services.question_bank import flag_question
    from models.database import QuestionFlag

    assert flag_question(sample_question, "not_a_reason") is False
    assert QuestionFlag.select().count() == 0


def test_flag_question_missing_question_returns_false(temp_db):
    from services.question_bank import flag_question
    assert flag_question(99999, "wrong_answer") is False


def test_auto_retire_below_threshold_keeps_live(temp_db, sample_question):
    """1 flag from one user shouldn't retire the question."""
    from services.question_bank import flag_question, auto_retire_flagged_questions
    from models.database import Question

    flag_question(sample_question, "wrong_answer")
    retired = auto_retire_flagged_questions(threshold=3, single_user_threshold=3)
    assert retired == []
    q = Question.get_by_id(sample_question)
    assert q.status == "live"


def test_auto_retire_three_distinct_users_retires(temp_db, sample_question):
    from services.question_bank import flag_question, auto_retire_flagged_questions
    from models.database import Question

    flag_question(sample_question, "wrong_answer", user_id="alice")
    flag_question(sample_question, "wrong_answer", user_id="bob")
    flag_question(sample_question, "wrong_answer", user_id="carol")

    retired = auto_retire_flagged_questions(threshold=3, single_user_threshold=10)
    assert sample_question in retired
    assert Question.get_by_id(sample_question).status == "retired"


def test_auto_retire_three_distinct_reasons_one_user_retires(temp_db, sample_question):
    """One user submitting 3+ distinct reasons should also retire."""
    from services.question_bank import flag_question, auto_retire_flagged_questions
    from models.database import Question

    flag_question(sample_question, "wrong_answer")
    flag_question(sample_question, "wrong_explanation")
    flag_question(sample_question, "doesnt_make_sense")

    retired = auto_retire_flagged_questions(threshold=10, single_user_threshold=1)
    # 3 rows from one user → trip the per-row gate (single_user_threshold + 2 = 3)
    assert sample_question in retired
    assert Question.get_by_id(sample_question).status == "retired"


def test_auto_retire_idempotent(temp_db, sample_question):
    """Already-retired questions stay retired and don't reappear in the list."""
    from services.question_bank import flag_question, auto_retire_flagged_questions

    for u in ("a", "b", "c"):
        flag_question(sample_question, "wrong_answer", user_id=u)

    first = auto_retire_flagged_questions(threshold=3, single_user_threshold=10)
    second = auto_retire_flagged_questions(threshold=3, single_user_threshold=10)
    assert first == [sample_question]
    assert second == []


def test_get_user_flag_for(temp_db, sample_question):
    from services.question_bank import flag_question, get_user_flag_for

    assert get_user_flag_for(sample_question) is None
    flag_question(sample_question, "wrong_answer")
    flag = get_user_flag_for(sample_question)
    assert flag is not None
    assert flag.reason == "wrong_answer"
