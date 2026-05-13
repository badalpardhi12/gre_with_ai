"""
Tests for the FSRS item-level scheduler (P2.E2).

Covers:
  1. ItemReview schema roundtrip (insert → query → field types).
  2. ``review_item`` state-machine transitions for each of the 4 ratings.
  3. ``get_due_items`` returns only due items, oldest-due-first.
  4. ``schedule_redo`` create path + reset path.
  5. Integration: schedule_redo wiring populates the due-queue so the
     practice-screen "Due for Review" banner can pick it up.

Uses the ``temp_db`` fixture from tests/conftest.py.
"""
from datetime import datetime, timedelta

import pytest


def _make_question(subtype="mcq_single", measure="quant", prompt="Q?"):
    """Insert a minimal live Question row and return the instance."""
    from models.database import Question
    return Question.create(
        measure=measure,
        subtype=subtype,
        prompt=prompt,
        time_target_seconds=60,
        concept_tags="[]",
        explanation="",
        difficulty_target=3,
        status="live",
    )


# ── 1. schema roundtrip ─────────────────────────────────────────────


def test_item_review_schema_roundtrip(temp_db):
    from models.database import ItemReview

    q = _make_question()
    row = ItemReview.create(
        user_id="local",
        question_id=q.id,
        state="new",
        stability=0.0,
        difficulty=5.0,
        n_reviews=0,
        n_lapses=0,
        next_due_at=None,
    )
    fetched = ItemReview.get(ItemReview.id == row.id)
    assert fetched.user_id == "local"
    assert fetched.question_id == q.id
    assert fetched.state == "new"
    assert fetched.stability == 0.0
    assert fetched.difficulty == 5.0
    assert fetched.n_reviews == 0
    assert fetched.n_lapses == 0
    assert fetched.next_due_at is None


def test_item_review_unique_per_user_question(temp_db):
    """The (user_id, question_id) composite index is UNIQUE — a second
    insert for the same pair must fail."""
    from peewee import IntegrityError
    from models.database import ItemReview

    q = _make_question()
    ItemReview.create(user_id="local", question_id=q.id)
    with pytest.raises(IntegrityError):
        ItemReview.create(user_id="local", question_id=q.id)


# ── 2. review_item transitions ─────────────────────────────────────


def test_review_item_new_to_learning_on_again(temp_db):
    from services import srs
    from models.database import ItemReview

    q = _make_question()
    out = srs.review_item("local", q.id, rating=1)
    assert out["state"] == "learning"
    # Should be due in ~minutes, not days
    delta = out["next_due_at"] - datetime.now()
    assert delta < timedelta(hours=1)
    row = ItemReview.get(ItemReview.question_id == q.id)
    assert row.n_reviews == 1
    assert row.n_lapses == 0


def test_review_item_new_to_review_on_good(temp_db):
    from services import srs
    from models.database import ItemReview

    q = _make_question()
    out = srs.review_item("local", q.id, rating=3)
    assert out["state"] == "review"
    # Due at least a day out
    delta = out["next_due_at"] - datetime.now()
    assert delta >= timedelta(hours=20)
    row = ItemReview.get(ItemReview.question_id == q.id)
    assert row.n_reviews == 1


def test_review_item_good_then_easy_extends_interval(temp_db):
    """Hitting Easy after stabilizing pushes next_due out at least 4
    days (the acceptance bar in the implementation plan)."""
    from services import srs

    q = _make_question()
    srs.review_item("local", q.id, rating=3)  # Good → review, ~1 day
    srs.review_item("local", q.id, rating=3)  # Good again → stability grows
    out = srs.review_item("local", q.id, rating=4)  # Easy
    delta = out["next_due_at"] - datetime.now()
    assert delta >= timedelta(days=4), (
        f"Easy after two Goods should push next_due ≥4d; got {delta}"
    )


def test_review_item_lapse_from_review_transitions_to_relearning(temp_db):
    """An Again (1) on a mature review card: state=relearning, lapse++."""
    from services import srs
    from models.database import ItemReview

    q = _make_question()
    srs.review_item("local", q.id, rating=3)  # → review
    out = srs.review_item("local", q.id, rating=1)
    assert out["state"] == "relearning"
    row = ItemReview.get(ItemReview.question_id == q.id)
    assert row.n_lapses == 1
    assert row.n_reviews == 2


def test_review_item_hard_keeps_state_in_review(temp_db):
    from services import srs

    q = _make_question()
    srs.review_item("local", q.id, rating=3)  # → review
    out = srs.review_item("local", q.id, rating=2)  # Hard → still review
    assert out["state"] == "review"


def test_review_item_invalid_rating_rejected(temp_db):
    from services import srs

    q = _make_question()
    with pytest.raises(ValueError):
        srs.review_item("local", q.id, rating=0)
    with pytest.raises(ValueError):
        srs.review_item("local", q.id, rating=5)


# ── 3. get_due_items ───────────────────────────────────────────────


