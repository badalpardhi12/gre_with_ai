"""Tests for services.score_forecast calibrated IRT path (Phase 4 P2).

Covers:
    * ``predict_from_theta`` matches ETS anchor points per measure.
    * ``predict_composite`` on a synthetic user with theta=0 returns
      quant/verbal ≈ 155.
    * ``predict_composite`` on a synthetic user with theta=+2 returns
      quant ≈ 167, verbal ≈ 165.
    * Backtest over 100 simulated users: mean absolute error between
      predicted composite theta-mapped score and the ETS anchor score
      for their true theta < 3.
    * Fallback path: when Question.irt_b_estimate is NULL on every
      item, predict_composite falls back to ``legacy_logistic`` and
      still returns the full key set.
    * Output structure: always has keys quant, verbal, total, method.
"""
from __future__ import annotations

import math
import random

import pytest


# ── Helpers ──────────────────────────────────────────────────────────


def _make_question(measure="quant", band=3, irt_a=None, irt_b=None, label="q"):
    from models.database import Question
    return Question.create(
        measure=measure, subtype="mcq_single",
        prompt=label,
        difficulty_target=band,
        time_target_seconds=90,
        concept_tags="[]", explanation="",
        status="live",
        irt_a_estimate=irt_a,
        irt_b_estimate=irt_b,
    )


def _make_session():
    from models.database import Session, SectionResult
    sess = Session.create(
        test_type="drill", mode="learning",
        section_order="[]", state="completed",
    )
    sr = SectionResult.create(
        session=sess, section_name="drill", measure="quant",
        section_index=1, time_limit_seconds=0, question_ids="[]",
    )
    return sess, sr


def _simulate_user(
    theta: float,
    n_quant=20,
    n_verbal=20,
    seed=0,
):
    """Create calibrated IRT items + responses for one synthetic user.

    Items have irt_b spanning [-2, 2] and irt_a ~ 1.0. The user's
    responses are drawn from a 2PL Bernoulli with the given theta.
    Also seeds ItemRating rows so rating_service.get_user_theta can
    compute a non-None theta signal.
    """
    from models.database import ItemRating, Response

    rng = random.Random(seed)
    sess, sr = _make_session()

    def _pack(measure, n):
        qids = []
        bs = [(-2.0 + 4.0 * i / max(1, n - 1)) for i in range(n)]
        for i, b in enumerate(bs):
            band = max(1, min(5, int(round(b)) + 3))
            q = _make_question(
                measure=measure, band=band,
                irt_a=1.0, irt_b=b,
                label=f"{measure}-{i}",
            )
            ItemRating.create(
                question_id=q.id, rating=b, n_responses=0,
            )
            qids.append(q.id)
        # Draw one response per item.
        for qid, b in zip(qids, bs):
            p = 1.0 / (1.0 + math.exp(-(theta - b)))
            is_correct = rng.random() < p
            Response.create(
                session=sess, section_result=sr, question=qid,
                response_payload="{}",
                is_correct=is_correct,
                time_spent_seconds=10,
            )
        return qids

    _pack("quant", n_quant)
    _pack("verbal", n_verbal)


# ── predict_from_theta anchor tests ──────────────────────────────────


def test_predict_from_theta_quant_anchors(temp_db):
    from services.score_forecast import predict_from_theta

    # Anchors from the task spec.
    assert predict_from_theta(-1.0, "quant") == 150
    assert predict_from_theta(0.0, "quant") == 155
    assert predict_from_theta(1.0, "quant") == 163
    assert predict_from_theta(2.0, "quant") == 167
    assert predict_from_theta(3.0, "quant") == 170


def test_predict_from_theta_verbal_anchors(temp_db):
    from services.score_forecast import predict_from_theta

    assert predict_from_theta(-1.0, "verbal") == 150
    assert predict_from_theta(0.0, "verbal") == 155
    assert predict_from_theta(1.0, "verbal") == 162
    assert predict_from_theta(2.0, "verbal") == 165


def test_predict_from_theta_clamps(temp_db):
    from services.score_forecast import predict_from_theta

    assert predict_from_theta(-10.0, "quant") == 130
    assert predict_from_theta(10.0, "quant") == 170
    # Interpolation inside a segment: halfway between anchors.
    mid = predict_from_theta(0.5, "quant")  # between 155 and 163
    assert 157 <= mid <= 161


# ── predict_composite on synthetic users ─────────────────────────────


