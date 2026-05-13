"""Tests for scripts/recalibrate_irt.py (Phase 4 P1).

Covers:
    * Synthetic Rasch-data recovery: 20 items with known ``b`` in
      [-2, 2], 100 simulated users × 10 responses/user per item →
      MAE(recovered b, true b) < 0.3.
    * ``--dry-run`` leaves Question.irt_a_estimate / irt_b_estimate
      untouched but still logs a summary.
    * ``--min-responses`` filter: items below the threshold are
      skipped entirely.
    * Skipped gracefully if ``girth`` isn't importable in the env.
"""
from __future__ import annotations

import importlib
import math
import random

import pytest


# Skip the whole module if girth isn't installed — CI may run lean.
girth = pytest.importorskip("girth")


# ── Helpers ──────────────────────────────────────────────────────────


def _make_question(band=3, label="q"):
    from models.database import Question
    return Question.create(
        measure="quant", subtype="mcq_single",
        prompt=label,
        difficulty_target=band,
        time_target_seconds=90,
        concept_tags="[]", explanation="",
        status="live",
    )


def _simulate_rasch_responses(
    true_b, n_users=100, responses_per_user_per_item=10, seed=20260512,
):
    """Create Questions + Sessions + Responses from a known-b Rasch model.

    Each user gets one Session (so Session id == person id). Inside a
    session the user answers every item ``responses_per_user_per_item``
    times — we use repeated draws so the person axis has enough depth
    for girth's 2PL to converge even on a 20-item bank.

    Returns the list of qids in the order of ``true_b``.
    """
    from models.database import Question, Session, SectionResult, Response

    rng = random.Random(seed)
    # Create items.
    qids = []
    for i, b in enumerate(true_b):
        # Map true b onto the 1-5 band (only affects Elo seed, not truth).
        band = max(1, min(5, int(round(b)) + 3))
        q = _make_question(band=band, label=f"item-{i}-b{b:.2f}")
        qids.append(q.id)

    # Create one session per simulated user; draw theta once.
    for u in range(n_users):
        theta = rng.gauss(0.0, 1.0)
        sess = Session.create(
            test_type="drill", mode="learning",
            section_order="[]", state="completed",
        )
        sr = SectionResult.create(
            session=sess, section_name="drill", measure="quant",
            section_index=1, time_limit_seconds=0, question_ids="[]",
        )
        for i, b in enumerate(true_b):
            qid = qids[i]
            for _ in range(responses_per_user_per_item):
                p = 1.0 / (1.0 + math.exp(-(theta - b)))
                is_correct = rng.random() < p
                Response.create(
                    session=sess, section_result=sr, question=qid,
                    response_payload="{}",
                    is_correct=is_correct,
                    time_spent_seconds=10,
                )
    return qids


# ── Tests ────────────────────────────────────────────────────────────


def test_recovers_difficulty_within_mae_threshold(temp_db):
    """Synthetic Rasch data → recovered b within MAE < 0.3 of truth."""
    import numpy as np

    # Force a fresh import so the module binds to the temp-db peewee handle.
    import scripts.recalibrate_irt as rir
    importlib.reload(rir)

    true_b = np.linspace(-2.0, 2.0, 20)
    # 500 simulated users × 1 response per item → dense 500×20 matrix.
    # 2PL MML needs enough persons (not repeated trials by the same
    # person) for the marginal likelihood to pin down both a and b.
    qids = _simulate_rasch_responses(true_b, n_users=500,
                                     responses_per_user_per_item=1)

    summary = rir.recalibrate(min_responses=50, dry_run=False)

    assert summary["n_items"] == 20
    # Align recovered b to the order of qids we returned (script sorts qids).
    order = {q: i for i, q in enumerate(summary["qids"])}
    recovered = np.array([
        summary["difficulties"][order[qid]] for qid in qids
    ])
    mae = float(np.mean(np.abs(recovered - true_b)))
    print(f"\n[synthetic-MAE] recovered-vs-true b MAE = {mae:.4f}")
    assert mae < 0.3, (
        f"MAE {mae:.3f} exceeds 0.3 threshold; recovered={recovered.tolist()}"
    )

    # DB was actually written.
    from models.database import Question
    q = Question.get(Question.id == qids[0])
    assert q.irt_b_estimate is not None
    assert q.irt_a_estimate is not None


def test_dry_run_does_not_mutate_db(temp_db):
    """--dry-run leaves irt_a_estimate / irt_b_estimate NULL."""
    import numpy as np
    import scripts.recalibrate_irt as rir
    importlib.reload(rir)

    true_b = np.linspace(-1.5, 1.5, 10)
    qids = _simulate_rasch_responses(
        true_b, n_users=40, responses_per_user_per_item=5,
    )

    summary = rir.recalibrate(min_responses=20, dry_run=True)
    assert summary["updated"] == len(qids)
    assert summary["dry_run"] is True

    from models.database import Question
    for qid in qids:
        q = Question.get(Question.id == qid)
        assert q.irt_a_estimate is None, (
            f"dry-run wrote irt_a_estimate on qid {qid}"
        )
        assert q.irt_b_estimate is None, (
            f"dry-run wrote irt_b_estimate on qid {qid}"
        )


def test_min_responses_filter_excludes_sparse_items(temp_db):
    """Items below --min-responses are silently skipped."""
    import numpy as np
    import scripts.recalibrate_irt as rir
    importlib.reload(rir)

    # 5 "popular" items + 5 "sparse" items that shouldn't qualify.
    popular_b = np.linspace(-1.0, 1.0, 5)
    sparse_b = np.linspace(-1.0, 1.0, 5)

    popular_qids = _simulate_rasch_responses(
        popular_b, n_users=40, responses_per_user_per_item=3, seed=1,
    )
    sparse_qids = _simulate_rasch_responses(
        sparse_b, n_users=2, responses_per_user_per_item=1, seed=2,
    )

    # Each popular item has 40*3 = 120 responses.
    # Each sparse item has 2*1 = 2 responses.
    summary = rir.recalibrate(min_responses=50, dry_run=False)

    fit_qids = set(summary["qids"])
    assert set(popular_qids).issubset(fit_qids), (
        "popular items should qualify (120 responses each)"
    )
    assert not set(sparse_qids).intersection(fit_qids), (
        "sparse items should be excluded (2 responses each, threshold 50)"
    )


def test_cli_dry_run_smoke(temp_db, caplog):
    """`main(['--dry-run', '--min-responses', '10'])` returns 0 on empty DB."""
    import scripts.recalibrate_irt as rir
    importlib.reload(rir)

    rc = rir.main(["--dry-run", "--min-responses", "10"])
    assert rc == 0
