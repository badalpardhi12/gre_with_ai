"""
Score forecast: predict GRE scaled scores based on user's historical accuracy
across difficulty bands.

Uses a simple logistic-regression-style mapping calibrated against ETS conversion tables.
"""
from collections import Counter
from typing import Optional, Tuple

from models.database import (
    Question, Response, MasteryRecord,
)


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
    if total_attempted < 10:
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
    # 0.95+ → 168-170
    # 0.85+ → 162-167
    # 0.75+ → 158-163
    # 0.65+ → 154-159
    # 0.50+ → 148-154
    # 0.35+ → 142-148
    # < 0.35 → 130-142
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


def overall_forecast(user_id: str = "local") -> dict:
    """Combined forecast: verbal + quant + total. None values mean
    "not enough data" — render an empty state in the UI instead of numbers.
    """
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
