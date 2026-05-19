"""Phase 7.3 — figure-floor module-level constants.

The floor values for figure-bearing Quant items per section are now
exposed as module-level constants so a Phase 6 follow-up can flip
them in a one-line edit once the geometry-MCQ image pool gap closes.

These tests pin the contract:
  * The constants exist, are introspectable, and are integers.
  * ``_quant_figure_floor(count)`` reads from those constants.
  * A 12-Q section gets ≥3 figures (current pre-Phase-6 floor).
  * A 15-Q section gets ≥4 figures (current pre-Phase-6 floor).

Phase 6 will raise QUANT_FIGURE_FLOOR_SHORT to 5 and QUANT_FIGURE_FLOOR_LONG
to 7 once the synthesis pipeline closes the figure gap; when that lands,
the >=3 / >=4 lower bounds in this file are still satisfied (5/7 ≥ 3/4)
so these regression tests stay green.
"""
from services import question_bank
from services.question_bank import (
    QUANT_FIGURE_FLOOR_SHORT,
    QUANT_FIGURE_FLOOR_LONG,
    QUANT_FIGURE_SECTION_BOUNDARY,
    _quant_figure_floor,
)


def test_constants_exist_and_are_integers():
    """Constants are exposed at module scope and are positive integers."""
    assert hasattr(question_bank, "QUANT_FIGURE_FLOOR_SHORT")
    assert hasattr(question_bank, "QUANT_FIGURE_FLOOR_LONG")
    assert hasattr(question_bank, "QUANT_FIGURE_SECTION_BOUNDARY")
    assert isinstance(QUANT_FIGURE_FLOOR_SHORT, int)
    assert isinstance(QUANT_FIGURE_FLOOR_LONG, int)
    assert isinstance(QUANT_FIGURE_SECTION_BOUNDARY, int)
    assert QUANT_FIGURE_FLOOR_SHORT >= 1
    assert QUANT_FIGURE_FLOOR_LONG >= 1


def test_section_12_floor_at_least_3():
    """A 12-Q Quant section gets ≥3 figures pre-Phase-6 (currently 3)."""
    assert _quant_figure_floor(12) >= 3
    # Mirrors the SHORT constant exactly when no pool_size hint.
    assert _quant_figure_floor(12) == QUANT_FIGURE_FLOOR_SHORT


def test_section_15_floor_at_least_4():
    """A 15-Q Quant section gets ≥4 figures pre-Phase-6 (currently 4)."""
    assert _quant_figure_floor(15) >= 4
    # Mirrors the LONG constant exactly when no pool_size hint.
    assert _quant_figure_floor(15) == QUANT_FIGURE_FLOOR_LONG


def test_section_boundary_is_inclusive_at_short_side():
    """count == QUANT_FIGURE_SECTION_BOUNDARY (12) uses the SHORT floor."""
    assert _quant_figure_floor(QUANT_FIGURE_SECTION_BOUNDARY) == QUANT_FIGURE_FLOOR_SHORT
    assert _quant_figure_floor(QUANT_FIGURE_SECTION_BOUNDARY + 1) == QUANT_FIGURE_FLOOR_LONG


def test_floor_softens_with_thin_pool():
    """When pool_size is small, the floor softens so a 40-item pool
    doesn't get 3 of its items jammed into every section.
    """
    # pool_size 5 -> 5//8=0 -> clamp to 1.
    assert _quant_figure_floor(12, pool_size=5) == 1
    # pool_size 200 -> 200//8=25 -> min(SHORT=3, 25) = SHORT.
    assert _quant_figure_floor(12, pool_size=200) == QUANT_FIGURE_FLOOR_SHORT
