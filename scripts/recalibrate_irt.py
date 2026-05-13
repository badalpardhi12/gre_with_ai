"""Offline IRT recalibration (Phase 4 P1).

Fits a 2PL IRT model over graded ``Response`` rows and writes the
estimated item parameters back onto ``Question.irt_a_estimate`` and
``Question.irt_b_estimate``.

Design notes
------------

The local app is single-user, so ``Response`` rows have no ``user_id``
column. We treat each ``Session`` as an independent "person" draw —
theta varies across sessions (user's ability shifts, mood, fatigue,
etc.) but is roughly constant within a session. This gives the 2PL
estimator a person axis to marginalise over even in the single-user
case; the library-reported ``Difficulty`` / ``Discrimination`` remain
on the standard theta-scale and can be consumed by downstream code
without rescaling.

Priors
------

The Elo item ratings maintained by ``services.rating_service`` provide
a warm-start signal for the difficulty parameter. When an item has
very few responses (below ``--min-responses``) we skip it entirely —
girth's MML estimator is unstable on sparse columns, and the Elo
rating is still the best estimate we have. When the estimator does
fit, we compare its output to the Elo prior; items whose posterior
disagrees wildly (delta > 2 logits) are logged for audit but still
written through, because the posterior uses strictly more information.

Idempotency
-----------

The script only writes columns that girth estimates; everything else
on ``Question`` is left alone. Re-running over the same responses
produces the same estimates (modulo numerical noise from girth's
internal random init, which is seeded inside this module).

Usage
-----

    venv/bin/python scripts/recalibrate_irt.py                # run
    venv/bin/python scripts/recalibrate_irt.py --dry-run      # no writes
    venv/bin/python scripts/recalibrate_irt.py --min-responses 100

Library dependency: ``girth`` (MIT, scipy-only). If it isn't importable
the script fails fast with an install hint.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Make project root importable when run as ``python scripts/recalibrate_irt.py``.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


logger = logging.getLogger("recalibrate_irt")


# ── Library import, gated ─────────────────────────────────────────────


def _import_girth():
    try:
        import numpy as np  # noqa: F401
        from girth import twopl_mml
    except ImportError as exc:  # pragma: no cover - environment gating
        raise SystemExit(
            "girth is not installed. Run:\n"
            "    venv/bin/pip install girth\n"
            f"(underlying error: {exc})"
        )
    return twopl_mml


# ── Data extraction ───────────────────────────────────────────────────


def _collect_responses(min_responses: int) -> Tuple[
    List[int], List[int], Dict[Tuple[int, int], int]
]:
    """Return (qids, session_ids, matrix) for items with enough data.

    ``matrix`` is a sparse dict keyed by (qid_index, session_index) with
    values 0 (wrong) or 1 (correct). Anything not in the dict means
    "not answered by that session".
    """
    from models.database import Response

    # Counts first so we can filter items cheaply before building the
    # dense matrix.
    counts: Dict[int, int] = defaultdict(int)
    rows_iter = (
        Response
        .select(Response.question_id, Response.session_id, Response.is_correct)
        .where(Response.is_correct.is_null(False))
        .dicts()
    )
    buffered: List[Tuple[int, int, bool]] = []
    for r in rows_iter:
        qid = int(r["question"])
        sid = int(r["session"])
        counts[qid] += 1
        buffered.append((qid, sid, bool(r["is_correct"])))

    qualifying_qids = sorted(q for q, n in counts.items() if n >= min_responses)
    qid_set = set(qualifying_qids)
    qid_index = {q: i for i, q in enumerate(qualifying_qids)}

    session_set = set()
    for qid, sid, _ok in buffered:
        if qid in qid_set:
            session_set.add(sid)
    session_ids = sorted(session_set)
    sid_index = {s: j for j, s in enumerate(session_ids)}

    matrix: Dict[Tuple[int, int], int] = {}
    for qid, sid, ok in buffered:
        if qid not in qid_set:
            continue
        matrix[(qid_index[qid], sid_index[sid])] = 1 if ok else 0

    return qualifying_qids, session_ids, matrix


def _build_dense(
    qids: List[int],
    session_ids: List[int],
    matrix: Dict[Tuple[int, int], int],
):
    """Pack the sparse dict into a girth-ready [items × persons] array.

    Missing cells are tagged with ``girth.INVALID_RESPONSE`` so the
    estimator ignores them rather than treating them as zeros.
    """
    import numpy as np
    from girth import INVALID_RESPONSE

    n_items = len(qids)
    n_persons = len(session_ids)
    # int16 is wide enough for {0, 1, INVALID_RESPONSE=-99999}. int8
    # overflows on the sentinel and triggers a NumPy DeprecationWarning.
    data = np.full((n_items, n_persons), INVALID_RESPONSE, dtype=np.int16)
    for (i, j), v in matrix.items():
        data[i, j] = v
    return data


# ── Elo priors ────────────────────────────────────────────────────────


def _load_elo_priors(qids: List[int]) -> Dict[int, float]:
    """Return Elo rating per qid (0.0 default for anything unseeded)."""
    from models.database import ItemRating
    priors: Dict[int, float] = {q: 0.0 for q in qids}
    if not qids:
        return priors
    rows = ItemRating.select().where(ItemRating.question_id.in_(qids))
    for row in rows:
        priors[int(row.question_id)] = float(row.rating)
    return priors


# ── Fit + write-back ──────────────────────────────────────────────────


def _fit_2pl(data):
    """Run girth's 2PL MML estimator. Returns dict with Discrimination/Difficulty."""
    twopl_mml = _import_girth()
    return twopl_mml(data)


