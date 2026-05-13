"""
Score forecast: predict GRE scaled scores based on user's historical
performance.

Two paths live side by side:

* **IRT theta path (preferred, Phase 4 P2):** once ``recalibrate_irt``
  has populated ``Question.irt_a_estimate`` / ``irt_b_estimate`` for a
  reasonable fraction of answered items, we map the user's theta (from
  ``services.rating_service.get_user_theta``) directly onto the ETS
  130–170 scale using a per-measure piecewise-linear concordance.

* **Legacy logistic path:** falls back to the pre-P2 3-band logistic
  heuristic keyed on ``difficulty_target`` when IRT coverage is thin
  (>80% of attempted items missing IRT estimates) or rating-service
  returns no theta signal.

The legacy function signatures (``predict_scaled_score``,
``overall_forecast``, ``forecast_history``, ``measure_accuracy_by_difficulty``)
are preserved for backward-compatibility with the Insights and Today
screens. New callers should prefer ``predict_composite``.
"""
from collections import Counter
from typing import Optional, Tuple

from models.database import (
    Question, Response, MasteryRecord,
)


# ── IRT coverage thresholds ──────────────────────────────────────────
#
# If more than this fraction of the user's graded responses point at
# items without IRT estimates, we decline the IRT path. Keeps the
# forecast honest while the dataset is still being calibrated.
_IRT_COVERAGE_FLOOR = 0.20  # need ≥20% of responses to have IRT params

# Minimum graded-response count to produce any forecast at all.
_MIN_ATTEMPTS = 10


# ── Public: legacy path (unchanged) ──────────────────────────────────


def measure_accuracy_by_difficulty(measure: str, user_id: str = "local") -> dict:
    """Return {difficulty: (correct, total)} from response history.

    Uses an explicit join + projected columns so we read each row once
    instead of lazy-loading r.question.difficulty_target N times.
    """
    rows = (Response
            .select(Response.is_correct, Question.difficulty_target)
            .join(Question)
            .where((Question.measure == measure) &
                   (Response.is_correct.is_null(False)))
            .tuples())
    correct = Counter()
    total = Counter()
    for is_correct, difficulty in rows:
        total[difficulty] += 1
        if is_correct:
            correct[difficulty] += 1
    return {d: (correct.get(d, 0), total.get(d, 0)) for d in (1, 2, 3, 4, 5)}


def predict_scaled_score(measure: str, user_id: str = "local"):
    """Return (low, high) predicted scaled score 130-170 — or (None, None)
    when there is too little response history to predict honestly.

    Heuristic: weight accuracy at each difficulty band.
    """
    by_diff = measure_accuracy_by_difficulty(measure, user_id)
    total_attempted = sum(t for c, t in by_diff.values())
    if total_attempted < _MIN_ATTEMPTS:
        return (None, None)  # not enough data — caller should show empty state

    # Weighted accuracy: hard questions worth more
    weights = {1: 0.6, 2: 0.8, 3: 1.0, 4: 1.3, 5: 1.6}
    weighted_correct = 0.0
    weighted_total = 0.0
    for d, (c, t) in by_diff.items():
        w = weights[d]
        weighted_correct += c * w
        weighted_total += t * w

    if weighted_total == 0:
        return (None, None)

    pct = weighted_correct / weighted_total

    # Map weighted accuracy to scaled score
    if pct >= 0.95:
        center = 169
    elif pct >= 0.85:
        center = 165
    elif pct >= 0.75:
        center = 160
    elif pct >= 0.65:
        center = 156
    elif pct >= 0.50:
        center = 151
    elif pct >= 0.35:
        center = 145
    else:
        center = 138

    # Confidence band narrows with more data
    if total_attempted >= 100:
        spread = 2
    elif total_attempted >= 50:
        spread = 3
    elif total_attempted >= 25:
        spread = 4
    else:
        spread = 5

    low = max(130, center - spread)
    high = min(170, center + spread)
    return (low, high)


# ── Public: IRT-theta path (Phase 4 P2) ──────────────────────────────


