"""Regression test for migration 046 — q3116 truncated-option repair.

The 2026-06-21 quant answer-key re-validation sweep found q3116 (manhattan
mcq_single): the simplification of (a/b)/(c/d + e/f) is adf/(bcf+bde), but
the marked-correct option E was truncated to just the denominator
``bcf + bde``. Migration 046 restores the dropped numerator. (Same
"answer not among the options" class as the q5420 user report, but here the
fix is a repair, not a retire, because the correction is unambiguous.)
"""
from __future__ import annotations

import os

import pytest

SEED = "data/gre_mock.db"
pytestmark = pytest.mark.skipif(
    not os.path.exists(SEED) or os.path.getsize(SEED) < 1024,
    reason="seed db absent or LFS pointer",
)


def test_q3116_option_e_is_full_fraction():
    import sqlite3
    c = sqlite3.connect(SEED)
    try:
        e = c.execute("SELECT option_text, is_correct FROM questionoption "
                      "WHERE question_id=3116 AND option_label='E'").fetchone()
        assert e is not None, "q3116 option E missing"
        # The numerator adf must be present — not the bare denominator.
        assert "adf" in e[0], f"option E not repaired: {e[0]!r}"
        assert e[1] == 1, "option E should be the marked-correct option"
        n_correct = c.execute("SELECT COUNT(*) FROM questionoption "
                              "WHERE question_id=3116 AND is_correct=1").fetchone()[0]
        assert n_correct == 1, f"q3116 should have exactly 1 correct option, has {n_correct}"
    finally:
        c.close()


def test_q3116_option_e_matches_symbolic_truth():
    """The repaired option E equals the true simplification (sympy)."""
    sp = pytest.importorskip("sympy")
    a, b, c, d, e, f = sp.symbols("a b c d e f", positive=True)
    correct = sp.simplify((a / b) / (c / d + e / f))
    repaired_E = a * d * f / (b * c * f + b * d * e)  # adf/(bcf+bde)
    assert sp.simplify(correct - repaired_E) == 0
