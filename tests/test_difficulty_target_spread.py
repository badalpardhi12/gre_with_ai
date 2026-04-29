"""Regression test for the difficulty_target uniformity bug.

Before the backfill fix, Princeton/Manhattan/legacy-ai_generated rows
were all stamped ``difficulty_target=3`` at persist time, which
collapsed ``services.question_bank``'s easy/hard filters to empty sets.

The fix lives in two places:
  * ``scripts/backfill_difficulty_target.py`` — spread existing rows
    via within-(source, subtype) prompt-length quintiles.
  * ``scripts/persist_princeton.py::_estimate_difficulty`` — future
    persists use a subtype-aware threshold curve instead of a hardcoded 3.

This test guards both ends: the DB should show a spread, and the
estimator should return varied difficulties for varied inputs.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _live_difficulty_counts(db_path: Path) -> dict:
    if not db_path.exists():
        pytest.skip(f"DB not present: {db_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT difficulty_target, COUNT(*) FROM question "
            "WHERE status='live' GROUP BY difficulty_target"
        ).fetchall()
    finally:
        conn.close()
    return {int(d): int(c) for d, c in rows}


def test_live_question_difficulty_has_spread():
    """Live pool must cover at least difficulties 2, 3, 4 so that
    ``question_bank`` can satisfy both easy and hard section requests.
    """
    db = PROJECT_ROOT / "data" / "gre_user.db"
    counts = _live_difficulty_counts(db)
    assert 3 in counts, f"no medium-difficulty live items? counts={counts}"
    # Must have easy (<=2) and hard (>=4) representatives.
    easy = sum(c for d, c in counts.items() if d <= 2)
    hard = sum(c for d, c in counts.items() if d >= 4)
    medium = counts.get(3, 0)
    total = sum(counts.values())
    assert easy > 0, f"no easy items (d<=2); counts={counts}"
    assert hard > 0, f"no hard items (d>=4); counts={counts}"
    # Sanity: no single difficulty should hold more than 80% of the pool.
    assert medium / total < 0.8, (
        f"difficulty_target suspiciously uniform: {counts}"
    )


def test_difficulty_spread_per_affected_source():
    """Princeton, Manhattan, ai_generated each need more than one
    difficulty represented. Prior bug had all three at uniform 3.
    """
    db = PROJECT_ROOT / "data" / "gre_user.db"
    if not db.exists():
        pytest.skip(f"DB not present: {db}")
    conn = sqlite3.connect(str(db))
    try:
        for source in ("princeton_2012", "manhattan_5lb_2018", "ai_generated"):
            rows = conn.execute(
                "SELECT difficulty_target, COUNT(*) FROM question "
                "WHERE source=? AND status='live' "
                "GROUP BY difficulty_target",
                (source,),
            ).fetchall()
            distinct = {int(d) for d, _ in rows}
            assert len(distinct) >= 3, (
                f"{source}: expected >=3 distinct difficulties "
                f"(was uniform before fix); got {distinct}"
            )
    finally:
        conn.close()


def test_persist_princeton_difficulty_estimator_is_varied():
    """The estimator used by future persists must return varied values
    for varied inputs, not hardcoded 3.
    """
    import sys
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from scripts.persist_princeton import _estimate_difficulty

    # Very short TC -> easy.
    assert _estimate_difficulty("tc", "a", "") <= 2
    # Very long TC -> hard.
    long_tc = "This is " + ("a long and intricate stem. " * 20)
    assert _estimate_difficulty("tc", long_tc, "") >= 4
    # RC single with no stimulus, short -> easy.
    assert _estimate_difficulty("rc_single", "short?", "") <= 2
    # Same question with a heavy stimulus -> harder.
    heavy_stim = "Passage text. " * 30
    assert _estimate_difficulty("rc_single", "short?", heavy_stim) >= 3