# Piecewise-linear anchor points approximating the ETS theta→scaled
# concordance. Values were chosen to pass through the task-specified
# anchors: theta=-1 → 150, theta=0 → 155, theta=+1 → 163, theta=+2 → 167,
# theta=+3 → 170. Quant is calibrated slightly hotter than Verbal at the
# top end because ETS Quant concordance compresses faster above 165.
#
# Anchors are kept as sorted (theta, scaled) pairs so
# ``predict_from_theta`` can linearly interpolate inside the list and
# clamp cleanly outside it.
_ETS_ANCHORS_QUANT: Tuple[Tuple[float, float], ...] = (
    (-3.0, 130.0),
    (-2.0, 140.0),
    (-1.0, 150.0),
    ( 0.0, 155.0),
    ( 1.0, 163.0),
    ( 2.0, 167.0),
    ( 3.0, 170.0),
)

# Verbal: the task spec says theta=+2 → 165 for Verbal vs 167 for Quant;
# keep the low end identical and taper slightly earlier above theta=+1.
_ETS_ANCHORS_VERBAL: Tuple[Tuple[float, float], ...] = (
    (-3.0, 130.0),
    (-2.0, 140.0),
    (-1.0, 150.0),
    ( 0.0, 155.0),
    ( 1.0, 162.0),
    ( 2.0, 165.0),
    ( 3.0, 169.0),
)


def _anchors_for(measure: str) -> Tuple[Tuple[float, float], ...]:
    m = (measure or "").lower()
    if m == "quant":
        return _ETS_ANCHORS_QUANT
    return _ETS_ANCHORS_VERBAL  # verbal default — AWA doesn't use this path


def predict_from_theta(theta: float, measure: str) -> int:
    """Map a user theta on [-3, +3] onto the ETS [130, 170] scaled score.

    Piecewise-linear interpolation between published concordance
    anchors (see module docstring). Out-of-range theta is clamped to
    the endpoints; output is rounded to the nearest integer and
    clamped to [130, 170].

    Separate coefficients for quant vs verbal — ETS Quant compresses
    faster than Verbal at the top end.
    """
    anchors = _anchors_for(measure)

    # Clamp below / above the anchor range.
    if theta <= anchors[0][0]:
        scaled = anchors[0][1]
    elif theta >= anchors[-1][0]:
        scaled = anchors[-1][1]
    else:
        scaled = anchors[-1][1]  # default if loop doesn't hit (shouldn't)
        for i in range(len(anchors) - 1):
            t0, s0 = anchors[i]
            t1, s1 = anchors[i + 1]
            if t0 <= theta <= t1:
                # Linear interpolation inside this segment.
                if t1 == t0:
                    scaled = s0
                else:
                    frac = (theta - t0) / (t1 - t0)
                    scaled = s0 + frac * (s1 - s0)
                break

    return int(max(130, min(170, round(scaled))))


def _irt_coverage_for(measure: str) -> Tuple[int, int]:
    """Return (n_with_irt, n_total) of a user's graded responses in a measure.

    We count each ``Response`` (not unique questions) because the
    coverage decision should weight items the user actually sees —
    a rarely-answered well-calibrated item shouldn't outvote a
    heavily-drilled uncalibrated one.
    """
    rows = (Response
            .select(Question.irt_b_estimate)
            .join(Question)
            .where((Question.measure == measure) &
                   (Response.is_correct.is_null(False)))
            .tuples())
    total = 0
    with_irt = 0
    for (irt_b,) in rows:
        total += 1
        if irt_b is not None:
            with_irt += 1
    return with_irt, total


