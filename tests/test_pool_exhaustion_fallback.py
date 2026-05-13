"""
Phase 1 R4: inverted pool-exhaustion fallback.

Priority order under pool shortfall:

1. First try: widen the difficulty band (easy/hard → medium) while keeping
   the FULL dedup_exclude. Matches how the real GRE fills thin-tier cells.
2. Second try: still short — partial dedup drop. Items seen within the
   last 7 days stay excluded; older recently-seen / mastered items are
   allowed back in.
3. Last resort: drop all dedup exclusions (in-session still honored).
   Logs at WARN — should be rare in the wild.

These tests pin the contract so future edits can't silently regress back
to the pre-R4 "drop dedup immediately" behavior. We re-use the ``temp_db``
fixture from conftest for isolation and the same hand-built pool patterns
``tests/test_cluster_aware_assembly.py`` / ``tests/test_cross_session_dedup.py``
use.
"""
from datetime import datetime, timedelta

import logging

import pytest

# temp_db fixture from tests/conftest.py is applied automatically.


def _seed_verbal_with_difficulties(n_per_band=(0, 0, 0, 0, 0)):
    """Create ``sum(n_per_band)`` verbal TC singletons with difficulties
    1..5 according to the tuple. Returns a dict ``{difficulty: [qids]}``.
    """
    from models.database import Question

    out = {}
    for band_idx, n in enumerate(n_per_band, start=1):
        ids = []
        for i in range(n):
            q = Question.create(
                measure="verbal", subtype="tc",
                prompt=f"TC-d{band_idx}-{i}",
                time_target_seconds=60,
                concept_tags="[]", explanation="",
                difficulty_target=band_idx, status="live",
            )
            ids.append(q.id)
        out[band_idx] = ids
    return out


def _record_response(qid, *, correct, days_ago):
    """Drop a Response row with a backdated created_at. Same shape as the
    helper in tests/test_cross_session_dedup.py."""
    from models.database import Question, Session, SectionResult, Response
    sess = Session.create(test_type="drill", mode="simulation",
                          section_order="[]", current_section_index=0,
                          state="completed")
    secr = SectionResult.create(session=sess, section_name="verbal_s1",
                                measure="verbal", section_index=1,
                                time_limit_seconds=1080,
                                question_ids="[]")
    ts = datetime.now() - timedelta(days=days_ago)
    Response.create(
        session=sess, section_result=secr,
        question=Question.get_by_id(qid),
        response_payload="{}", is_marked=False,
        is_correct=correct, time_spent_seconds=30,
        answered_at=ts, created_at=ts,
    )


def _attach_caplog_to_qb(caplog):
    """services/log.py namespaces under ``gre_app`` with propagation off.
    Attach caplog's handler so test warnings/info lines surface."""
    logger = logging.getLogger("gre_app.question_bank")
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.INFO)
    return logger


# ── Branch 1: band widening before dropping dedup ─────────────────────

def test_widen_band_before_dropping_dedup(temp_db, caplog):
    """When the requested band can't fill the section but the wider pool
    (medium) can, the assembler must widen the band and keep dedup intact.

    Fixture: 0 hard verbal items, plenty of medium items. Mark 5 medium
    items as recently-seen. Request a ``difficulty_band="hard"`` section.
    Expected: we pick from the medium pool but NEVER touch the recently-
    seen medium qids.
    """
    _attach_caplog_to_qb(caplog)

    # No hard items at all; plenty of medium so widening saves us.
    bands = _seed_verbal_with_difficulties(n_per_band=(0, 0, 30, 0, 0))
    medium_ids = bands[3]

    recently_seen = set(medium_ids[:5])
    for qid in recently_seen:
        _record_response(qid, correct=False, days_ago=3)  # within 7d too

    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    picked = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="hard",
        exclude_user_seen="local",
    )

    # Band widening must trigger (no hard items at all).
    assert len(picked) == 12, f"expected 12 picks, got {len(picked)}"
    # And recently-seen items must NOT be in the picks — dedup preserved.
    overlap = set(picked) & recently_seen
    assert not overlap, (
        f"band-widening fallback leaked recently-seen items: {overlap}")

    # The INFO line for band widening should have fired.
    msgs = [r.message for r in caplog.records]
    assert any("widening to medium" in m for m in msgs), (
        f"expected band-widening log line, got: {msgs}")


