"""
Tests for services.scheduler — the cluster-aware SRS layer that replaced
the naive recent_seen_days filter.

Scenarios covered:
  1. item memory state derivation from Response log
  2. retrievability / stability updates under correct/incorrect responses
  3. DI cluster cooldown — solving ONE sibling suppresses the WHOLE chart
  4. RC cluster cooldown — shorter window than DI
  5. passage re-exposure past the cooldown window
  6. target_difficulty_for — maps rolling accuracy to a band
  7. select_questions_composed integration: back-to-back mocks don't
     recycle the same DI chart
  8. pool exhaustion: when all clusters are cooled, the fallback still
     ships a full section rather than crashing

Uses the ``temp_db`` fixture from tests/conftest.py.
"""
from datetime import datetime, timedelta

import pytest


# ── helpers ──────────────────────────────────────────────────────────


def _create_stimulus(kind="graph", title="Cluster"):
    from models.database import Stimulus
    return Stimulus.create(
        stimulus_type=kind, title=title, content="{}", render_spec="{}",
    )


def _create_question(measure="quant", subtype="mcq_single", difficulty=3,
                     stimulus=None, prompt="Q"):
    from models.database import Question
    return Question.create(
        measure=measure, subtype=subtype, prompt=prompt,
        time_target_seconds=60, concept_tags="[]", explanation="",
        difficulty_target=difficulty, status="live",
        stimulus=stimulus,
    )


def _log_response(qid, *, correct, days_ago, session_id=None):
    from models.database import Session, SectionResult, Response, Question
    sess = Session.create(
        test_type="drill", mode="simulation",
        section_order="[]", current_section_index=0,
        state="completed",
    )
    if session_id is None:
        session_id = sess.id
    secr = SectionResult.create(
        session=sess, section_name="quant_s1", measure="quant",
        section_index=1, time_limit_seconds=1080, question_ids="[]",
    )
    ts = datetime.now() - timedelta(days=days_ago)
    return Response.create(
        session=sess, section_result=secr,
        question=Question.get_by_id(qid),
        response_payload="{}", is_marked=False,
        is_correct=correct, time_spent_seconds=30,
        answered_at=ts, created_at=ts,
    )


# ── item memory state ────────────────────────────────────────────────


def test_never_seen_item_has_no_state(temp_db):
    from services.scheduler import build_user_profile
    _create_question()
    profile = build_user_profile("local")
    assert profile.items == {}
    assert profile.rolling_accuracy == 0.5  # default for empty history


def test_seen_item_populates_memory_state(temp_db):
    from services.scheduler import build_user_profile
    q = _create_question()
    _log_response(q.id, correct=True, days_ago=5)
    _log_response(q.id, correct=False, days_ago=2)
    profile = build_user_profile("local")
    state = profile.items[q.id]
    assert state.times_seen == 2
    assert state.times_correct == 1
    assert state.last_seen is not None
    # Rolling accuracy = 1/2
    assert profile.rolling_accuracy == pytest.approx(0.5)


def test_fsrs_lite_stability_grows_on_success(temp_db):
    from services.scheduler import build_user_profile
    q = _create_question()
    _log_response(q.id, correct=True, days_ago=30)
    _log_response(q.id, correct=True, days_ago=5)
    profile = build_user_profile("local")
    state = profile.items[q.id]
    # After two correct answers, stability should be > the default 3 days.
    assert state.stability_days > 3.0
    # Difficulty drifts down on success.
    assert state.difficulty < 5.0


def test_fsrs_lite_stability_collapses_on_lapse(temp_db):
    from services.scheduler import build_user_profile
    q = _create_question()
    _log_response(q.id, correct=True, days_ago=30)
    _log_response(q.id, correct=False, days_ago=5)
    profile = build_user_profile("local")
    state = profile.items[q.id]
    # Lapse should push difficulty up.
    assert state.difficulty > 5.0


# ── cluster cooldowns ────────────────────────────────────────────────


def test_di_cluster_cooldown_suppresses_sibling(temp_db):
    """The core bug fix: solving ONE DI sibling suppresses the WHOLE
    chart for the cluster cooldown window."""
    from services.scheduler import scheduler_exclusions, stim_ids_to_qids

    stim = _create_stimulus(kind="graph", title="Bar chart")
    q1 = _create_question(stimulus=stim, prompt="DI Q1")
    q2 = _create_question(stimulus=stim, prompt="DI Q2")
    q3 = _create_question(stimulus=stim, prompt="DI Q3")

    # User solved q1 yesterday, never saw q2 / q3.
    _log_response(q1.id, correct=True, days_ago=1)

    hard_qids, cluster_stims = scheduler_exclusions("local")
    # q1 is in hard exclusion (recent-seen).
    assert q1.id in hard_qids
    # Cluster is cooled down.
    assert stim.id in cluster_stims
    # Expanded: q2 and q3 also suppressed even though never individually seen.
    expanded = stim_ids_to_qids(cluster_stims)
    assert q2.id in expanded
    assert q3.id in expanded