def _theta_from_irt(measure: str, user_id: str = "local") -> Optional[float]:
    """Estimate theta for one measure using IRT-calibrated items only.

    Method: maximum-likelihood fit under the 2PL model against all
    the user's graded responses that target items with both
    ``irt_a_estimate`` and ``irt_b_estimate``. Uses a simple bisection
    search on the score-equation (sum_i a_i * (y_i - p_i(theta)) = 0),
    which is monotone decreasing in theta for 2PL and so has a unique
    root on [-6, +6].

    Returns None when there's no IRT-calibrated response history.
    """
    import math as _math

    rows = (Response
            .select(Question.irt_a_estimate,
                    Question.irt_b_estimate,
                    Response.is_correct)
            .join(Question)
            .where((Question.measure == measure) &
                   (Response.is_correct.is_null(False)) &
                   (Question.irt_b_estimate.is_null(False)))
            .tuples())

    items = []
    n_correct = 0
    for a, b, is_correct in rows:
        a_eff = float(a) if a is not None else 1.0
        # Clamp discrimination to a sane range — Phase-1 girth output
        # can emit wild a's on sparse items.
        a_eff = max(0.2, min(2.5, a_eff))
        b_eff = max(-4.0, min(4.0, float(b)))
        items.append((a_eff, b_eff, 1.0 if is_correct else 0.0))
        if is_correct:
            n_correct += 1

    if not items:
        return None

    # Edge cases: all-correct / all-wrong blows up the score equation
    # (no finite root). Clamp to the concordance domain.
    if n_correct == 0:
        return -3.0
    if n_correct == len(items):
        return 3.0

    def _score(theta: float) -> float:
        """Score-equation derivative: positive when theta is too low."""
        s = 0.0
        for a, b, y in items:
            # p = 1 / (1 + exp(-a*(theta - b)))
            z = a * (theta - b)
            if z > 40:
                p = 1.0
            elif z < -40:
                p = 0.0
            else:
                p = 1.0 / (1.0 + _math.exp(-z))
            s += a * (y - p)
        return s

    # Bisection on [-6, +6]. Score is monotone decreasing in theta
    # (derivative is -sum a_i^2 * p * (1-p) ≤ 0), so exactly one root.
    lo, hi = -6.0, 6.0
    f_lo = _score(lo)
    f_hi = _score(hi)
    if f_lo < 0:
        return -3.0
    if f_hi > 0:
        return 3.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        f_mid = _score(mid)
        if f_mid > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-4:
            break
    theta_hat = 0.5 * (lo + hi)
    return max(-3.0, min(3.0, theta_hat))


def predict_composite(user_id: str = "local") -> dict:
    """Return a calibrated score forecast.

    Preferred method: IRT-theta mapped through per-measure ETS
    concordance. Falls back to the legacy logistic path when IRT
    coverage is thin or the rating-service theta is unavailable.

    Output keys are always present:
        {
            "quant":  int in [130, 170] or None,
            "verbal": int in [130, 170] or None,
            "total":  int in [260, 340] or None,
            "method": "irt_theta" | "legacy_logistic" | "insufficient_data",
        }
    """
    from services import rating_service

    # Pull the rating-service theta (single user today, but plumbed).
    try:
        rating_theta = rating_service.get_user_theta(user_id=user_id)
    except Exception:
        rating_theta = None

    quant: Optional[int] = None
    verbal: Optional[int] = None
    method = "insufficient_data"

    # Do we have enough IRT-calibrated coverage to trust the theta path?
    def _coverage_ok(measure: str) -> bool:
        with_irt, total = _irt_coverage_for(measure)
        if total < _MIN_ATTEMPTS:
            return False
        return (with_irt / total) >= _IRT_COVERAGE_FLOOR

    q_ok = _coverage_ok("quant")
    v_ok = _coverage_ok("verbal")

    use_irt = (rating_theta is not None) and (q_ok or v_ok)

    if use_irt:
        # Per-measure theta. If a measure has no IRT coverage, blend in
        # the rating-service theta so we still emit a number rather
        # than a hole in the forecast.
        q_theta = _theta_from_irt("quant", user_id=user_id)
        v_theta = _theta_from_irt("verbal", user_id=user_id)
        if q_theta is None:
            q_theta = rating_theta
        if v_theta is None:
            v_theta = rating_theta
        quant = predict_from_theta(q_theta, "quant")
        verbal = predict_from_theta(v_theta, "verbal")
        method = "irt_theta"
    else:
        # Legacy path — midpoint of the (low, high) band for the single-
        # number composite output. Low/high still available via
        # overall_forecast() for the UI.
        q_lo, q_hi = predict_scaled_score("quant", user_id)
        v_lo, v_hi = predict_scaled_score("verbal", user_id)
        if q_lo is not None and q_hi is not None:
            quant = int(round((q_lo + q_hi) / 2))
        if v_lo is not None and v_hi is not None:
            verbal = int(round((v_lo + v_hi) / 2))
        if quant is not None or verbal is not None:
            method = "legacy_logistic"

    total: Optional[int] = None
    if quant is not None and verbal is not None:
        total = quant + verbal

    return {
        "quant": quant,
        "verbal": verbal,
        "total": total,
        "method": method,
    }


