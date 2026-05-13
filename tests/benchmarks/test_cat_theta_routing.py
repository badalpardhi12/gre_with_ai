"""
P3.S1 — section-level CAT benchmark.

Two checks, both gated at the test-file level (not just `test_baseline_snapshot`
which is a recording test):

1. **Repetition-floor regression** — re-runs the same 20-mock simulation
   from ``test_repetition_floor`` and asserts the post-S1 floor matches
   or exceeds the post-R4 floor. S1 changes the *ranking* inside
   ``_take_cluster_aware`` but must not regress the DI / figure /
   RC-passage floors from Phase 1.

2. **Theta-routing direction check** — the core S1 acceptance test.
   Simulate two users against the real seed bank:

   - *330-target user*: answers every S1 item correctly, so
     ``rating_service.get_user_theta`` climbs toward +1 and the S2
     selector routes them to harder items. Assertion: Q2 mean item
     rating >= +0.3.

   - *300-target user*: answers roughly half correct, half wrong, so
     theta sits near 0 and Q2 mean item rating stays near 0 (absolute
     value < 0.3).

The `after_s1_2026_05_12.json` snapshot is written alongside the
regression-gate JSONs from Phase 1 so the two generations diff cleanly.
"""
from __future__ import annotations

import json
import math
import random
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AFTER_R4 = Path(__file__).parent / "after_r4_2026_05_12.json"
AFTER_S1 = Path(__file__).parent / "after_s1_2026_05_12.json"


Q2_COUNT = 15
S1_COUNT = 12


def _bootstrap_bench_db(tmpdir):
    """Copy the shipped seed DB into a scratch path, rebind config, and
    re-import models / services against it. Mirrors the setup used by
    ``tests.benchmarks.test_repetition_floor._simulate_mocks``.

    Returns the rebound ``config`` module plus its original paths so the
    caller can restore them in a ``finally`` block.
    """
    import config
    seed_src = PROJECT_ROOT / "data" / "gre_mock.db"
    assert seed_src.exists(), f"real seed DB not found at {seed_src}"
    db_copy = tmpdir / "bench.db"
    shutil.copy2(str(seed_src), str(db_copy))

    orig_db_path = config.DB_PATH
    orig_seed_path = config.SEED_DB_PATH
    config.DB_PATH = db_copy
    config.SEED_DB_PATH = tmpdir / "no_seed.db"

    for prefix in ("models", "services"):
        for mod in [m for m in list(sys.modules)
                    if m.startswith(prefix + ".") or m == prefix]:
            del sys.modules[mod]

    from models.database import init_db, Response
    init_db()
    Response.delete().execute()
    return config, orig_db_path, orig_seed_path


def _restore_bench_db(config, orig_db_path, orig_seed_path):
    config.DB_PATH = orig_db_path
    config.SEED_DB_PATH = orig_seed_path
    for prefix in ("models", "services"):
        for mod in [m for m in list(sys.modules)
                    if m.startswith(prefix + ".") or m == prefix]:
            del sys.modules[mod]


