"""
Tests for P3.S4 — forgetting-curve decay on per-subtopic mastery, the
heatmap payload for the Insights screen, and the downstream study-plan
hook that reorders recommendations by decayed weakness.
"""
from datetime import datetime, timedelta

import pytest


def _make_question(subtopic: str, measure: str = "quant",
                   difficulty: int = 3):
    from models.database import Question
    return Question.create(
        version=1,
        measure=measure,
        subtype="mcq_single",
        prompt=f"stub prompt for {subtopic}",
        difficulty_target=difficulty,
        time_target_seconds=90,
        topic="test_topic",
        subtopic=subtopic,
        status="live",
        explanation="",
    )


def _seed_record(subtopic: str, score: float, last_attempt_at: datetime,
                 attempts: int = 5, user_id: str = "local"):
    """Create a MasteryRecord pinned to an exact timestamp (bypassing
    ``update_mastery`` so tests don't depend on the EWMA update rule)."""
    from models.database import MasteryRecord
    return MasteryRecord.create(
        user_id=user_id,
        subtopic=subtopic,
        attempts=attempts,
        correct=max(0, int(attempts * score)),
        mastery_score=score,
        last_attempt_at=last_attempt_at,
        last_updated_at=last_attempt_at,
    )


# ── decayed_mastery ─────────────────────────────────────────────────

def test_decayed_mastery_halves_over_half_life(temp_db):
    from services.mastery import decayed_mastery, FORGETTING_HALF_LIFE_DAYS

    now = datetime(2026, 5, 12, 12, 0, 0)
    last_seen = now - timedelta(days=FORGETTING_HALF_LIFE_DAYS)
    _seed_record("algebra_linear", 0.8, last_seen)

    decayed = decayed_mastery("local", "algebra_linear", now=now)
    # 0.8 * 0.5^(14/14) = 0.4
    assert decayed == pytest.approx(0.4, abs=1e-9)


def test_decayed_mastery_two_half_lives(temp_db):
    from services.mastery import decayed_mastery, FORGETTING_HALF_LIFE_DAYS

    now = datetime(2026, 5, 12, 12, 0, 0)
    last_seen = now - timedelta(days=2 * FORGETTING_HALF_LIFE_DAYS)
    _seed_record("algebra_linear", 0.8, last_seen)

    decayed = decayed_mastery("local", "algebra_linear", now=now)
    assert decayed == pytest.approx(0.2, abs=1e-9)


def test_decayed_mastery_fresh_attempt_no_decay(temp_db):
    from services.mastery import decayed_mastery

    now = datetime(2026, 5, 12, 12, 0, 0)
    _seed_record("algebra_linear", 0.7, now)  # same instant
    # Also probe a slightly-before timestamp — within the same hour, decay
    # should be effectively nil.
    assert decayed_mastery("local", "algebra_linear", now=now) == pytest.approx(0.7)


def test_decayed_mastery_never_seen_returns_zero(temp_db):
    from services.mastery import decayed_mastery
    assert decayed_mastery("local", "never_seen_subtopic") == 0.0


def test_decayed_mastery_ignores_record_with_null_last_attempt(temp_db):
    from services.mastery import decayed_mastery
    from models.database import MasteryRecord

    MasteryRecord.create(
        user_id="local",
        subtopic="stale_cold_record",
        attempts=0,
        correct=0,
        mastery_score=0.5,
        last_attempt_at=None,
    )
    assert decayed_mastery("local", "stale_cold_record") == 0.0


# ── heatmap_data ────────────────────────────────────────────────────

def test_heatmap_data_sorted_by_decayed_ascending(temp_db):
    from services.mastery import heatmap_data

    now = datetime(2026, 5, 12, 12, 0, 0)

    # (A) fresh + strong     — should land last.
    _make_question("fresh_strong")
    _seed_record("fresh_strong", 0.9, now - timedelta(days=1))

    # (B) stale + formerly strong — decays below (A) but above (C).
    _make_question("stale_strong")
    _seed_record("stale_strong", 0.9, now - timedelta(days=28))

    # (C) fresh + weak — low raw, barely decayed → weakest.
    _make_question("fresh_weak")
    _seed_record("fresh_weak", 0.2, now - timedelta(days=1))

    # (D) never seen — 0.0 decayed, should tie with C at the bottom but
    # sort below (C) because C > 0 and D == 0. heatmap_data's secondary
    # sort key is subtopic name for determinism.
    _make_question("unseen_sub")

    rows = heatmap_data("local", now=now)
    subs = [r["subtopic"] for r in rows]

    # unseen_sub and any other 0.0-decayed rows come first.
    assert subs[0] == "unseen_sub"
    # The ordering amongst seen rows is by decayed ascending.
    seen_ordered = [s for s in subs if s in
                    ("fresh_weak", "stale_strong", "fresh_strong")]
    assert seen_ordered == ["fresh_weak", "stale_strong", "fresh_strong"]


