"""
Tests for the "Report issue" affordance added to the post-test review
dialog (`screens.answer_review_dialog.AnswerReviewDialog`).

These tests poke at the dialog directly — constructing it on a live
wx.App, then driving `_on_report_clicked` while monkey-patching the
inner FlagQuestionDialog so we don't pop an actual modal. The
persistence path goes through `services.question_bank.flag_question`
which writes a `QuestionFlag` row into the temp_db fixture's SQLite.

Skips cleanly if wxPython can't open a display (CI without Xvfb).
"""
from __future__ import annotations

from unittest import mock

import pytest


# ── Fixtures ─────────────────────────────────────────────────────────

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


@pytest.fixture
def sample_question(temp_db):
    from models.database import Question
    q = Question.create(
        measure="verbal",
        subtype="mcq_single",
        prompt="Which of the following is a prime number?",
        explanation="Only 7 has no divisors other than 1 and itself.",
        status="live",
        source="kaplan_2024",
    )
    return q.id


@pytest.fixture
def review_details(sample_question):
    """Minimal detail dict shaped like what main_frame._build_question_details
    passes to AnswerReviewDialog."""
    return [
        {
            "question_id": sample_question,
            "measure": "verbal",
            "subtype": "mcq_single",
            "prompt": "Which of the following is a prime number?",
            "options": [
                {"label": "A", "text": "4", "is_correct": False},
                {"label": "B", "text": "7", "is_correct": True},
                {"label": "C", "text": "9", "is_correct": False},
            ],
            "user_response": {"selected": ["A"]},
            "is_correct": False,
            "explanation": "7 is prime.",
        }
    ]


# ── Tests ────────────────────────────────────────────────────────────

def test_dialog_builds_report_button_per_card(wx_app, temp_db, review_details):
    """Each card gets a live, bound report button."""
    import wx
    from screens.answer_review_dialog import AnswerReviewDialog

    frame = wx.Frame(None)
    try:
        dlg = AnswerReviewDialog(frame, review_details)
        qid = review_details[0]["question_id"]
        assert qid in dlg._report_widgets
        handles = dlg._report_widgets[qid]
        assert handles["button"] is not None
        assert handles["button"].IsEnabled()
        assert handles["label"].IsShown() is False
        dlg.Destroy()
    finally:
        frame.Destroy()


def test_submit_creates_question_flag_row(wx_app, temp_db, review_details):
    """Driving _on_report_clicked with an OK-returning dialog writes a row."""
    import wx
    from screens.answer_review_dialog import AnswerReviewDialog
    from models.database import QuestionFlag

    qid = review_details[0]["question_id"]
    frame = wx.Frame(None)
    try:
        dlg = AnswerReviewDialog(frame, review_details)

        # Stub out the FlagQuestionDialog import so we never pop a
        # real modal — ShowModal → wx.ID_OK, get_reason/get_note fixed.
        fake_inner = mock.MagicMock()
        fake_inner.ShowModal.return_value = wx.ID_OK
        fake_inner.get_reason.return_value = "wrong_answer"
        fake_inner.get_note.return_value = "The marked answer is actually A."
        # Also stub the Yes/No "open GitHub?" prompt and the GitHub URL
        # builder so no browser opens during tests.
        with mock.patch(
            "widgets.flag_dialog.FlagQuestionDialog", return_value=fake_inner
        ), mock.patch(
            "wx.MessageBox", return_value=wx.NO,
        ):
            dlg._on_report_clicked(qid)

        rows = list(
            QuestionFlag.select().where(QuestionFlag.question_id == qid)
        )
        assert len(rows) == 1
        assert rows[0].reason == "wrong_answer"
        assert rows[0].note == "The marked answer is actually A."
        assert rows[0].user_id == "local"

        # Button should now be disabled + hidden; "Reported" label shown.
        handles = dlg._report_widgets[qid]
        assert handles["button"].IsEnabled() is False
        assert handles["label"].IsShown() is True

        dlg.Destroy()
    finally:
        frame.Destroy()


def test_cancel_does_not_write_row(wx_app, temp_db, review_details):
    """Closing the inner dialog without OK must leave the DB untouched."""
    import wx
    from screens.answer_review_dialog import AnswerReviewDialog
    from models.database import QuestionFlag

    qid = review_details[0]["question_id"]
    frame = wx.Frame(None)
    try:
        dlg = AnswerReviewDialog(frame, review_details)

        fake_inner = mock.MagicMock()
        fake_inner.ShowModal.return_value = wx.ID_CANCEL
        fake_inner.get_reason.return_value = "wrong_answer"
        fake_inner.get_note.return_value = ""
        with mock.patch(
            "widgets.flag_dialog.FlagQuestionDialog", return_value=fake_inner
        ):
            dlg._on_report_clicked(qid)

        assert QuestionFlag.select().count() == 0

        # Button still enabled — user can try again.
        handles = dlg._report_widgets[qid]
        assert handles["button"].IsEnabled() is True
        assert handles["label"].IsShown() is False

        dlg.Destroy()
    finally:
        frame.Destroy()


def test_submit_with_missing_question_does_not_flip_ui(
    wx_app, temp_db, review_details
):
    """If flag_question returns False (e.g. question got retired between
    the review opening and the submit), the UI should stay interactive
    and no row should be written."""
    import wx
    from screens.answer_review_dialog import AnswerReviewDialog
    from models.database import QuestionFlag, Question

    qid = review_details[0]["question_id"]
    # Delete the question out from under us; flag_question should 404.
    Question.delete().where(Question.id == qid).execute()

    frame = wx.Frame(None)
    try:
        dlg = AnswerReviewDialog(frame, review_details)

        fake_inner = mock.MagicMock()
        fake_inner.ShowModal.return_value = wx.ID_OK
        fake_inner.get_reason.return_value = "wrong_answer"
        fake_inner.get_note.return_value = ""
        with mock.patch(
            "widgets.flag_dialog.FlagQuestionDialog", return_value=fake_inner
        ), mock.patch("wx.MessageBox", return_value=wx.OK):
            dlg._on_report_clicked(qid)

        assert QuestionFlag.select().count() == 0
        handles = dlg._report_widgets[qid]
        assert handles["button"].IsEnabled() is True

        dlg.Destroy()
    finally:
        frame.Destroy()


def test_submit_idempotent_within_dialog(wx_app, temp_db, review_details):
    """Clicking Report → Submit twice for the same reason still yields
    exactly one row, thanks to flag_question's idempotence."""
    import wx
    from screens.answer_review_dialog import AnswerReviewDialog
    from models.database import QuestionFlag

    qid = review_details[0]["question_id"]
    frame = wx.Frame(None)
    try:
        dlg = AnswerReviewDialog(frame, review_details)

        fake_inner = mock.MagicMock()
        fake_inner.ShowModal.return_value = wx.ID_OK
        fake_inner.get_reason.return_value = "wrong_answer"
        fake_inner.get_note.return_value = "dup"
        with mock.patch(
            "widgets.flag_dialog.FlagQuestionDialog", return_value=fake_inner
        ), mock.patch("wx.MessageBox", return_value=wx.NO):
            dlg._on_report_clicked(qid)
            dlg._on_report_clicked(qid)

        assert QuestionFlag.select().where(
            QuestionFlag.question_id == qid
        ).count() == 1

        dlg.Destroy()
    finally:
        frame.Destroy()