def test_get_due_items_returns_due_ordered(temp_db):
    """Items past their due-time surface; future items don't. Order is
    oldest-due-first (most urgent)."""
    from services import srs
    from models.database import ItemReview

    q1 = _make_question(prompt="q1")
    q2 = _make_question(prompt="q2")
    q3 = _make_question(prompt="q3")

    now = datetime.now()
    # q2 most-overdue, q1 just-due, q3 in the future.
    ItemReview.create(user_id="local", question_id=q1.id,
                      state="learning", stability=0.4,
                      next_due_at=now - timedelta(minutes=5))
    ItemReview.create(user_id="local", question_id=q2.id,
                      state="learning", stability=0.4,
                      next_due_at=now - timedelta(hours=2))
    ItemReview.create(user_id="local", question_id=q3.id,
                      state="review", stability=1.0,
                      next_due_at=now + timedelta(days=1))

    due = srs.get_due_items("local", limit=10)
    assert q3.id not in due
    assert due == [q2.id, q1.id]

    assert srs.due_items_count("local") == 2


def test_get_due_items_skips_null_due(temp_db):
    """Rows with next_due_at=NULL (e.g. freshly inserted before any
    review) are not surfaced — they should only arrive via an explicit
    schedule."""
    from services import srs
    from models.database import ItemReview

    q = _make_question()
    ItemReview.create(user_id="local", question_id=q.id, next_due_at=None)
    assert srs.get_due_items("local") == []


def test_get_due_items_scoped_per_user(temp_db):
    from services import srs
    from models.database import ItemReview

    q = _make_question()
    now = datetime.now()
    ItemReview.create(user_id="alice", question_id=q.id,
                      state="learning", next_due_at=now - timedelta(minutes=5))
    assert srs.get_due_items("local") == []
    assert srs.get_due_items("alice") == [q.id]


# ── 4. schedule_redo ───────────────────────────────────────────────


def test_schedule_redo_creates_row_when_absent(temp_db):
    """First-ever schedule_redo for (user, q) creates a learning-state
    row due in the near future. n_reviews stays at 0 — the user hasn't
    actually re-answered yet."""
    from services import srs
    from models.database import ItemReview

    q = _make_question()
    srs.schedule_redo("local", q.id)
    row = ItemReview.get(ItemReview.question_id == q.id)
    assert row.state == "learning"
    assert row.n_reviews == 0
    assert row.n_lapses == 0
    assert row.next_due_at is not None
    assert row.next_due_at <= datetime.now() + timedelta(hours=1)


def test_schedule_redo_resets_mature_card(temp_db):
    """Calling schedule_redo on a card already in 'review' state drops
    it back to 'learning' with a short due-window, but leaves n_reviews
    / n_lapses untouched."""
    from services import srs
    from models.database import ItemReview

    q = _make_question()
    srs.review_item("local", q.id, rating=3)  # → review
    srs.review_item("local", q.id, rating=3)  # compound
    pre = ItemReview.get(ItemReview.question_id == q.id)
    pre_reviews = pre.n_reviews

    srs.schedule_redo("local", q.id)
    row = ItemReview.get(ItemReview.question_id == q.id)
    assert row.state == "learning"
    assert row.n_reviews == pre_reviews  # not bumped
    assert row.next_due_at <= datetime.now() + timedelta(hours=1)


# ── 5. integration: Schedule Redo → due queue ───────────────────────


def test_schedule_redo_surfaces_in_due_queue(temp_db):
    """End-to-end wiring of the error-log Schedule-Redo button: after
    schedule_redo, the question id appears in ``get_due_items`` within
    a minute and can be picked up by the practice-screen banner."""
    from services import srs
    from services.question_bank import QuestionBankService

    q = _make_question()
    srs.schedule_redo("local", q.id)

    # schedule_redo parks the item in the learning step (~10 min out by
    # default). Bypass that for the due check by pulling directly on a
    # now-shifted horizon — simulate "a few minutes later".
    from models.database import ItemReview
    row = ItemReview.get(ItemReview.question_id == q.id)
    row.next_due_at = datetime.now() - timedelta(seconds=1)
    row.save()

    due = srs.get_due_items("local")
    assert q.id in due

    qb = QuestionBankService()
    pool = qb.select_review_queue(count=10, user_id="local")
    assert pool == [q.id]


def test_select_review_queue_filters_retired(temp_db):
    """select_review_queue drops retired questions so a stale ItemReview
    can't ship a deleted item."""
    from services import srs
    from services.question_bank import QuestionBankService
    from models.database import ItemReview, Question

    q_live = _make_question(prompt="live")
    q_gone = _make_question(prompt="gone")
    q_gone.status = "retired"
    q_gone.save()

    now = datetime.now()
    ItemReview.create(user_id="local", question_id=q_live.id,
                      state="learning", next_due_at=now - timedelta(minutes=1))
    ItemReview.create(user_id="local", question_id=q_gone.id,
                      state="learning", next_due_at=now - timedelta(minutes=2))

    qb = QuestionBankService()
    assert qb.select_review_queue(count=10, user_id="local") == [q_live.id]
