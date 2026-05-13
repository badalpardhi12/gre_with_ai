"""
Tests for services.timing_analytics — per-subtype P50/P90 + outlier detection.

Seeds a synthetic Response history against the temp_db fixture and checks
that percentiles, means, and z-score outlier flagging behave as expected.
Also verifies empty-data graceful fallback.
"""
from datetime import datetime, timedelta


def _make_parent_rows():
    """Minimal Session + SectionResult parents for Response FKs."""
    from models.database import Session, SectionResult
    sess = Session.create(test_type="drill", mode="simulation",
                          section_order="[]", current_section_index=0,
                          state="completed")
    secr = SectionResult.create(session=sess, section_name="verbal_s1",
                                measure="verbal", section_index=1,
                                time_limit_seconds=1080,
                                question_ids="[]")
    return sess, secr


def _mk_q(subtype, i=0, measure="verbal"):
    from models.database import Question
    return Question.create(measure=measure, subtype=subtype,
                           prompt=f"{subtype}-{i}",
                           time_target_seconds=60,
                           concept_tags="[]", explanation="",
                           difficulty_target=3, status="live")


def _mk_resp(sess, secr, qid, ms, days_ago=0, seconds=None):
    from models.database import Question, Response
    ts = datetime.now() - timedelta(days=days_ago)
    return Response.create(
        session=sess, section_result=secr,
        question=Question.get_by_id(qid),
        response_payload="{}", is_marked=False, is_correct=True,
        time_spent_seconds=seconds if seconds is not None
        else (int(ms / 1000) if ms else 0),
        time_to_answer_ms=ms,
        answered_at=ts, created_at=ts,
    )


def test_empty_data_returns_empty(temp_db):
    from services.timing_analytics import per_subtype_p50_p90, outliers
    assert per_subtype_p50_p90() == {}
    assert outliers() == []


def test_per_subtype_p50_p90_basic(temp_db):
    from services.timing_analytics import per_subtype_p50_p90
    sess, secr = _make_parent_rows()
    q = _mk_q("tc")
    # 11 evenly-spaced response times for a single subtype: 1s..11s
    for i in range(1, 12):
        _mk_resp(sess, secr, q.id, i * 1000)

    result = per_subtype_p50_p90()
    assert "tc" in result
    r = result["tc"]
    assert r["n"] == 11
    # Median of 1..11 = 6 (in ms: 6000). p90 of 1..11 linear-interp = 10000.
    assert r["p50"] == 6000
    assert r["p90"] == 10000
    assert abs(r["mean"] - 6000) < 1


def test_per_subtype_groups_by_subtype(temp_db):
    from services.timing_analytics import per_subtype_p50_p90
    sess, secr = _make_parent_rows()
    q_tc = _mk_q("tc")
    q_rc = _mk_q("rc_single")
    for ms in (1000, 2000, 3000):
        _mk_resp(sess, secr, q_tc.id, ms)
    for ms in (10000, 20000, 30000):
        _mk_resp(sess, secr, q_rc.id, ms)

    result = per_subtype_p50_p90()
    assert set(result.keys()) == {"tc", "rc_single"}
    assert result["tc"]["p50"] == 2000
    assert result["rc_single"]["p50"] == 20000


def test_falls_back_to_time_spent_seconds_when_ms_null(temp_db):
    """Rows from before migration 024 still participate via seconds col."""
    from services.timing_analytics import per_subtype_p50_p90
    sess, secr = _make_parent_rows()
    q = _mk_q("se")
    # Explicit ms=None, seconds=5 -> should be read as 5000 ms.
    _mk_resp(sess, secr, q.id, ms=None, seconds=5)
    _mk_resp(sess, secr, q.id, ms=None, seconds=7)
    _mk_resp(sess, secr, q.id, ms=None, seconds=9)

    result = per_subtype_p50_p90()
    assert result["se"]["n"] == 3
    assert result["se"]["p50"] == 7000


def test_window_excludes_old_responses(temp_db):
    from services.timing_analytics import per_subtype_p50_p90
    sess, secr = _make_parent_rows()
    q = _mk_q("qc", measure="quant")
    # One recent, one 40 days old (outside default 30-day window).
    _mk_resp(sess, secr, q.id, 3000, days_ago=1)
    _mk_resp(sess, secr, q.id, 99000, days_ago=40)

    result = per_subtype_p50_p90(days=30)
    assert result["qc"]["n"] == 1
    assert result["qc"]["p50"] == 3000


def test_outliers_flag_high_z_scores(temp_db):
    from services.timing_analytics import outliers
    sess, secr = _make_parent_rows()
    q = _mk_q("tc")
    # 9 tight samples around ~2s, one egregious 30s outlier.
    tight = [1800, 1900, 2000, 2100, 2000, 2100, 1900, 2000, 2200]
    for ms in tight:
        _mk_resp(sess, secr, q.id, ms)
    outlier_resp = _mk_resp(sess, secr, q.id, 30000)

    flagged = outliers(z_threshold=2.0)
    flagged_ids = {r.id for r in flagged}
    assert outlier_resp.id in flagged_ids
    # None of the tight cluster should be flagged.
    for r in flagged:
        assert r.time_to_answer_ms == 30000


def test_outliers_skip_subtype_with_single_sample(temp_db):
    from services.timing_analytics import outliers
    sess, secr = _make_parent_rows()
    q = _mk_q("rc_single")
    _mk_resp(sess, secr, q.id, 12345)
    assert outliers() == []


def test_outliers_skip_constant_subtype(temp_db):
    """SD == 0 -> no outliers (all identical reads)."""
    from services.timing_analytics import outliers
    sess, secr = _make_parent_rows()
    q = _mk_q("se")
    for _ in range(5):
        _mk_resp(sess, secr, q.id, 5000)
    assert outliers() == []