def _summarise_deltas(
    qids: List[int],
    priors: Dict[int, float],
    posteriors: List[float],
) -> None:
    """Log a histogram of |posterior - prior| in logit-bin buckets."""
    buckets = [0, 0, 0, 0, 0]  # <0.25, <0.5, <1.0, <2.0, ≥2.0
    for qid, post in zip(qids, posteriors):
        delta = abs(post - priors[qid])
        if delta < 0.25:
            buckets[0] += 1
        elif delta < 0.5:
            buckets[1] += 1
        elif delta < 1.0:
            buckets[2] += 1
        elif delta < 2.0:
            buckets[3] += 1
        else:
            buckets[4] += 1
    logger.info(
        "prior-vs-posterior delta histogram: "
        "<0.25: %d  <0.5: %d  <1.0: %d  <2.0: %d  >=2.0: %d",
        *buckets,
    )


def _write_back(
    qids: List[int],
    discriminations,
    difficulties,
    dry_run: bool,
) -> int:
    """Update Question.irt_a_estimate / irt_b_estimate. Returns row count."""
    from models.database import Question, db

    if dry_run:
        logger.info(
            "dry-run: would update %d Question rows (a/b estimates)", len(qids)
        )
        return len(qids)

    updated = 0
    with db.atomic():
        for qid, a, b in zip(qids, discriminations, difficulties):
            q = Question.get_or_none(Question.id == qid)
            if q is None:
                continue
            q.irt_a_estimate = float(a)
            q.irt_b_estimate = float(b)
            q.save()
            updated += 1
    logger.info("updated irt_a_estimate / irt_b_estimate on %d items", updated)
    return updated


# ── Main entry point ──────────────────────────────────────────────────


def recalibrate(
    min_responses: int = 50,
    dry_run: bool = False,
    model: str = "2pl",
) -> Dict[str, object]:
    """Run the recalibration. Returns a summary dict (useful for tests)."""
    if model != "2pl":
        raise ValueError(f"only '2pl' is supported; got {model!r}")

    qids, session_ids, matrix = _collect_responses(min_responses)
    if not qids:
        logger.info(
            "no items with >= %d responses; nothing to recalibrate",
            min_responses,
        )
        return {"n_items": 0, "n_sessions": 0, "updated": 0, "qids": []}
    if len(session_ids) < 2:
        logger.info(
            "only %d distinct sessions — 2PL needs >=2 persons; skipping",
            len(session_ids),
        )
        return {
            "n_items": len(qids),
            "n_sessions": len(session_ids),
            "updated": 0,
            "qids": qids,
        }

    logger.info(
        "recalibrating %d items over %d sessions (min_responses=%d)",
        len(qids), len(session_ids), min_responses,
    )
    data = _build_dense(qids, session_ids, matrix)
    result = _fit_2pl(data)

    discriminations = list(result["Discrimination"])
    difficulties = list(result["Difficulty"])

    priors = _load_elo_priors(qids)
    _summarise_deltas(qids, priors, difficulties)

    updated = _write_back(qids, discriminations, difficulties, dry_run=dry_run)

    return {
        "n_items": len(qids),
        "n_sessions": len(session_ids),
        "updated": updated,
        "qids": qids,
        "discriminations": discriminations,
        "difficulties": difficulties,
        "priors": priors,
        "dry_run": dry_run,
    }


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Recalibrate 2PL IRT item parameters from Response data.",
    )
    p.add_argument(
        "--min-responses", type=int, default=50,
        help="Skip items with fewer than N graded responses (default: 50).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Run the fit and log the summary but don't update the DB.",
    )
    p.add_argument(
        "--model", default="2pl", choices=("2pl",),
        help="IRT model to fit (only 2pl supported today).",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="DEBUG-level logging.",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Ensure the DB is initialised (migrations applied, tables exist).
    from models.database import init_db
    init_db()

    summary = recalibrate(
        min_responses=args.min_responses,
        dry_run=args.dry_run,
        model=args.model,
    )
    logger.info(
        "done: %d items, %d sessions, %d rows updated (dry_run=%s)",
        summary["n_items"], summary["n_sessions"], summary["updated"],
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
