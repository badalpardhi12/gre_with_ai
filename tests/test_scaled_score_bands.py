"""Scaled-score floor+ceiling band tests (balancing fix #5).

The legacy ``_build_score_table`` used ONE table for both measures and
gave each difficulty band only a CAP (easy ~155 / medium ~165 / hard 170)
with no FLOOR — so a test-taker routed to a HARD second section who then
under-performed could still drop toward 130, and a strong MEDIUM performer
was under-rewarded by the ~165 cap. Real GRE section-level forms set BOTH
a ceiling AND a floor (the "safety net"): only a hard form reaches 170 and
it also guarantees a minimum; an easy form caps well below 170.

These tests pin the reverse-engineered per-measure [floor, ceiling] bands
(APPROXIMATE — ETS does not publish raw→scaled tables). They lock:

  * the hard form has a real FLOOR (low estimate never sinks below it),
  * the easy/medium forms have real CAPS (high estimate never reaches 170),
  * only the hard form can reach 170,
  * scores are monotonic non-decreasing in raw-correct within a band,
  * Quant is curved slightly harder than Verbal (same raw → ≤ Verbal score),
  * the public ``estimate_scaled_score`` / ``compute_session_scores`` API and
    its documented fallbacks keep working.
"""
from __future__ import annotations

import pytest

from services.scoring import (
    ScoringEngine,
    SCALED_SCORE_BANDS,
    RAW_MAX,
)

MEASURES = ("verbal", "quant")
BANDS = ("easy", "medium", "hard")


def _est(raw, band, measure):
    return ScoringEngine.estimate_scaled_score(raw, band, measure=measure)


# ── bands table sanity ────────────────────────────────────────────────

def test_bands_table_shape():
    for measure in MEASURES:
        for band in BANDS:
            floor, ceiling = SCALED_SCORE_BANDS[measure][band]
            assert 130 <= floor <= ceiling <= 170


def test_only_hard_band_reaches_170():
    for measure in MEASURES:
        assert SCALED_SCORE_BANDS[measure]["hard"][1] == 170
        assert SCALED_SCORE_BANDS[measure]["easy"][1] < 170
        assert SCALED_SCORE_BANDS[measure]["medium"][1] < 170


def test_hard_band_has_safety_net_floor():
    """A hard form floor is meaningfully above 130 (the global minimum)."""
    for measure in MEASURES:
        assert SCALED_SCORE_BANDS[measure]["hard"][0] >= 145
        # easy form's floor is the global minimum (no safety net).
        assert SCALED_SCORE_BANDS[measure]["easy"][0] == 130


# ── floor / ceiling guarantees across all raw scores ───────────────────

@pytest.mark.parametrize("measure", MEASURES)
def test_hard_form_low_never_below_floor(measure):
    floor = SCALED_SCORE_BANDS[measure]["hard"][0]
    for raw in range(RAW_MAX + 1):
        low, high = _est(raw, "hard", measure)
        assert low >= floor, f"{measure} hard raw={raw}: low {low} < floor {floor}"
        assert high <= 170


@pytest.mark.parametrize("measure", MEASURES)
def test_easy_form_high_never_above_cap(measure):
    cap = SCALED_SCORE_BANDS[measure]["easy"][1]
    for raw in range(RAW_MAX + 1):
        low, high = _est(raw, "easy", measure)
        assert high <= cap, f"{measure} easy raw={raw}: high {high} > cap {cap}"
        assert low >= 130


@pytest.mark.parametrize("measure", MEASURES)
def test_medium_form_high_never_above_cap(measure):
    cap = SCALED_SCORE_BANDS[measure]["medium"][1]
    for raw in range(RAW_MAX + 1):
        _low, high = _est(raw, "medium", measure)
        assert high <= cap


# ── monotonicity ───────────────────────────────────────────────────────

@pytest.mark.parametrize("measure", MEASURES)
@pytest.mark.parametrize("band", BANDS)
def test_monotonic_non_decreasing_in_raw(measure, band):
    prev_low, prev_high = -1, -1
    for raw in range(RAW_MAX + 1):
        low, high = _est(raw, band, measure)
        assert low >= prev_low, f"{measure}/{band} low dipped at raw={raw}"
        assert high >= prev_high, f"{measure}/{band} high dipped at raw={raw}"
        prev_low, prev_high = low, high


@pytest.mark.parametrize("measure", MEASURES)
def test_hard_reaches_170_at_top_raw(measure):
    low, high = _est(RAW_MAX, "hard", measure)
    assert high == 170


# ── Quant curved slightly harder than Verbal ───────────────────────────

def test_quant_curved_harder_than_verbal():
    """For the same raw + band, Quant maps to a ≤ Verbal scaled score
    (real GRE curves Quant marginally harder). Checked on the midpoint
    raw across all bands using the high estimate."""
    mid = RAW_MAX // 2
    for band in BANDS:
        _vl, v_high = _est(mid, band, "verbal")
        _ql, q_high = _est(mid, band, "quant")
        assert q_high <= v_high, (
            f"band={band} raw={mid}: quant {q_high} should be <= verbal {v_high}")


# ── higher routed form yields higher ceiling at top performance ────────

@pytest.mark.parametrize("measure", MEASURES)
def test_harder_form_has_higher_ceiling(measure):
    _e_l, e_high = _est(RAW_MAX, "easy", measure)
    _m_l, m_high = _est(RAW_MAX, "medium", measure)
    _h_l, h_high = _est(RAW_MAX, "hard", measure)
    assert e_high < m_high < h_high


# ── compute_session_scores wires the right per-measure band ────────────

def test_compute_session_scores_uses_per_measure_bands():
    scores = ScoringEngine.compute_session_scores(
        verbal_raw=27, verbal_band="hard",
        quant_raw=27, quant_band="easy",
    )
    # Verbal hard → reaches 170; Quant easy → capped at its easy ceiling.
    assert scores["verbal_estimated_high"] == 170
    assert scores["quant_estimated_high"] == SCALED_SCORE_BANDS["quant"]["easy"][1]
    assert scores["verbal_raw"] == 27
    assert scores["quant_raw"] == 27


def test_compute_session_scores_hard_quant_floor():
    """A hard-routed Quant taker who scored low total still floors at the
    hard band's safety net, not 130."""
    scores = ScoringEngine.compute_session_scores(
        verbal_raw=0, verbal_band="medium",
        quant_raw=2, quant_band="hard",
    )
    assert scores["quant_estimated_low"] >= SCALED_SCORE_BANDS["quant"]["hard"][0]


# ── legacy API fallbacks still hold (mirrors tests/test_scoring.py) ─────

def test_legacy_fallbacks_preserved():
    assert ScoringEngine.estimate_scaled_score("not a number") == (130, 135)
    assert ScoringEngine.estimate_scaled_score(None) == (130, 135)
    assert (ScoringEngine.estimate_scaled_score(-5)
            == ScoringEngine.estimate_scaled_score(0))
    assert (ScoringEngine.estimate_scaled_score(999)
            == ScoringEngine.estimate_scaled_score(27))
    # Unknown band → medium, holding measure constant.
    assert (ScoringEngine.estimate_scaled_score(20, "wat")
            == ScoringEngine.estimate_scaled_score(20, "medium"))
