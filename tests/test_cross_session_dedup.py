"""
Cross-session dedup tests.

`select_questions_composed(exclude_user_seen=<user_id>)` should filter out
questions the user touched in the last N days, and items they answered
correctly within the spaced-repetition cooldown window. When the adjusted
pool can't satisfy the section, the assembler logs a warning and falls
back to the relaxed pool (no silent repeats).
"""
from datetime import datetime, timedelta

import pytest

# temp_db fixture from tests/conftest.py is applied automatically.


def _seed_bank(temp_db, n_tc=20, n_se=20):
    """Populate a verbal-only pool of individual items."""
    from models.database import Question

    tc_ids, se_ids = [], []
    for i in range(n_tc):
        q = Question.create(measure="verbal", subtype="tc",
                            prompt=f"TC-{i}", time_target_seconds=60,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        tc_ids.append(q.id)
    for i in range(n_se):
        q = Question.create(measure="verbal", subtype="se",
                            prompt=f"SE-{i}", time_target_seconds=60,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        se_ids.append(q.id)
    return tc_ids, se_ids


def _record_response(qid, *, correct, days_ago):
    """Drop a Response row with a backdated created_at."""
    from models.database import Question, Session, SectionResult, Response
    # Need a parent session + section_result (FK-required).
    sess = Session.create(test_type="drill", mode="simulation",
                          section_order="[]", current_section_index=0,
                          state="completed")
    secr = SectionResult.create(session=sess, section_name="verbal_s1",
                                measure="verbal", section_index=1,
                                time_limit_seconds=1080,
                                question_ids="[]")
    ts = datetime.now() - timedelta(days=days_ago)
    r = Response.create(session=sess, section_result=secr,
                        question=Question.get_by_id(qid),
                        response_payload="{}", is_marked=False,
                        is_correct=correct, time_spent_seconds=30,
                        answered_at=ts, created_at=ts)
    return r


def test_recently_seen_items_are_excluded(temp_db):
    """Items the user saw in the last 30d don't resurface."""
    from services.question_bank import QuestionBankService

    tc_ids, se_ids = _seed_bank(temp_db)
    # User saw TC 0-4 recently (5 days ago)
    seen = set(tc_ids[:5])
    for qid in seen:
        _record_response(qid, correct=False, days_ago=5)

    qb = QuestionBankService()
    picked = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    assert not (set(picked) & seen), \
        f"recently-seen items resurfaced: {set(picked) & seen}"


def test_mastered_items_are_cooled_down(temp_db):
    """Items answered CORRECTLY within mastery window are excluded even
    if the recent-seen window has expired."""
    from services.question_bank import QuestionBankService

    tc_ids, _se = _seed_bank(temp_db)
    mastered = set(tc_ids[:3])
    for qid in mastered:
        # 45 days ago: outside default 30d recent-seen, INSIDE default 60d
        # mastery cooldown.
        _record_response(qid, correct=True, days_ago=45)

    qb = QuestionBankService()
    picked = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
        recent_seen_days=30, mastery_cooldown_days=60,
    )
    assert not (set(picked) & mastered), \
        "mastered items reappeared despite cooldown"


def test_wrong_items_may_resurface_sooner(temp_db):
    """Items the user got WRONG >30d ago should be eligible again —
    the learner benefits from revisiting them."""
    from services.question_bank import QuestionBankService

    tc_ids, _ = _seed_bank(temp_db, n_tc=5, n_se=5)
    wrong_old = set(tc_ids)
    for qid in wrong_old:
        # 45 days ago + wrong -> outside recent-seen, no mastery cooldown
        # (cooldown only applies to correct answers).
        _record_response(qid, correct=False, days_ago=45)

    qb = QuestionBankService()
    picked = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    # At least some wrong-old items should come back into rotation.
    assert set(picked) & wrong_old


def test_pool_exhaustion_falls_back_with_warning(temp_db, caplog):
    """When dedup would leave the section short, a warning is logged and
    the assembler refills from the relaxed pool instead of crashing."""
    import logging
    from services.question_bank import QuestionBankService
    # `gre_app` root disables propagation (see services/log.py) — attach
    # caplog's handler directly to the namespaced logger so warnings
    # surface here.
    logger = logging.getLogger("gre_app.question_bank")
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.WARNING)

    # Tiny pool: 12 TC + 12 SE = 24 items. Mark 20 as recently-seen.
    tc_ids, se_ids = _seed_bank(temp_db, n_tc=12, n_se=12)
    for qid in (tc_ids + se_ids)[:20]:
        _record_response(qid, correct=False, days_ago=5)

    qb = QuestionBankService()
    picked = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    # Should get the full 12 via the fallback path.
    assert len(picked) == 12
    # And should have emitted the warning.
    msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("pool exhausted" in m for m in msgs), \
        f"expected pool-exhaustion warning, got: {msgs}"


def test_in_session_exclude_still_honored(temp_db):
    """Legacy exclude_ids (in-session S1→S2 dedup) must still work."""
    from services.question_bank import QuestionBankService

    tc_ids, _se = _seed_bank(temp_db)
    qb = QuestionBankService()
    first = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
    )
    second = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_ids=first,
    )
    assert not (set(first) & set(second))


def test_user_stats_api(temp_db):
    """`user_stats` surfaces seen/total/dedup-window counts for the UI."""
    from services.question_bank import user_stats

    tc_ids, se_ids = _seed_bank(temp_db)
    # 5 items in last 30d, 3 items 45d ago (outside recent, inside mastery)
    for qid in tc_ids[:5]:
        _record_response(qid, correct=False, days_ago=3)
    for qid in tc_ids[5:8]:
        _record_response(qid, correct=True, days_ago=45)

    stats = user_stats(user_id="local")
    assert stats["total_pool"] == len(tc_ids) + len(se_ids)
    assert stats["seen_count"] == 8
    assert stats["dedup_days_active"] == 30
    assert stats["mastery_cooldown_days"] == 60
    # 5 recent + 3 mastered == 8 active dedup
    assert stats["dedup_active_count"] == 8