def test_rc_cluster_has_shorter_cooldown_than_di(temp_db):
    """RC uses 45d default, DI uses 90d default."""
    from services.scheduler import scheduler_exclusions

    di_stim = _create_stimulus(kind="graph", title="Chart")
    rc_stim = _create_stimulus(kind="passage", title="Passage")
    di_q = _create_question(measure="quant", subtype="data_interp",
                             stimulus=di_stim)
    rc_q = _create_question(measure="verbal", subtype="rc_single",
                             stimulus=rc_stim)

    # Both seen 60 days ago — past RC window (45d), inside DI window (90d).
    _log_response(di_q.id, correct=True, days_ago=60)
    _log_response(rc_q.id, correct=True, days_ago=60)

    _hard, cluster_stims = scheduler_exclusions("local")
    assert di_stim.id in cluster_stims, \
        "DI chart should still be cooled at 60d (window=90d)"
    assert rc_stim.id not in cluster_stims, \
        "RC passage should be eligible at 60d (window=45d)"


def test_cluster_cooldown_expires_past_window(temp_db):
    """After the DI cooldown window, the cluster is eligible again."""
    from services.scheduler import scheduler_exclusions

    stim = _create_stimulus(kind="graph")
    q = _create_question(stimulus=stim)
    # Seen 100 days ago, past the 90d DI window.
    _log_response(q.id, correct=True, days_ago=100)

    _hard, cluster_stims = scheduler_exclusions("local")
    assert stim.id not in cluster_stims


def test_cluster_cooldown_override_via_kwargs(temp_db):
    """Callers can tighten/loosen the cluster cooldown via kwargs."""
    from services.scheduler import scheduler_exclusions

    stim = _create_stimulus(kind="graph")
    q = _create_question(stimulus=stim)
    _log_response(q.id, correct=True, days_ago=30)

    # Default DI cooldown = 90d, so cooled at 30d.
    _h, cooled_default = scheduler_exclusions("local")
    assert stim.id in cooled_default

    # Tighter cooldown: 10d. Past window at 30d → not cooled.
    _h, cooled_tight = scheduler_exclusions(
        "local", cluster_di_days=10,
    )
    assert stim.id not in cooled_tight


# ── difficulty targeting ─────────────────────────────────────────────


def test_target_difficulty_follows_accuracy(temp_db):
    from services.scheduler import target_difficulty_for, UserProfile

    hot = UserProfile(user_id="local", rolling_accuracy=0.90)
    assert target_difficulty_for(hot) == 4

    sweet = UserProfile(user_id="local", rolling_accuracy=0.75)
    assert target_difficulty_for(sweet) == 3

    cold = UserProfile(user_id="local", rolling_accuracy=0.40)
    assert target_difficulty_for(cold) == 2


# ── integration with question_bank selection ─────────────────────────


def test_back_to_back_mocks_dont_recycle_di_cluster(temp_db):
    """Build two full Quant sections in a row; the second should NOT pick
    any DI cluster that was in the first."""
    from services.question_bank import QuestionBankService

    # Seed: 4 DI clusters (3 questions each) + 30 solo MCQs so each
    # section has a real composition to work with.
    di_qids_per_cluster = []
    for i in range(4):
        stim = _create_stimulus(kind="graph", title=f"Chart {i}")
        cluster = [
            _create_question(measure="quant", subtype="mcq_single",
                              stimulus=stim, prompt=f"DI{i}-q{j}")
            for j in range(3)
        ]
        di_qids_per_cluster.append({q.id for q in cluster})
    for i in range(30):
        _create_question(measure="quant", subtype="mcq_single",
                          prompt=f"Solo {i}")

    qb = QuestionBankService()
    first = qb.select_questions_composed(
        measure="quant", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )

    # Simulate the user responded to the first section's DI items.
    for qid in first:
        for cluster in di_qids_per_cluster:
            if qid in cluster:
                _log_response(qid, correct=True, days_ago=0)
                break

    # Next mock, after responses logged.
    second = qb.select_questions_composed(
        measure="quant", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )

    # Figure out which DI clusters ended up in each section.
    def cluster_ids(qids):
        out = set()
        for qid in qids:
            for idx, cluster in enumerate(di_qids_per_cluster):
                if qid in cluster:
                    out.add(idx)
        return out

    first_clusters = cluster_ids(first)
    second_clusters = cluster_ids(second)
    overlap = first_clusters & second_clusters
    assert not overlap, (
        f"DI cluster repetition across back-to-back mocks! "
        f"first={first_clusters}, second={second_clusters}, overlap={overlap}"
    )


