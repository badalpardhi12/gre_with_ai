"""
Headless tests for the reusable ETS section-transition screen
(screens/transition_screen.py).

Builds the screen on a hidden frame under a wx.App and verifies, per kind, that
configure() sets the right title, body text, chrome ribbon button set, and
section-bar timer visibility — and that the on_review / on_return / on_continue
callbacks fire through the chrome ribbon.

Whole file is skipped when wxPython isn't importable (headless CI).
"""
import pytest

pytest.importorskip("wx", reason="TransitionScreen requires wxPython")

import wx  # noqa: E402

from screens.transition_screen import TransitionScreen  # noqa: E402


@pytest.fixture(scope="module")
def wx_app():
    app = wx.App(False)
    yield app


@pytest.fixture
def screen(wx_app):
    frame = wx.Frame(None)
    frame.Hide()
    scr = TransitionScreen(frame)
    yield scr
    frame.Destroy()


def _ribbon_ids(chrome):
    return list(chrome._btns.keys())


def _body_text(screen):
    """Concatenate all body paragraph labels for substring assertions.

    Collapses the newlines wx.StaticText.Wrap() injects so substring checks
    compare the logical text, not the wrapped layout."""
    joined = " ".join(lbl.GetLabel() for lbl in screen._body_labels)
    return " ".join(joined.split())


def _fire(chrome, button_id):
    """Fire the chrome ribbon button by id the way a real click does."""
    chrome._fire(button_id)


# ── construction ──────────────────────────────────────────────────────


def test_construction_has_chrome(screen):
    assert screen.chrome is not None
    assert screen.kind is None


# ── end_of_section ────────────────────────────────────────────────────


def test_end_of_section_title_body_ribbon(screen):
    screen.configure("end_of_section", section_label="Section 2 of 6")
    assert screen.kind == "end_of_section"
    assert screen.title_label.GetLabel() == "End of Section"
    body = _body_text(screen)
    assert "reached the end of this section" in body
    assert "WILL NOT be able to return" in body
    assert "Select Review" in body
    assert "Select Return" in body
    assert "Select Continue" in body
    # Ribbon = Review, Return, Continue (in order).
    assert _ribbon_ids(screen.chrome) == ["review", "return", "continue"]
    # Timer visible on this timed kind.
    assert screen.chrome.timer.IsShown() is True
    assert screen.chrome.section_label.GetLabel() == "Section 2 of 6"


def test_end_of_section_callbacks_fire(screen):
    calls = []
    screen.configure(
        "end_of_section",
        section_label="Section 1 of 6",
        on_review=lambda: calls.append("review"),
        on_return=lambda: calls.append("return"),
        on_continue=lambda: calls.append("continue"),
    )
    _fire(screen.chrome, "review")
    _fire(screen.chrome, "return")
    _fire(screen.chrome, "continue")
    assert calls == ["review", "return", "continue"]


# ── section_finished ──────────────────────────────────────────────────


def test_section_finished_title_body_ribbon_no_timer(screen):
    screen.configure("section_finished", section_label="Section 3 of 6")
    assert screen.title_label.GetLabel() == "Section Finished"
    body = _body_text(screen)
    assert "finished this section" in body
    assert "Select Continue" in body
    # Ribbon = Continue only.
    assert _ribbon_ids(screen.chrome) == ["continue"]
    # Timer hidden on this kind.
    assert screen.chrome.timer.IsShown() is False
    assert screen.chrome.section_label.GetLabel() == "Section 3 of 6"


def test_section_finished_continue_fires(screen):
    calls = []
    screen.configure("section_finished", section_label="Section 3 of 6",
                     on_continue=lambda: calls.append("go"))
    _fire(screen.chrome, "continue")
    assert calls == ["go"]


# ── confirm_exit_awa ──────────────────────────────────────────────────


def test_confirm_exit_awa_title_body_ribbon(screen):
    screen.configure("confirm_exit_awa",
                     section_label="Section 1 of 6 | Question 1 of 1")
    assert screen.title_label.GetLabel() == \
        "Confirm Early Exit on Analytical Writing Section"
    body = _body_text(screen)
    assert "still have time remaining" in body
    assert "Select Return" in body
    assert "Select Continue" in body
    assert "WILL NOT be able to return" in body
    # Ribbon = Return, Continue.
    assert _ribbon_ids(screen.chrome) == ["return", "continue"]
    # Timer visible.
    assert screen.chrome.timer.IsShown() is True
    assert screen.chrome.section_label.GetLabel() == \
        "Section 1 of 6 | Question 1 of 1"


def test_confirm_exit_awa_callbacks(screen):
    calls = []
    screen.configure("confirm_exit_awa", section_label="Section 1 of 6",
                     on_return=lambda: calls.append("return"),
                     on_continue=lambda: calls.append("continue"))
    _fire(screen.chrome, "return")
    _fire(screen.chrome, "continue")
    assert calls == ["return", "continue"]


# ── reconfiguration ───────────────────────────────────────────────────


def test_reconfigure_switches_kind_and_restores_timer(screen):
    # Going to a no-timer kind hides the timer; going back shows it again.
    screen.configure("section_finished", section_label="Section 2 of 6")
    assert screen.chrome.timer.IsShown() is False
    screen.configure("end_of_section", section_label="Section 2 of 6")
    assert screen.chrome.timer.IsShown() is True
    assert _ribbon_ids(screen.chrome) == ["review", "return", "continue"]


def test_unknown_kind_raises(screen):
    with pytest.raises(ValueError):
        screen.configure("not_a_kind")