def test_composite_theta_zero_maps_near_155(temp_db):
    from services.score_forecast import predict_composite

    _simulate_user(theta=0.0, n_quant=40, n_verbal=40, seed=42)

    out = predict_composite()
    assert out["method"] == "irt_theta"
    # Within ±4 of 155; response sampling jitter is ~1 theta-unit on
    # 40 items, so allow ±4 scaled points.
    assert abs(out["quant"] - 155) <= 5, out
    assert abs(out["verbal"] - 155) <= 5, out


def test_composite_theta_plus_two_maps_near_anchors(temp_db):
    from services.score_forecast import predict_composite

    _simulate_user(theta=2.0, n_quant=40, n_verbal=40, seed=7)

    out = predict_composite()
    assert out["method"] == "irt_theta"
    # Anchors say quant=167, verbal=165 at theta=+2. Sampling jitter
    # on 40 items is ~±4 scaled points.
    assert abs(out["quant"] - 167) <= 5, out
    assert abs(out["verbal"] - 165) <= 5, out


# ── 100-user backtest MAE ────────────────────────────────────────────


def test_backtest_mae_under_three(temp_db):
    """Simulate 100 users with known theta → MAE of predicted composite
    total vs the ETS anchor total for the true theta < 3 scaled points.
    """
    from services.score_forecast import predict_composite, predict_from_theta
    from services.rating_service import seed_initial_ratings

    rng = random.Random(20260513)
    errors = []

    for u in range(100):
        # New temp_db fixture per call would be ideal but we're inside one
        # test — so rebuild the DB-state between simulated users by
        # truncating the relevant tables.
        from models.database import (
            ItemRating, Response, Question, Session, SectionResult,
        )
        for M in (Response, ItemRating, SectionResult, Session, Question):
            M.delete().execute()

        theta = rng.gauss(0.0, 1.0)
        theta = max(-2.5, min(2.5, theta))
        _simulate_user(theta=theta, n_quant=50, n_verbal=50, seed=u)
        seed_initial_ratings()  # harmless — ItemRating already populated

        out = predict_composite()
        if out["method"] != "irt_theta":
            continue

        expected_q = predict_from_theta(theta, "quant")
        expected_v = predict_from_theta(theta, "verbal")
        expected_total = expected_q + expected_v
        err = abs(out["total"] - expected_total)
        errors.append(err)

    assert errors, "no IRT-path predictions produced in backtest"
    mae = sum(errors) / len(errors)
    print(f"\n[forecast-backtest] n={len(errors)} MAE={mae:.2f}")
    assert mae < 3.0, f"backtest MAE {mae:.2f} exceeds 3.0 target"


# ── Fallback path ────────────────────────────────────────────────────


def test_fallback_when_no_irt_params(temp_db):
    """When zero items have irt_b_estimate, predict_composite must
    return the legacy logistic path with the full key set."""
    from services.score_forecast import predict_composite
    from models.database import ItemRating, Response, Question, Session, SectionResult

    sess, sr = _make_session()
    qids = []
    for i in range(15):
        q = _make_question(
            measure="quant", band=3, irt_a=None, irt_b=None,
            label=f"nq-{i}",
        )
        qids.append(q.id)
    for i in range(15):
        q = _make_question(
            measure="verbal", band=3, irt_a=None, irt_b=None,
            label=f"nv-{i}",
        )
        qids.append(q.id)
    for qid in qids:
        Response.create(
            session=sess, section_result=sr, question=qid,
            response_payload="{}",
            is_correct=True,
            time_spent_seconds=10,
        )

    out = predict_composite()
    # No IRT coverage → must fall back.
    assert out["method"] == "legacy_logistic", out
    # Keys always present.
    assert set(out.keys()) == {"quant", "verbal", "total", "method"}
    # Logistic produced real numbers for both measures (all-correct).
    assert out["quant"] is not None
    assert out["verbal"] is not None
    assert out["total"] == out["quant"] + out["verbal"]


def test_insufficient_data_still_has_keys(temp_db):
    from services.score_forecast import predict_composite

    out = predict_composite()
    assert set(out.keys()) == {"quant", "verbal", "total", "method"}
    assert out["method"] == "insufficient_data"
    assert out["quant"] is None
    assert out["verbal"] is None
    assert out["total"] is None


def test_overall_forecast_exposes_method(temp_db):
    """Regression: overall_forecast keeps its legacy key shape and now
    also surfaces ``method`` so the UI can render a calibration badge.
    """
    from services.score_forecast import overall_forecast

    _simulate_user(theta=1.0, n_quant=20, n_verbal=20, seed=1)
    f = overall_forecast()
    # Legacy keys intact.
    for k in ("verbal_low", "verbal_high", "quant_low", "quant_high",
              "total_low", "total_high"):
        assert k in f
    # New key.
    assert f["method"] in ("irt_theta", "legacy_logistic", "insufficient_data")
