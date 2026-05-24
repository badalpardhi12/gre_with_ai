"""Tests for the user-controlled font-size zoom multiplier."""
import pytest

from config import (
    USER_PREF_DEFAULTS, FONT_SIZE_MIN, FONT_SIZE_MAX, FONT_SIZE_STEP,
    clamp_font_multiplier, load_user_prefs, save_user_pref,
)
import widgets.ui_scale as ui_scale


@pytest.fixture(autouse=True)
def _reset_zoom():
    """Reset zoom to 1.0 before/after every test so test order doesn't matter."""
    save_user_pref("font_size_multiplier", 1.0)
    ui_scale.invalidate_user_zoom_cache()
    yield
    save_user_pref("font_size_multiplier", 1.0)
    ui_scale.invalidate_user_zoom_cache()


def test_default_multiplier_is_one():
    assert USER_PREF_DEFAULTS["font_size_multiplier"] == 1.0


def test_clamp_below_min_returns_min():
    assert clamp_font_multiplier(0.1) == FONT_SIZE_MIN
    assert clamp_font_multiplier(-1.0) == FONT_SIZE_MIN


def test_clamp_above_max_returns_max():
    assert clamp_font_multiplier(5.0) == FONT_SIZE_MAX
    assert clamp_font_multiplier(2.5) == FONT_SIZE_MAX


def test_clamp_in_range_passes_through():
    assert clamp_font_multiplier(1.0) == 1.0
    assert clamp_font_multiplier(1.3) == 1.3


def test_clamp_invalid_input_returns_one():
    assert clamp_font_multiplier(None) == 1.0
    assert clamp_font_multiplier("not-a-number") == 1.0
    assert clamp_font_multiplier(float("nan")) == 1.0


def test_save_and_load_roundtrip():
    save_user_pref("font_size_multiplier", 1.4)
    assert load_user_prefs()["font_size_multiplier"] == pytest.approx(1.4)


def test_save_clamps_out_of_range():
    """Out-of-range save should be clamped, not rejected."""
    save_user_pref("font_size_multiplier", 99.0)
    assert load_user_prefs()["font_size_multiplier"] == FONT_SIZE_MAX


def test_scale_picks_up_user_zoom_after_invalidation():
    base = ui_scale.scale()
    save_user_pref("font_size_multiplier", 1.5)
    ui_scale.invalidate_user_zoom_cache()
    assert ui_scale.scale() == pytest.approx(base * 1.5)


def test_html_font_pt_responds_to_zoom():
    base = ui_scale.get_dashboard_html_font_pt()
    save_user_pref("font_size_multiplier", 1.5)
    ui_scale.invalidate_user_zoom_cache()
    bigger = ui_scale.get_dashboard_html_font_pt()
    assert bigger > base
    # The integer rounding means it isn't *exactly* 1.5x, but it should be
    # at least 30% larger.
    assert bigger >= int(round(base * 1.3))


def test_step_is_reasonable():
    """The View-menu zoom-in/out increments must be > 0 and small enough
    to give users 5+ steps inside the allowed range."""
    span = FONT_SIZE_MAX - FONT_SIZE_MIN
    assert span / FONT_SIZE_STEP >= 5
    assert FONT_SIZE_STEP > 0


def test_unknown_pref_key_rejected():
    """save_user_pref guards against typos so a misspelled key doesn't
    silently disappear into llm_config.json."""
    with pytest.raises(KeyError):
        save_user_pref("font_zise_multiplier", 1.2)