def _mean_rating(qids):
    """Mean ItemRating for a list of qids; items without a rating row
    are skipped (a fresh-seed bank has one row per live Q so this is
    nearly always full coverage)."""
    from models.database import ItemRating
    vals = []
    for qid in qids:
        row = ItemRating.get_or_none(ItemRating.question_id == qid)
        if row is not None:
            vals.append(float(row.rating))
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _simulate_theta_routing(s1_outcome_fn, rng_seed):
    """Run one simulated user through V1 + Q1 + Q2 and return the Q2
    mean rating. ``s1_outcome_fn(qid) -> bool`` decides whether each S1
    response is marked correct; that determines theta for Q2.

    We skip V2 because only Q2 is needed for the routing check — Verbal
    section behaviour is identical.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="gre_s1_bench_"))
    config, orig_db_path, orig_seed_path = _bootstrap_bench_db(tmpdir)
    try:
        from models.database import Session, SectionResult, Response, Question
        from services.question_bank import QuestionBankService
        from services.rating_service import (
            seed_initial_ratings, update_on_response, get_user_theta,
        )

        seed_initial_ratings()
        random.seed(rng_seed)
        qb = QuestionBankService()

        # Simulate Q1 (user hits quant medium S1).
        q1_ids = qb.select_questions_composed(
            measure="quant", count=S1_COUNT, difficulty_band="medium",
            exclude_user_seen="s1bench",
        )

        sess = Session.create(
            test_type="mock", mode="simulation",
            section_order="[]", state="in_progress",
        )
        sr = SectionResult.create(
            session=sess, section_name="quant_s1", measure="quant",
            section_index=1, time_limit_seconds=1260, question_ids="[]",
        )

        # Answer each S1 item; drive rating_service.get_user_theta.
        for qid in q1_ids:
            is_correct = bool(s1_outcome_fn(qid))
            Response.create(
                session=sess, section_result=sr,
                question=qid, response_payload="{}",
                is_correct=is_correct, time_spent_seconds=60,
            )
            update_on_response(
                user_id="s1bench", question_id=qid, is_correct=is_correct,
            )

        from services.scoring import compute_theta
        theta = compute_theta(user_id="s1bench")

        # Q2 selection with the adapted band AND the new target_theta.
        # Mirror the routing exact: accuracy-based band fallback, then theta.
        correct_count = sum(1 for qid in q1_ids if s1_outcome_fn(qid))
        pct = correct_count / max(1, len(q1_ids))
        if pct < 0.4:
            band = "easy"
        elif pct > 0.7:
            band = "hard"
        else:
            band = "medium"

        q2_ids = qb.select_questions_composed(
            measure="quant", count=Q2_COUNT, difficulty_band=band,
            exclude_ids=list(q1_ids),
            exclude_user_seen="s1bench",
            target_theta=theta,
        )

        q2_mean = _mean_rating(q2_ids)
        q1_mean = _mean_rating(q1_ids)

        return {
            "s1_correct_count": correct_count,
            "s1_total": len(q1_ids),
            "s1_mean_rating": q1_mean,
            "theta_estimate": theta,
            "s2_band_applied": band,
            "q2_mean_rating": q2_mean,
            "q2_count": len(q2_ids),
        }
    finally:
        _restore_bench_db(config, orig_db_path, orig_seed_path)


# ── Tests ────────────────────────────────────────────────────────────


def test_theta_routing_high_target_user_q2_mean_rating(tmp_path):
    """330-target user — every S1 answer correct → theta climbs → Q2
    mean rating should be at least +0.3.

    Writes the snapshot to ``after_s1_2026_05_12.json`` so the metric
    is reproducible and diff-able across runs.
    """
    # Both users land in the same snapshot JSON for clarity.
    high = _simulate_theta_routing(
        s1_outcome_fn=lambda qid: True,
        rng_seed=20260512,
    )
    low = _simulate_theta_routing(
        s1_outcome_fn=lambda qid: (qid % 2 == 0),  # ~50/50
        rng_seed=20260513,
    )

    snapshot = {
        "meta": {
            "generated_for": "P3.S1 section-CAT wire-up",
            "random_seed_high": 20260512,
            "random_seed_mixed": 20260513,
        },
        "high_target_user": high,
        "mixed_accuracy_user": low,
    }
    AFTER_S1.write_text(json.dumps(snapshot, indent=2, sort_keys=True))

    # Acceptance gates.
    assert high["q2_mean_rating"] >= 0.3, (
        "theta-aware Q2 routing for a 330-target (all-correct) user "
        f"should surface a mean item rating >= +0.3; observed "
        f"{high['q2_mean_rating']:.3f} "
        f"(theta={high['theta_estimate']:.3f}, band={high['s2_band_applied']})"
    )
    # Mixed-accuracy user: theta near 0 → Q2 mean should NOT be strongly
    # positive. The medium band is applied and soft-weighting keeps the
    # mean bounded; allow a symmetric ±0.3 tolerance.
    assert abs(low["q2_mean_rating"]) < 0.3, (
        "mixed-accuracy user with theta near 0 should see a Q2 mean "
        f"rating near 0 (|mean| < 0.3); observed "
        f"{low['q2_mean_rating']:.3f} "
        f"(theta={low['theta_estimate']:.3f}, band={low['s2_band_applied']})"
    )


def test_repetition_floor_not_regressed_after_s1():
    """Re-run the 20-mock simulation and assert the post-S1 repetition
    floor matches or exceeds the post-R4 floor. S1 changes the ranker
    only inside ``_take_cluster_aware`` — the DI / figure / RC-passage
    floors from Phase 1 must not regress.
    """
    # Reuse the exact simulation + metrics helpers from the Phase 1
    # benchmark. Importing here keeps the module self-contained.
    from tests.benchmarks import test_repetition_floor as rfmod

    metrics = rfmod._compute_metrics(rfmod._simulate_mocks())
    first = metrics["first_repeat_mock_by_bucket"]

    # Same gates as ``test_post_phase1_targets``.
    assert first["di"] >= 12, (
        f"S1 regressed DI repeat floor: first={first['di']}, want >=12")
    assert first["rc_passage"] >= 8, (
        f"S1 regressed RC-passage repeat floor: first={first['rc_passage']}, "
        "want >=8")

    fig_counts = metrics["hot_items"].get("figure_singleton", {})
    max_fig = max(fig_counts.values(), default=0)
    assert max_fig <= 3, (
        f"S1 regressed figure repetition: max={max_fig}, want <=3; "
        f"hot={fig_counts}"
    )