def test_pool_exhaustion_relaxes_cluster_cooldown(temp_db):
    """When cluster cooldown would leave a section short, the existing
    pool-exhaustion fallback in select_questions_composed still fires
    (it relaxes the dedup set, which for us now includes cluster
    cooldowns)."""
    from services.question_bank import QuestionBankService

    # Only 1 DI cluster in the whole bank, with 2 siblings. User saw both
    # one day ago — cluster fully cooled.
    stim = _create_stimulus(kind="graph")
    cluster = [
        _create_question(measure="quant", subtype="mcq_single",
                          stimulus=stim, prompt=f"DI-{i}")
        for i in range(2)
    ]
    for q in cluster:
        _log_response(q.id, correct=True, days_ago=1)
    # Pad with enough solo items so the section can fill (minus DI slot).
    for i in range(40):
        _create_question(measure="quant", subtype="mcq_single",
                          prompt=f"Solo {i}")

    qb = QuestionBankService()
    picked = qb.select_questions_composed(
        measure="quant", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    assert len(picked) == 12  # fallback fills the section


def test_scheduler_does_not_break_legacy_dedup_tests(temp_db):
    """Legacy test: items seen in the last 30d are still excluded."""
    from services.question_bank import QuestionBankService
    from models.database import Question

    tc_ids = []
    for i in range(20):
        q = _create_question(measure="verbal", subtype="tc",
                              prompt=f"TC-{i}")
        tc_ids.append(q.id)
    for i in range(20):
        _create_question(measure="verbal", subtype="se", prompt=f"SE-{i}")

    seen = set(tc_ids[:5])
    for qid in seen:
        _log_response(qid, correct=False, days_ago=5)

    qb = QuestionBankService()
    picked = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    assert not (set(picked) & seen)


def test_user_stats_surfaces_cluster_cooldown(temp_db):
    """The UX banner should show cluster-cooldown counts so the user
    understands why DI pickings are scarce."""
    from services.question_bank import user_stats

    # 2 cooled DI charts, each with 3 siblings.
    for i in range(2):
        stim = _create_stimulus(kind="graph")
        kids = [
            _create_question(stimulus=stim, prompt=f"DI{i}-{j}")
            for j in range(3)
        ]
        _log_response(kids[0].id, correct=True, days_ago=1)

    stats = user_stats("local")
    assert stats["cluster_cooled_count"] == 2
    # Expansion: 2 clusters × 3 kids = 6 suppressed qids.
    assert stats["cluster_cooled_qid_count"] == 6


# ── score_item ────────────────────────────────────────────────────────


def test_score_item_prefers_never_seen(temp_db):
    from services.scheduler import build_user_profile, score_item
    from models.database import Question

    seen = _create_question()
    fresh = _create_question()
    _log_response(seen.id, correct=True, days_ago=1)

    profile = build_user_profile("local")

    seen_row = Question.get_by_id(seen.id)
    fresh_row = Question.get_by_id(fresh.id)
    s_seen = score_item(seen_row, profile)
    s_fresh = score_item(fresh_row, profile)
    assert s_fresh > s_seen, \
        f"fresh item should outscore seen one: fresh={s_fresh}, seen={s_seen}"


def test_score_item_boosts_leeches(temp_db):
    """Items answered wrong should come back sooner than items never seen
    once enough time has elapsed."""
    from services.scheduler import build_user_profile, score_item
    from models.database import Question

    leech = _create_question()
    fresh = _create_question()
    # Wrong answer 120 days ago (well past the 90-day mastery window —
    # that window only applies to CORRECT answers anyway).
    _log_response(leech.id, correct=False, days_ago=120)

    profile = build_user_profile("local")
    l_row = Question.get_by_id(leech.id)
    f_row = Question.get_by_id(fresh.id)
    s_leech = score_item(l_row, profile)
    s_fresh = score_item(f_row, profile)
    # Leech should beat fresh via the w_leech bonus and overdueness.
    assert s_leech > s_fresh
