"""Regression test for migration 047 — q5395 explanation fix.

User report #49 (the SECOND on q5395): the answer key is CORRECT (perimeter
40 → area ∈ (0,100] → {96,75,100,51,25} achievable, {104,110} not), but the
shipped explanation was a broken ai_synthetic_v2 ramble ("25 = 5×15",
"51 ≈ 3.14×16.86", self-contradictory text). Migration 047 rewrites the
explanation; the key is unchanged.
"""
from __future__ import annotations

import os

import pytest

SEED = "data/gre_mock.db"
pytestmark = pytest.mark.skipif(
    not os.path.exists(SEED) or os.path.getsize(SEED) < 1024,
    reason="seed db absent or LFS pointer",
)


def test_q5395_key_unchanged_and_correct():
    import sqlite3
    c = sqlite3.connect(SEED)
    try:
        correct = sorted(r[0] for r in c.execute(
            "SELECT option_label FROM questionoption "
            "WHERE question_id=5395 AND is_correct=1"))
        assert correct == ["A", "B", "C", "F", "G"], correct
    finally:
        c.close()


def test_q5395_explanation_no_longer_garbled():
    import sqlite3
    c = sqlite3.connect(SEED)
    try:
        e = c.execute("SELECT explanation FROM question WHERE id=5395").fetchone()[0]
        # The specific arithmetic errors from the old explanation are gone.
        assert "5×15 ✓ (0" not in e          # the "25 = 5×15" fragment
        assert "3.14×16.86" not in e          # the bogus "51 ≈" fragment
        # The corrected, teaching content is present.
        assert "10" in e and ("100" in e)     # max area / interval
        assert "integer" in e.lower()          # addresses the non-integer trap
    finally:
        c.close()


def test_q5395_areas_achievable_sympy():
    sp = pytest.importorskip("sympy")
    l = sp.symbols("l", positive=True)
    achievable = {a for a in (96, 75, 100, 104, 110, 51, 25)
                  if any(s.is_real and 0 < s < 20
                         for s in sp.solve(sp.Eq(l * (20 - l), a), l))}
    assert achievable == {96, 75, 100, 51, 25}
