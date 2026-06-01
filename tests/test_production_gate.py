"""CI regression gate: the shipped seed must pass every corruption-class audit.

This is the backstop for WS-A..E — if any future change reintroduces an option
graft, a phantom figure, a relived exact-duplicate, or a GRE-shape violation,
this test fails. Mirrors scripts/run_all_audits.py.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SEED = "data/gre_mock.db"
pytestmark = pytest.mark.skipif(not os.path.exists(SEED), reason="seed db absent")


def test_no_option_graft_regression():
    from scripts.run_all_audits import _gate_option_graft
    count, detail = _gate_option_graft(SEED)
    assert count == 0, f"option-graft suspects reappeared: {detail}"


def test_no_phantom_figures():
    from scripts.run_all_audits import _gate_figures
    count, detail = _gate_figures(SEED)
    assert count == 0, f"live phantom figures: {detail}"


def test_no_relived_exact_dupes():
    from scripts.run_all_audits import _gate_exact_dupes_retired
    count, detail = _gate_exact_dupes_retired(SEED)
    assert count == 0, f"retired exact-dupes are live again: {detail}"


def test_gre_shape_faithfulness():
    from scripts.run_all_audits import _gate_faithfulness
    count, detail = _gate_faithfulness(SEED)
    assert count == 0, f"GRE shape violations: {detail}"
