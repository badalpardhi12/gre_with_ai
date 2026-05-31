"""
Past Tests screen tests — analytics helpers + screen-launch sanity.

Covers:
  * `get_past_session_summaries` returns one row per completed session,
    newest-first, skipping in-progress and abandoned sessions.
  * `build_session_question_details` returns the dict shape that
    `AnswerReviewDialog` expects.
  * `PastTestsScreen.refresh()` populates rows without crashing on a
    fresh DB and on a populated DB.
  * Empty-state shows when there are no completed sessions.
  * Row activation invokes the dialog with the right session details.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest import mock

import pytest


# ── Helpers ──────────────────────────────────────────────────────────


def _make_question(measure="quant", subtype="mcq_single",
                   prompt="Sample prompt", explanation="Sample explanation",
                   correct_label="A"):
    from models.database import Question, QuestionOption
    q = Question.create(
        measure=measure,
        subtype=subtype,
        prompt=prompt,
        difficulty_target=3,
        time_target_seconds=90,
        concept_tags="[]",
        topic="algebra_basics",
        subtopic="linear",
        explanation=explanation,
        status="live",
    )
    QuestionOption.create(question=q, option_label="A",
                          option_text="opt A",
                          is_correct=(correct_label == "A"))
    QuestionOption.create(question=q, option_label="B",
                          option_text="opt B",
                          is_correct=(correct_label == "B"))
    QuestionOption.create(question=q, option_label="C",
                          option_text="opt C",
                          is_correct=(correct_label == "C"))
    return q


def _make_session(state="completed", test_type="full_mock",
                  mode="simulation", started_at=None, ended_at=None,
                  created_at=None):
    from models.database import Session
    return Session.create(
        test_type=test_type,
        mode=mode,
        state=state,
        section_order="[]",
        started_at=started_at or datetime.now(),
        ended_at=ended_at,
        created_at=created_at or datetime.now(),
    )


def _make_section(session, section_name="verbal_s1", measure="verbal"):
    from models.database import SectionResult
    return SectionResult.create(
        session=session,
        section_name=section_name,
        measure=measure,
        section_index=1,
        difficulty_band="medium",
        time_limit_seconds=600,
        time_used_seconds=300,
    )


def _make_response(session, section, question, *, is_correct=True,
                   selected="A", marked=False):
    from models.database import Response
    return Response.create(
        session=session,
        section_result=section,
        question=question,
        response_payload=json.dumps({"selected": [selected]}),
        is_correct=is_correct,
        is_marked=marked,
        time_spent_seconds=42,
    )


def _make_scoring_result(session, *, verbal_low=150, verbal_high=158,
                         quant_low=160, quant_high=168, awa=4.5):
    from models.database import ScoringResult
    return ScoringResult.create(
        session=session,
        verbal_raw=8,
        quant_raw=10,
        verbal_estimated_low=verbal_low,
        verbal_estimated_high=verbal_high,
        quant_estimated_low=quant_low,
        quant_estimated_high=quant_high,
        awa_estimated=awa,
    )


# ── wx fixture ───────────────────────────────────────────────────────


@pytest.fixture
def wx_app():
    """Instantiate a wx.App once per test; skip on headless envs."""
    wx = pytest.importorskip("wx")
    try:
        app = wx.App(False)
    except SystemExit as exc:  # pragma: no cover — headless CI
        pytest.skip(f"wx.App cannot start: {exc}")
    except Exception as exc:  # pragma: no cover — headless CI
        pytest.skip(f"wx.App cannot start: {exc}")
    yield app


# ── get_past_session_summaries ───────────────────────────────────────


def test_summaries_empty_db_returns_empty_list(temp_db):
    from services.analytics import get_past_session_summaries
    assert get_past_session_summaries() == []


def test_summaries_returns_completed_only_newest_first(temp_db):
    """In-progress and abandoned sessions are excluded; rest sorted newest first."""
    from services.analytics import get_past_session_summaries

    q = _make_question()

    # 3 completed sessions at different times.
    older = _make_session(
        created_at=datetime.now() - timedelta(days=2),
        started_at=datetime.now() - timedelta(days=2),
        ended_at=datetime.now() - timedelta(days=2, hours=-1),
    )
    middle = _make_session(
        created_at=datetime.now() - timedelta(days=1),
        started_at=datetime.now() - timedelta(days=1),
        ended_at=datetime.now() - timedelta(days=1, hours=-1),
    )
    newest = _make_session(
        created_at=datetime.now(),
        started_at=datetime.now(),
        ended_at=datetime.now() + timedelta(minutes=30),
    )
    # An in-progress session (no ended_at, state=in_progress) — should
    # NOT appear in the list.
    _make_session(state="in_progress", created_at=datetime.now())
    # An abandoned session — should also NOT appear.
    _make_session(state="abandoned", created_at=datetime.now())

    # Each completed session needs a section + responses to make
    # accuracy stats meaningful.
    for s in (older, middle, newest):
        sec = _make_section(s)
        _make_response(s, sec, q, is_correct=True, selected="A")
        _make_response(s, sec, q, is_correct=False, selected="B")

    rows = get_past_session_summaries()
    assert [r["session_id"] for r in rows] == [
        newest.id, middle.id, older.id,
    ]
    # Each row carries n_questions / n_correct.
    for row in rows:
        assert row["n_questions"] == 2
        assert row["n_correct"] == 1
        assert row["accuracy"] == 0.5


def test_summaries_attaches_scoring_result_when_present(temp_db):
    from services.analytics import get_past_session_summaries

    s = _make_session()
    _make_section(s)
    _make_scoring_result(s, verbal_low=152, verbal_high=156,
                         quant_low=164, quant_high=168, awa=5.0)

    rows = get_past_session_summaries()
    assert len(rows) == 1
    scores = rows[0]["scores"]
    assert scores is not None
    assert scores["verbal_low"] == 152
    assert scores["verbal_high"] == 156
    assert scores["quant_low"] == 164
    assert scores["quant_high"] == 168
    assert scores["awa"] == 5.0


def test_summaries_handles_session_without_responses(temp_db):
    """A completed AWA-only session has zero responses; should not crash."""
    from services.analytics import get_past_session_summaries

    s = _make_session(test_type="section")
    rows = get_past_session_summaries()
    assert len(rows) == 1
    assert rows[0]["n_questions"] == 0
    assert rows[0]["n_correct"] == 0
    # Accuracy with no attempts is None — caller can render "—".
    assert rows[0]["accuracy"] is None


def test_summaries_respects_limit(temp_db):
    from services.analytics import get_past_session_summaries
    for _ in range(5):
        _make_session()
    rows = get_past_session_summaries(limit=3)
    assert len(rows) == 3


# ── build_session_question_details ───────────────────────────────────


def test_details_shape_matches_dialog_contract(temp_db):
    """Returned dicts carry the keys AnswerReviewDialog reads."""
    from services.analytics import build_session_question_details

    q1 = _make_question(prompt="What is 2+2?", correct_label="A",
                        explanation="Adding two and two yields four.")
    q2 = _make_question(measure="verbal", subtype="mcq_single",
                        prompt="Pick a synonym for 'big'.",
                        correct_label="B")

    s = _make_session()
    sec = _make_section(s)
    _make_response(s, sec, q1, is_correct=True, selected="A")
    _make_response(s, sec, q2, is_correct=False, selected="C")

    details = build_session_question_details(s.id)
    assert len(details) == 2

    keys = {
        "question_id", "measure", "subtype", "difficulty",
        "is_correct", "is_marked", "time_spent",
        "prompt", "options", "stimulus", "numeric_answer",
        "explanation", "user_response",
    }
    for d in details:
        assert keys.issubset(d.keys())

    # First detail mirrors q1 + correct answer.
    d1 = details[0]
    assert d1["question_id"] == q1.id
    assert d1["prompt"] == "What is 2+2?"
    assert d1["is_correct"] is True
    assert d1["user_response"] == {"selected": ["A"]}
    assert d1["explanation"] == "Adding two and two yields four."
    # Options carry is_correct flags.
    correct_opts = [o for o in d1["options"] if o["is_correct"]]
    assert len(correct_opts) == 1
    assert correct_opts[0]["label"] == "A"

    # Second detail records wrong answer.
    d2 = details[1]
    assert d2["is_correct"] is False
    assert d2["user_response"] == {"selected": ["C"]}


def test_details_handles_numeric_entry(temp_db):
    from services.analytics import build_session_question_details
    from models.database import Question, NumericAnswer

    q = Question.create(
        measure="quant", subtype="numeric_entry",
        prompt="Compute 5 × 3.",
        difficulty_target=2, status="live",
    )
    NumericAnswer.create(question=q, exact_value=15.0, tolerance=0.001,
                         mode="decimal")

    s = _make_session()
    sec = _make_section(s)
    from models.database import Response
    Response.create(
        session=s, section_result=sec, question=q,
        response_payload=json.dumps({"value": "15"}),
        is_correct=True, time_spent_seconds=5,
    )

    details = build_session_question_details(s.id)
    assert len(details) == 1
    assert details[0]["subtype"] == "numeric_entry"
    assert details[0]["numeric_answer"] is not None
    assert details[0]["numeric_answer"]["exact_value"] == 15.0


def test_details_returns_empty_for_unknown_session(temp_db):
    from services.analytics import build_session_question_details
    assert build_session_question_details(99999) == []


def test_details_handles_question_hard_deleted(temp_db):
    """If a question row is missing, surface a stub instead of crashing.

    The Response.question FK is `on_delete=CASCADE`, so a real `DELETE
    FROM question WHERE id=?` would also wipe the response. That said,
    `build_session_question_details` defends against the question
    being None on lookup (it could happen if the seed swap ever leaves
    a Response row pointing at a question that didn't make it across).
    Drive the defensive branch directly with a monkeypatch so the
    behaviour is exercised without fighting SQLite cascades.
    """
    from services import analytics
    from services.analytics import build_session_question_details

    q = _make_question()
    s = _make_session()
    sec = _make_section(s)
    _make_response(s, sec, q, is_correct=True)

    real_get_or_none = analytics.Question.get_or_none

    def _stub_get_or_none(*args, **kwargs):
        return None

    analytics.Question.get_or_none = _stub_get_or_none
    try:
        details = build_session_question_details(s.id)
    finally:
        analytics.Question.get_or_none = real_get_or_none

    assert len(details) == 1
    assert details[0]["question_id"] == q.id
    assert details[0]["measure"] == "unknown"
    assert "no longer in bank" in details[0]["prompt"]


# ── PastTestsScreen ──────────────────────────────────────────────────


def test_screen_launches_on_empty_db(wx_app, temp_db):
    """Empty state renders without crashing when there are no sessions."""
    import wx
    from screens.past_tests_screen import PastTestsScreen

    frame = wx.Frame(None)
    try:
        screen = PastTestsScreen(frame)
        screen.refresh()
        # ListCtrl is hidden, empty-state is shown.
        assert screen._list_ctrl.IsShown() is False
        assert screen._empty_state.IsShown() is True
        assert screen._review_btn.IsEnabled() is False
        screen.Destroy()
    finally:
        frame.Destroy()


def test_screen_populates_rows_for_completed_sessions(wx_app, temp_db):
    """Refresh wires one row per completed session into the list ctrl."""
    import wx
    from screens.past_tests_screen import PastTestsScreen

    q = _make_question()
    for _ in range(3):
        s = _make_session()
        sec = _make_section(s)
        _make_response(s, sec, q, is_correct=True, selected="A")

    frame = wx.Frame(None)
    try:
        screen = PastTestsScreen(frame)
        screen.refresh()
        assert screen._list_ctrl.IsShown() is True
        assert screen._empty_state.IsShown() is False
        assert screen._list_ctrl.GetItemCount() == 3
        assert len(screen._rows_cache) == 3
        screen.Destroy()
    finally:
        frame.Destroy()


def test_row_activation_opens_dialog_with_session_details(wx_app, temp_db):
    """Activating a row builds details for the right session and shows dialog."""
    import wx
    from screens.past_tests_screen import PastTestsScreen

    q = _make_question(prompt="Activated row prompt")
    s = _make_session()
    sec = _make_section(s)
    _make_response(s, sec, q, is_correct=True, selected="A")

    frame = wx.Frame(None)
    try:
        screen = PastTestsScreen(frame)
        screen.refresh()

        captured: dict = {}

        class _FakeDialog:
            def __init__(self, parent, details):
                captured["details"] = details
                captured["parent"] = parent

            def ShowModal(self):
                return wx.ID_OK

            def Destroy(self):
                pass

        with mock.patch(
            "screens.answer_review_dialog.AnswerReviewDialog", _FakeDialog,
        ):
            screen._open_review_for_row(0)

        assert "details" in captured
        # Single row = single response = single detail.
        assert len(captured["details"]) == 1
        assert captured["details"][0]["question_id"] == q.id
        assert captured["details"][0]["prompt"] == "Activated row prompt"
        screen.Destroy()
    finally:
        frame.Destroy()


def test_review_button_disabled_until_row_selected(wx_app, temp_db):
    """The review button is disabled in the empty list and enabled on selection."""
    import wx
    from screens.past_tests_screen import PastTestsScreen

    q = _make_question()
    s = _make_session()
    sec = _make_section(s)
    _make_response(s, sec, q, is_correct=True, selected="A")

    frame = wx.Frame(None)
    try:
        screen = PastTestsScreen(frame)
        screen.refresh()
        # Initially nothing selected.
        assert screen._review_btn.IsEnabled() is False
        # Programmatically select the row by firing the binding.
        screen._list_ctrl.Select(0)
        # The selection event is async on some platforms — call the
        # handler directly to assert enable behaviour deterministically.
        evt = wx.ListEvent(wx.EVT_LIST_ITEM_SELECTED.typeId)
        evt.SetIndex(0)
        screen._on_row_selected(evt)
        assert screen._review_btn.IsEnabled() is True
        screen.Destroy()
    finally:
        frame.Destroy()
