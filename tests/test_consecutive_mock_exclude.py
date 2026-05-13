"""
Phase 1 R5: consecutive-mock qid exclude.

Mock N+1 must not repeat any qid that was assigned to the user's most-recent
COMPLETED full mock. In-progress or abandoned mocks do NOT count — the
learner never finished seeing them as a coherent batch.

These tests stub the question bank so we can assert exactly which ids the
assembler was told to exclude. The real ``QuestionBankService.select_questions_composed``
is exercised by other suites; here we want to pin the contract between
``ExamSession.build_full_mock`` and whatever bank it's given.
"""
from datetime import datetime, timedelta

import pytest

# temp_db fixture from tests/conftest.py is applied automatically.


class RecordingBank:
    """Captures every ``exclude_ids`` passed into S1 assembly."""

    def __init__(self, ids_to_return=None):
        self.calls = []
        self._ids_to_return = ids_to_return

    def select_questions_composed(self, measure, count, difficulty_band,
                                   exclude_ids=None, **kwargs):
        self.calls.append({
            "measure": measure,
            "count": count,
            "difficulty_band": difficulty_band,
            "exclude_ids": set(exclude_ids or []),
        })
        if self._ids_to_return is not None:
            return list(self._ids_to_return.get(measure, []))
        base = 1000 if measure == "verbal" else 2000
        return [base + i for i in range(count)]

    def select_questions(self, *args, **kwargs):
        return [99]

    def select_awa_prompt(self):
        return [1]


def _seed_one_question(qid, measure="verbal", subtype="tc"):
    """Create a minimal Question row (FKs in SectionResult/Response need real ids)."""
    from models.database import Question
    q, _ = Question.get_or_create(
        id=qid,
        defaults=dict(
            measure=measure, subtype=subtype,
            prompt=f"Q-{qid}", time_target_seconds=60,
            concept_tags="[]", explanation="",
            difficulty_target=3, status="live",
        ),
    )
    return q


def _create_mock_session(qids_by_section, *, state="completed",
                        ended_days_ago=1):
    """Create a Session + SectionResult rows representing one mock.

    ``qids_by_section`` maps section_name ("verbal_s1", "quant_s2", ...) to
    a list of qid ints that were assigned to that section.
    """
    import json
    from models.database import Session, SectionResult
    ts = datetime.now() - timedelta(days=ended_days_ago)
    sess = Session.create(
        test_type="full_mock", mode="simulation",
        section_order=json.dumps(list(qids_by_section.keys())),
        current_section_index=0,
        state=state,
        started_at=ts - timedelta(hours=2),
        ended_at=ts if state == "completed" else None,
    )
    for sec_name, qids in qids_by_section.items():
        measure = sec_name.split("_")[0]
        sec_idx = 2 if sec_name.endswith("_s2") else 1
        # Seed Question rows so the qids are referenceable (SectionResult
        # doesn't FK to Question directly, but a downstream reader might).
        for qid in qids:
            _seed_one_question(qid, measure=measure if measure != "awa" else "verbal")
        SectionResult.create(
            session=sess,
            section_name=sec_name,
            measure=measure if measure != "awa" else "awa",
            section_index=sec_idx,
            time_limit_seconds=1080,
            question_ids=json.dumps(list(qids)),
        )
    return sess


# ── (a) No prior mock → exclude empty, no crash ───────────────────────

def test_get_previous_mock_qids_with_no_history(temp_db):
    from models.exam_session import get_previous_mock_qids
    assert get_previous_mock_qids("local") == set()


def test_build_full_mock_no_prior_mock_does_not_crash(temp_db):
    """A brand-new user starting their first mock should assemble cleanly
    with no pre-existing exclusions to merge in."""
    from models.exam_session import ExamSession

    bank = RecordingBank()
    exam = ExamSession(test_type="full_mock", mode="simulation")
    exam.build_full_mock(bank)

    # At least one S1 section was assembled (AWA doesn't call the bank
    # via select_questions_composed).
    assert bank.calls, "expected at least one section-assembly call"
    # The first S1 call must see an empty exclude set (no prior mock).
    first_s1 = bank.calls[0]
    assert first_s1["exclude_ids"] == set()


# ── (b) After a completed mock, next mock's picks exclude those qids ──