# ── Branch 2: partial-dedup protects 7-day window ─────────────────────

def test_partial_dedup_protects_last_7_days(temp_db, caplog):
    """When widening also can't fill the section, the partial-dedup
    branch allows OLDER-than-7d recently-seen items back in while still
    excluding items seen in the last 7 days.
    """
    _attach_caplog_to_qb(caplog)

    # Pool: 20 verbal TC items total (tiny; widening won't help since
    # every item is already in the medium band).
    bands = _seed_verbal_with_difficulties(n_per_band=(0, 0, 20, 0, 0))
    all_ids = bands[3]

    # 3 items seen 3 days ago (within 7d — must stay excluded).
    within_7d = set(all_ids[:3])
    for qid in within_7d:
        _record_response(qid, correct=False, days_ago=3)

    # 15 items seen 15 days ago (outside 7d but inside 30d — dedup_exclude
    # catches them on the strict pass; partial-dedup should let them back
    # in). That leaves only 2 never-seen items available under strict dedup,
    # but 17 items available under partial dedup (more than enough for 12).
    older_than_7d = set(all_ids[3:18])
    for qid in older_than_7d:
        _record_response(qid, correct=False, days_ago=15)

    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    picked = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    picked_set = set(picked)

    # Final section must still fill.
    assert len(picked) == 12, f"short section after partial dedup: {picked}"

    # Items seen in the last 7 days must NOT resurface — partial-dedup
    # was sufficient, so the 7-day floor still holds.
    leaked = picked_set & within_7d
    assert not leaked, (
        f"partial-dedup leaked items seen within last 7 days: {leaked}")

    # And at least one older-than-7d item must have come back in.
    resurfaced = picked_set & older_than_7d
    assert resurfaced, (
        "partial-dedup branch never re-admitted older items; expected "
        "overlap with older_than_7d set.")

    # The INFO line for partial-dedup should have fired; WARN (full-drop)
    # should NOT have fired because partial-dedup was sufficient.
    msgs_info = [r.message for r in caplog.records
                 if r.levelno == logging.INFO]
    msgs_warn = [r.message for r in caplog.records
                 if r.levelno == logging.WARNING]
    assert any("protecting" in m and "last 7 days" in m
               for m in msgs_info), (
        f"expected partial-dedup INFO log, got INFO={msgs_info}")
    assert not any("pool exhausted after dedup" in m for m in msgs_warn), (
        f"unexpected full-drop WARN fired: {msgs_warn}")


# ── Branch 3: full drop + WARN as last resort ─────────────────────────

def test_full_drop_warn_fires_when_everything_fails(temp_db, caplog):
    """When every single item is within the 7-day window, partial-dedup
    cannot save the day. The full-drop branch must fire with a WARN.
    """
    _attach_caplog_to_qb(caplog)

    bands = _seed_verbal_with_difficulties(n_per_band=(0, 0, 15, 0, 0))
    all_ids = bands[3]

    # Mark all 15 items as seen within 7 days. No widening can help (every
    # item is already medium); no partial dedup can help (everyone is in
    # the 7-day window).
    for qid in all_ids:
        _record_response(qid, correct=False, days_ago=2)

    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    picked = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )

    # Full-drop gets us the 12 items.
    assert len(picked) == 12

    warn_msgs = [r.message for r in caplog.records
                 if r.levelno >= logging.WARNING]
    assert any("pool exhausted after dedup" in m for m in warn_msgs), (
        f"expected full-drop WARN log, got WARN={warn_msgs}")


# ── Regression: strict dedup still wins when pool is healthy ──────────

def test_no_fallback_when_pool_is_healthy(temp_db, caplog):
    """Sanity: if the strict-dedup pool already satisfies the section,
    NONE of the fallback branches should trigger (no INFO, no WARN)."""
    _attach_caplog_to_qb(caplog)

    _seed_verbal_with_difficulties(n_per_band=(0, 0, 30, 0, 0))

    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    picked = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    assert len(picked) == 12

    msgs = [r.message for r in caplog.records]
    assert not any("widening to medium" in m for m in msgs), (
        f"unexpected band-widening log: {msgs}")
    assert not any("protecting" in m and "last 7 days" in m for m in msgs), (
        f"unexpected partial-dedup log: {msgs}")
    assert not any("pool exhausted after dedup" in m for m in msgs), (
        f"unexpected full-drop WARN: {msgs}")