# ── Public: combined forecast (legacy-shape, IRT-aware low/high) ─────


def _band_around(center: int, spread: int) -> Tuple[int, int]:
    return max(130, center - spread), min(170, center + spread)


def overall_forecast(user_id: str = "local") -> dict:
    """Combined forecast: verbal + quant + total with low/high bands.

    When the IRT-theta path is active, we build a narrow confidence
    band around the IRT point estimate (spread scales with response
    count). When it isn't, we fall back to the legacy logistic bands.
    None values mean "not enough data" — render an empty state.
    """
    composite = predict_composite(user_id)
    method = composite["method"]

    if method == "irt_theta":
        # Re-derive spread from total response count, same schedule as
        # the legacy path so the UI's behaviour is continuous across
        # the switchover.
        q_with_irt, q_total = _irt_coverage_for("quant")
        v_with_irt, v_total = _irt_coverage_for("verbal")
        total_attempted = q_total + v_total

        if total_attempted >= 100:
            spread = 2
        elif total_attempted >= 50:
            spread = 3
        elif total_attempted >= 25:
            spread = 4
        else:
            spread = 5

        q_center = composite["quant"]
        v_center = composite["verbal"]
        q_lo = q_hi = v_lo = v_hi = None
        if q_center is not None:
            q_lo, q_hi = _band_around(q_center, spread)
        if v_center is not None:
            v_lo, v_hi = _band_around(v_center, spread)

        def _add(a, b):
            if a is None or b is None:
                return None
            return a + b

        return {
            "verbal_low": v_lo, "verbal_high": v_hi,
            "quant_low": q_lo, "quant_high": q_hi,
            "total_low": _add(v_lo, q_lo),
            "total_high": _add(v_hi, q_hi),
            "method": method,
        }

    # Legacy shape.
    v_low, v_high = predict_scaled_score("verbal", user_id)
    q_low, q_high = predict_scaled_score("quant", user_id)

    def _add(a, b):
        if a is None or b is None:
            return None
        return a + b

    return {
        "verbal_low": v_low, "verbal_high": v_high,
        "quant_low": q_low, "quant_high": q_high,
        "total_low": _add(v_low, q_low),
        "total_high": _add(v_high, q_high),
        "method": method,
    }


def forecast_history(user_id: str = "local", n: int = 10):
    """Return the last N completed-session combined-score midpoints, for the
    Today-screen sparkline.

    Pulls from `ScoringResult` rather than recomputing on the fly so the
    history reflects what the user actually saw, not retroactive estimates.
    """
    from models.database import ScoringResult, Session as DBSession
    rows = (ScoringResult
            .select(ScoringResult,
                    DBSession.created_at)
            .join(DBSession,
                  on=(ScoringResult.session == DBSession.id))
            .where(DBSession.state == "completed")
            .order_by(DBSession.created_at.desc())
            .limit(n))
    out = []
    for r in rows:
        v_lo = r.verbal_estimated_low
        v_hi = r.verbal_estimated_high
        q_lo = r.quant_estimated_low
        q_hi = r.quant_estimated_high
        if None in (v_lo, v_hi, q_lo, q_hi):
            continue
        out.append((v_lo + v_hi) / 2 + (q_lo + q_hi) / 2)
    return list(reversed(out))   # chronological for the sparkline