def test_previous_completed_mock_qids_are_excluded(temp_db):
    """Prior completed mock had qids {A, B, C, ...}; the next mock's S1
    assembly call must receive all of them in ``exclude_ids``."""
    from models.exam_session import ExamSession, get_previous_mock_qids

    prev_verbal = [101, 102, 103, 104]
    prev_quant = [201, 202, 203]
    _create_mock_session({
        "awa": [],
        "verbal_s1": prev_verbal[:2],
        "verbal_s2": prev_verbal[2:],
        "quant_s1": prev_quant[:2],
        "quant_s2": prev_quant[2:],
    }, state="completed", ended_days_ago=1)

    expected = set(prev_verbal) | set(prev_quant)
    assert get_previous_mock_qids("local") == expected

    # Build mock N+1 and confirm the exclude_ids passed into every S1
    # call is a superset of the prior qid set.
    bank = RecordingBank()
    exam = ExamSession(test_type="full_mock", mode="simulation")
    exam.build_full_mock(bank)

    for call in bank.calls:
        assert expected.issubset(call["exclude_ids"]), (
            f"section {call['measure']} S1 excluded {call['exclude_ids']} "
            f"but expected to include {expected}")

    # None of the picks returned should overlap with the prior mock
    # (RecordingBank returns 1000+ / 2000+ ids, so this is trivially
    # true — but the assertion also covers the shape of the S2 pipeline
    # when we simulate adaptation below).
    for sec in exam.sections.values():
        overlap = set(sec.question_ids) & expected
        assert not overlap, f"mock N+1 reused prior-mock qids: {overlap}"


def test_previous_completed_mock_qids_excluded_in_s2_assembly(temp_db):
    """S2 is loaded lazily after S1 adapts. The prev-mock exclude set must
    propagate into that deferred call too."""
    from models.exam_session import ExamSession, SectionType

    prev_verbal = [501, 502, 503]
    _create_mock_session({
        "verbal_s1": prev_verbal[:2],
        "verbal_s2": prev_verbal[2:],
    }, state="completed", ended_days_ago=1)
    expected = set(prev_verbal)

    bank = RecordingBank()
    exam = ExamSession(test_type="full_mock", mode="simulation")
    exam.build_full_mock(bank)

    # Fake a completed S1 so _adapt_next_section loads S2.
    for s1_type in (SectionType.VERBAL_S1, SectionType.QUANT_S1):
        if s1_type in exam.sections:
            s1 = exam.sections[s1_type]
            s1._correctness = {qid: True for qid in s1.question_ids}
            exam._adapt_next_section(s1_type)

    # Every call — S1 and S2 — must have the prior mock qids in its exclude set.
    assert len(bank.calls) >= 2, "expected at least S1+S2 calls"
    for call in bank.calls:
        assert expected.issubset(call["exclude_ids"]), (
            f"call exclude_ids={call['exclude_ids']} missing prev mock qids "
            f"{expected - call['exclude_ids']}")


# ── (c) Abandoned mock doesn't count ──────────────────────────────────

def test_abandoned_mock_does_not_count_as_previous(temp_db):
    """An abandoned (unfinished) mock must NOT contribute to the
    exclude set — the user never saw it as a complete batch."""
    from models.exam_session import get_previous_mock_qids

    _create_mock_session({
        "verbal_s1": [301, 302],
    }, state="abandoned", ended_days_ago=1)

    assert get_previous_mock_qids("local") == set()


def test_in_progress_mock_does_not_count_as_previous(temp_db):
    """Similarly, a mock still in progress must not leak into the
    exclude set for some other mock."""
    from models.exam_session import get_previous_mock_qids

    _create_mock_session({
        "verbal_s1": [401, 402],
    }, state="in_progress", ended_days_ago=0)

    assert get_previous_mock_qids("local") == set()


def test_completed_mock_wins_over_older_completed_mock(temp_db):
    """When multiple completed mocks exist, only the MOST RECENT one is
    used as the exclude set (not the union of all history — that's R3's
    ServedLog)."""
    from models.exam_session import get_previous_mock_qids

    _create_mock_session({
        "verbal_s1": [111, 112],
    }, state="completed", ended_days_ago=10)
    _create_mock_session({
        "verbal_s1": [221, 222],
    }, state="completed", ended_days_ago=1)

    assert get_previous_mock_qids("local") == {221, 222}
