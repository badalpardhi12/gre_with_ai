"""Unit tests for ``_quant_figure_floor`` (Phase 1 R2).

The helper scales the Quant figure-bearing floor down when the live
pool is shallow, so a 40-item pool doesn't get three of its items
jammed into every section (heavy rotation). ``pool_size=None``
preserves the old hard floor for backward compatibility.
"""
from services.question_bank import _quant_figure_floor


def test_figure_floor_default_section_12():
    # No pool_size: preserves original hard floor (3 for 12-Q section).
    assert _quant_figure_floor(12) == 3


def test_figure_floor_large_pool_keeps_base():
    # Pool >= 8x base: no softening.
    assert _quant_figure_floor(12, pool_size=200) == 3


def test_figure_floor_medium_pool_scales_down():
    # pool_size // 8 = 2 -> floor softens to 2.
    assert _quant_figure_floor(12, pool_size=20) == 2


def test_figure_floor_tiny_pool_clamps_to_one():
    # pool_size // 8 = 0 -> clamped to 1 (never below 1).
    assert _quant_figure_floor(12, pool_size=5) == 1


def test_figure_floor_section_15_large_pool():
    # 15-Q section has base=4; pool 100 -> 100//8=12 -> min(4, 12) = 4.
    assert _quant_figure_floor(15, pool_size=100) == 4
