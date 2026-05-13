"""
Timing analytics — per-subtype response-time percentiles and outliers.

Phase 2 E3. Reads from the ``response`` table and groups by
``question.subtype``. Prefers ``response.time_to_answer_ms`` when present,
falling back to ``time_spent_seconds * 1000`` for rows that pre-date the
``_024_response_time_ms`` migration.

All public helpers are safe on empty data (return ``{}`` or ``[]``) so the
insights screen can render a blank panel without try/except.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from math import sqrt
from typing import Dict, List, Optional

from models.database import Question, Response


def _bucket_ms(resp: Response) -> Optional[int]:
    """Pick the best-available millisecond reading for a Response row."""
    ms = resp.time_to_answer_ms
    if ms is not None and ms > 0:
        return int(ms)
    secs = resp.time_spent_seconds or 0
    if secs > 0:
        return int(secs * 1000)
    return None


def _percentile(sorted_values: List[int], pct: float) -> float:
    """Linear-interpolated percentile on a pre-sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (len(sorted_values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return float(sorted_values[f])
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1


def _collect(user_id: str, days: int):
    """Yield (subtype, ms) tuples for responses in the last ``days``.

    ``user_id`` is accepted for forward compatibility with multi-user DBs;
    today every row is implicitly the local user so the filter is a no-op
    but the signature is stable.
    """
    _ = user_id  # reserved for future per-user filtering
    cutoff = datetime.now() - timedelta(days=days)
    query = (Response
             .select(Response, Question)
             .join(Question, on=(Response.question == Question.id))
             .where(Response.created_at >= cutoff))
    for r in query:
        ms = _bucket_ms(r)
        if ms is None:
            continue
        subtype = r.question.subtype or "unknown"
        yield subtype, ms, r


def per_subtype_p50_p90(user_id: str = "local", days: int = 30) -> Dict[str, Dict]:
    """Return ``{subtype: {"p50": ms, "p90": ms, "mean": ms, "n": count}}``.

    Empty-safe: returns ``{}`` when no qualifying responses exist.
    """
    buckets: Dict[str, List[int]] = defaultdict(list)
    for subtype, ms, _r in _collect(user_id, days):
        buckets[subtype].append(ms)

    out: Dict[str, Dict] = {}
    for subtype, values in buckets.items():
        values.sort()
        n = len(values)
        mean = sum(values) / n
        out[subtype] = {
            "p50": _percentile(values, 0.50),
            "p90": _percentile(values, 0.90),
            "mean": mean,
            "n": n,
        }
    return out


def outliers(user_id: str = "local", days: int = 30,
             z_threshold: float = 2.0) -> List[Response]:
    """Return Response rows ``>= z_threshold`` SDs above their subtype mean.

    Uses population standard deviation (divide by n, not n-1). Subtypes
    with fewer than 2 samples are skipped — a single data point has no
    meaningful spread. Empty-safe.
    """
    # Materialise per subtype so we can compute mean/SD before flagging.
    buckets: Dict[str, List] = defaultdict(list)  # subtype -> [(ms, resp)]
    for subtype, ms, r in _collect(user_id, days):
        buckets[subtype].append((ms, r))

    flagged: List[Response] = []
    for subtype, pairs in buckets.items():
        if len(pairs) < 2:
            continue
        values = [ms for ms, _r in pairs]
        n = len(values)
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        sd = sqrt(variance)
        if sd <= 0:
            continue
        for ms, r in pairs:
            z = (ms - mean) / sd
            if z >= z_threshold:
                flagged.append(r)
    return flagged
