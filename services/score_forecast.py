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
    """Return {difficulty: (correct, total)} from response history."""
    rows = (Response
            .select(Response, Question)
            .join(Question)
            .where((Question.measure == measure) &
                   (Response.is_correct.is_null(False))))
    correct = Counter()
    total = Counter()
    for r in rows:
        d = r.question.difficulty_target
        total[d] += 1
        if r.is_correct:
            correct[d] += 1
    return {d: (correct.get(d, 0), total.get(d, 0)) for d in (1, 2, 3, 4, 5)}


def predict_scaled_score(measure: str, user_id: str = "local") -> Tuple[int, int]:
    """Return (low, high) predicted scaled score 130-170.

    Heuristic: weight accuracy at each difficulty band.
    """
    by_diff = measure_accuracy_by_difficulty(measure, user_id)
    total_attempted = sum(t for c, t in by_diff.values())
    if total_attempted < 10:
        return (140, 160)  # too little data → wide band

    # Weighted accuracy: hard questions worth more
    weights = {1: 0.6, 2: 0.8, 3: 1.0, 4: 1.3, 5: 1.6}
    weighted_correct = 0.0
    weighted_total = 0.0
    for d, (c, t) in by_diff.items():
        w = weights[d]
        weighted_correct += c * w
        weighted_total += t * w

    if weighted_total == 0:
        return (140, 160)

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
    """Combined forecast: verbal + quant + total."""
    v_low, v_high = predict_scaled_score("verbal", user_id)
    q_low, q_high = predict_scaled_score("quant", user_id)
    return {
        "verbal_low": v_low, "verbal_high": v_high,
        "quant_low": q_low, "quant_high": q_high,
        "total_low": v_low + q_low,
        "total_high": v_high + q_high,
    }
