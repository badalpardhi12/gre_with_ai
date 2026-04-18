"""
Tests for `models/exam_session.py` adaptive routing.

Focuses on the boundary semantics and the "no answers" defaulting that the
audit flagged. The exam session is constructed manually (no DB) by stubbing
the question bank.
"""
import pytest

from models.exam_session import (
    ExamSession, ExamState, SectionType, SECTION_META, SectionState,
)


class StubBank:
    """Minimal QuestionBank-like stub for tests."""

    def __init__(self):
        self.calls = []

    def select_questions_composed(self, measure, count, difficulty_band,
                                   exclude_ids=None):
        self.calls.append((measure, count, difficulty_band))
        # Return predictable IDs in the requested range, offset by measure to
        # avoid collisions.
        base = 1000 if measure == "verbal" else 2000
        return [base + i for i in range(count)]

    def select_questions(self, *args, **kwargs):
        return [99]

    def select_awa_prompt(self):
        return [1]


def _build_full_mock():
    exam = ExamSession(test_type="full_mock", mode="simulation")
    exam.build_full_mock(StubBank())
    return exam


# ── Section ordering ──

def test_full_mock_starts_with_awa():
    exam = _build_full_mock()
    assert exam.section_order[0] == SectionType.AWA


def test_full_mock_has_5_sections():
    exam = _build_full_mock()
    assert len(exam.section_order) == 5


# ── Adaptive boundary at exactly 40% / 70% ──
# Total questions = 12, so:
#   correct=4  -> pct=0.333 (< 0.40) -> easy
#   correct=5  -> pct=0.4167 (> 0.40, < 0.70) -> medium
#   correct=8  -> pct=0.667 (< 0.70) -> medium
#   correct=9  -> pct=0.75 (> 0.70) -> hard
# Exact 0.40 / 0.70 boundaries land in `medium` per the strict `<` / `>` semantics
# in `_adapt_next_section`. To exercise both edges of the boundary cleanly we
# parameterize over correct-counts.

@pytest.mark.parametrize("n_correct,expected_band", [
    (0, "easy"),
    (4, "easy"),       # 33% < 40% -> easy
    (5, "medium"),     # 41% inside 40-70 band -> medium
    (8, "medium"),     # 66% < 70% -> medium
    (9, "hard"),       # 75% > 70% -> hard
    (12, "hard"),
])
def test_adapt_boundary(n_correct, expected_band):
    exam = _build_full_mock()
    s1 = exam.sections[SectionType.VERBAL_S1]
    s1.question_ids = list(range(12))
    s1._correctness = {qid: i < n_correct for i, qid in enumerate(s1.question_ids)}
    exam._adapt_next_section(SectionType.VERBAL_S1)
    s2 = exam.sections[SectionType.VERBAL_S2]
    assert s2.difficulty_band == expected_band


def test_adapt_with_zero_answers_defaults_medium_not_easy():
    """REGRESSION: previously fell through to easy because empty correctness
    map gave pct=0.0 < 0.40 → easy."""
    exam = _build_full_mock()
    s1 = exam.sections[SectionType.VERBAL_S1]
    s1.question_ids = list(range(12))
    s1._correctness = {}  # nothing answered
    exam._adapt_next_section(SectionType.VERBAL_S1)
    s2 = exam.sections[SectionType.VERBAL_S2]
    assert s2.difficulty_band == "medium"


# ── Section navigation ──

def test_advance_section_returns_false_at_end():
    exam = _build_full_mock()
    exam.start()
    for _ in range(len(exam.section_order) - 1):
        assert exam.advance_section() is True
    assert exam.advance_section() is False
    assert exam.state == ExamState.COMPLETED


def test_section_state_navigate_bounds():
    s = SectionState(SectionType.VERBAL_S1, [1, 2, 3], 60)
    assert s.navigate_to(0) is True
    assert s.navigate_to(2) is True
    assert s.navigate_to(3) is False
    assert s.navigate_to(-1) is False


def test_section_state_count_answered():
    s = SectionState(SectionType.VERBAL_S1, [10, 11, 12], 60)
    s.set_response(10, {"selected": ["A"]})
    s.set_response(11, {})  # empty -> not answered
    assert s.count_answered() == 1


def test_section_state_tick_returns_true_on_expiry():
    s = SectionState(SectionType.VERBAL_S1, [1], 2)
    assert s.tick(1) is False
    assert s.tick(1) is True   # boundary: time hits zero


# ── Section build for non-mock test types ──

def test_build_drill():
    exam = ExamSession(test_type="drill", mode="learning")
    exam.build_drill(measure="verbal", topic="rc_inference", count=10,
                     question_bank=StubBank())
    assert SectionType.VERBAL_S1 in exam.sections
    assert exam.sections[SectionType.VERBAL_S1].time_limit == 10 * 90