def test_heatmap_data_includes_bank_only_subtopics(temp_db):
    from services.mastery import heatmap_data

    _make_question("bank_only_subtopic")
    rows = heatmap_data()
    subs = {r["subtopic"] for r in rows}
    assert "bank_only_subtopic" in subs
    bank_row = next(r for r in rows if r["subtopic"] == "bank_only_subtopic")
    assert bank_row["mastery_raw"] == 0.0
    assert bank_row["mastery_decayed"] == 0.0
    assert bank_row["days_since_seen"] is None
    assert bank_row["n_responses"] == 0


def test_heatmap_data_days_since_seen_populated(temp_db):
    from services.mastery import heatmap_data

    now = datetime(2026, 5, 12, 12, 0, 0)
    _make_question("tracked")
    _seed_record("tracked", 0.6, now - timedelta(days=10))

    rows = heatmap_data("local", now=now)
    row = next(r for r in rows if r["subtopic"] == "tracked")
    assert row["days_since_seen"] == pytest.approx(10.0, abs=1e-6)
    # 0.6 * 0.5^(10/14) ≈ 0.3650
    assert row["mastery_decayed"] == pytest.approx(
        0.6 * 0.5 ** (10 / 14), rel=1e-6,
    )


# ── study-plan integration ─────────────────────────────────────────

def test_decayed_weakness_ranking_prioritizes_forgotten_strong(temp_db):
    """A subtopic that was mastered long ago (stale_strong) should beat a
    slightly-higher-decayed-but-fresh subtopic for drill priority — the
    point of wiring decay into the recommender."""
    from services.mastery import decayed_weakness_ranking

    now = datetime(2026, 5, 12, 12, 0, 0)
    _seed_record("stale_formerly_strong", 0.9,
                 now - timedelta(days=60))   # 0.9 * 0.5^(60/14) ≈ 0.046
    _seed_record("fresh_meh", 0.55, now - timedelta(hours=1))
    _seed_record("fresh_strong", 0.9, now - timedelta(hours=1))

    ranked = decayed_weakness_ranking("local", limit=5, now=now)
    names = [r[0] for r in ranked]
    assert names.index("stale_formerly_strong") < names.index("fresh_meh")
    assert names.index("fresh_meh") < names.index("fresh_strong")


def test_study_plan_context_includes_decayed_mastery(temp_db):
    """_build_context should surface decayed scores (the hook the LLM uses
    to pick priority subtopics). When a subtopic is stale, its rendered
    percentage should be the decayed value, not the raw one."""
    from services.study_plan import _build_context

    now = datetime.now()
    _seed_record("stale_was_good", 0.9,
                 now - timedelta(days=28))  # ~2 half-lives → ~0.225
    _seed_record("fresh_weak", 0.25, now - timedelta(hours=1))

    ctx = _build_context(diagnostic=None)
    # The weakest-first block should list either stale_was_good or
    # fresh_weak near the top — both < 0.5 decayed. The key assertion is
    # that stale_was_good's rendered score is the DECAYED ~22–23%, not
    # the raw 90%.
    assert "stale_was_good" in ctx
    # "WEAK   stale_was_good: 22%" (or similar) appears — definitely not 90%.
    stale_line = [ln for ln in ctx.splitlines()
                  if "stale_was_good" in ln][0]
    assert "90%" not in stale_line.split("(")[0]  # ignore the "(raw 90%)" tag
    # Confirm the raw-tag annotation is present since the decayed value
    # diverges from the raw value.
    assert "raw 90%" in stale_line


def test_get_all_decayed_mastery_matches_decayed_mastery(temp_db):
    from services.mastery import (
        decayed_mastery, get_all_decayed_mastery,
    )

    now = datetime(2026, 5, 12, 12, 0, 0)
    _seed_record("a", 0.8, now - timedelta(days=7))
    _seed_record("b", 0.5, now - timedelta(days=21))

    bulk = get_all_decayed_mastery("local", now=now)
    assert set(bulk.keys()) == {"a", "b"}
    assert bulk["a"] == pytest.approx(
        decayed_mastery("local", "a", now=now), rel=1e-9,
    )
    assert bulk["b"] == pytest.approx(
        decayed_mastery("local", "b", now=now), rel=1e-9,
    )


# ── Insights screen smoke ───────────────────────────────────────────

def test_insights_screen_constructor_smoke(temp_db):
    """The freshness card should instantiate + refresh without crashing,
    even on an empty DB."""
    wx = pytest.importorskip("wx")
    app = wx.App(False)  # noqa: F841 — kept alive for the frame
    frame = wx.Frame(None)
    try:
        from screens.insights_screen import InsightsScreen
        screen = InsightsScreen(frame)
        screen.refresh()
    finally:
        frame.Destroy()
