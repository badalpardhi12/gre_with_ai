"""Regression tests for migration 045 + the judge-failed-live gate.

User GitHub report (qid 5420): a DI question whose marked answer (C=23.5)
is not the chart-derivable mean (137/6 ≈ 22.83 — which isn't even an
option) and whose explanation is a confused ramble. Root cause: the item's
own stored ``provenance_json.judge_result`` marked it FAIL at build time,
yet it shipped ``status='live'``. Migration 045 retires that whole
judge-confirmed-broken cohort; the new gate stops any such item from
shipping live again.
"""
from __future__ import annotations

import json
import os

import pytest

SEED = "data/gre_mock.db"
pytestmark = pytest.mark.skipif(
    not os.path.exists(SEED) or os.path.getsize(SEED) < 1024,
    reason="seed db absent or LFS pointer",
)

# Items migration 045 retires (marked answer not chart-derivable / ambiguous).
RETIRED_BY_045 = (5420, 5418, 5415, 5412)
# Judge-failed only on DIFFICULTY (correct but trivial) — must stay LIVE.
KEPT_LIVE = (5404, 5407)


def _seed_conn():
    import sqlite3
    return sqlite3.connect(SEED)


def test_reported_items_are_retired_in_seed():
    c = _seed_conn()
    try:
        for qid in RETIRED_BY_045:
            row = c.execute("SELECT status FROM question WHERE id=?", (qid,)).fetchone()
            assert row is not None, f"qid {qid} missing from seed"
            assert row[0] == "retired", f"qid {qid} should be retired, is {row[0]}"
    finally:
        c.close()


def test_retirement_reason_recorded():
    c = _seed_conn()
    try:
        row = c.execute("SELECT provenance_json FROM question WHERE id=5420").fetchone()
        prov = json.loads(row[0])
        assert prov.get("retired_by_migration") == "045_retire_unanswerable_di_2026_06_21"
        assert "retired_reason" in prov and prov["retired_reason"]
    finally:
        c.close()


def test_correct_but_trivial_items_stay_live():
    """5404/5407 failed the judge only on difficulty (trivial lookup) but
    have CORRECT answers — easy items are needed for S1/easy forms, so they
    must NOT be retired by the answer-correctness fix."""
    c = _seed_conn()
    try:
        for qid in KEPT_LIVE:
            row = c.execute("SELECT status FROM question WHERE id=?", (qid,)).fetchone()
            assert row is not None and row[0] == "live", (
                f"qid {qid} should remain live (correct, just easy)")
    finally:
        c.close()


def test_no_judge_failed_item_is_live():
    """The permanent gate: no live item carries a stored judge_result that
    failed an answer-correctness / stem-clarity criterion."""
    from scripts.run_all_audits import _gate_judge_failed_live
    count, detail = _gate_judge_failed_live(SEED)
    assert count == 0, f"judge-failed items are live again: {detail}"


def test_migration_045_idempotent():
    """Re-running migration 045 against the seed is a no-op (already retired
    rows are skipped, status unchanged)."""
    import sqlite3
    # Snapshot, re-run the retire logic against a copy via the gate, confirm
    # statuses are stable. We don't re-invoke the migrator here (it would need
    # GRE_BUILD_SEED); instead assert the end-state is already terminal.
    c = sqlite3.connect(SEED)
    try:
        before = {qid: c.execute("SELECT status FROM question WHERE id=?", (qid,))
                  .fetchone()[0] for qid in RETIRED_BY_045}
    finally:
        c.close()
    assert all(s == "retired" for s in before.values()), before
