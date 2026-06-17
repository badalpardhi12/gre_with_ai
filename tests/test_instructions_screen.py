"""
Tests for the ETS-skinned InstructionsScreen (section-intro page).

Verifies the preserved public API used by main_frame.py:
``set_section(section_type, section_state=None)``, ``set_on_begin``,
``set_on_cancel``, and the ``display_label`` override path for mixed
Quick Drills — plus that the Continue / Back-to-Dashboard ExamButtons
fire their callbacks when they emit wx.EVT_BUTTON.

Whole file is skipped when wxPython isn't importable (headless CI).
"""
import pytest

pytest.importorskip("wx", reason="InstructionsScreen requires wxPython")

import wx  # noqa: E402

from models.exam_session import SectionType  # noqa: E402
from screens.instructions_screen import (  # noqa: E402
    InstructionsScreen,
    SECTION_INSTRUCTIONS,
    FIGURES_CAVEAT,
)


@pytest.fixture(scope="module")
def wx_app():
    """One wx.App per module — creating many leaks resources on macOS."""
    app = wx.App(False)
    yield app
    # Don't MainLoop; just let the app go out of scope.


@pytest.fixture
def screen(wx_app):
    """Build an InstructionsScreen on a hidden frame."""
    frame = wx.Frame(None)
    scr = InstructionsScreen(frame)
    yield scr
    frame.Destroy()


class _FakeDrillState:
    """Stub of the mixed-drill SectionState slice set_section reads."""

    def __init__(self, display_label, question_ids, time_limit):
        self.display_label = display_label
        self.question_ids = list(question_ids)
        self.time_limit = time_limit


def _click(button):
    """Emit wx.EVT_BUTTON from an ExamButton, the way it does on a real click."""
    evt = wx.CommandEvent(wx.wxEVT_BUTTON, button.GetId())
    evt.SetEventObject(button)
    button.ProcessEvent(evt)


# ── set_section: normal sections ──────────────────────────────────────


def test_normal_section_sets_title_and_body(screen):
    """A normal section pulls its canonical title/body from the dict."""
    screen.set_section(SectionType.VERBAL_S1)
    info = SECTION_INSTRUCTIONS[SectionType.VERBAL_S1]
    assert screen.title_label.GetLabel() == info["title"]
    body = screen.body_text.GetLabel()
    # Count / time / navigation rules from the canonical body survive.
    assert "12 questions" in body
    assert "18 minutes" in body
    assert "cannot return to this section" in body
    # Verbal has no figures caveat.
    assert FIGURES_CAVEAT not in body


def test_quant_section_appends_figures_caveat(screen):
    """Quant sections must show the figures-not-to-scale caveat and keep
    the calculator note."""
    screen.set_section(SectionType.QUANT_S1)
    body = screen.body_text.GetLabel()
    assert screen.title_label.GetLabel() == \
        SECTION_INSTRUCTIONS[SectionType.QUANT_S1]["title"]
    assert "on-screen calculator" in body.lower()
    assert FIGURES_CAVEAT in body


def test_quant_section_does_not_double_append_caveat(screen):
    """Re-configuring the same Quant section must not duplicate the caveat."""
    screen.set_section(SectionType.QUANT_S2)
    screen.set_section(SectionType.QUANT_S2)
    body = screen.body_text.GetLabel()
    assert body.count(FIGURES_CAVEAT) == 1


def test_switching_sections_updates_title_and_body(screen):
    """Title/body update each time set_section is called."""
    screen.set_section(SectionType.AWA)
    assert "Analytical Writing" in screen.title_label.GetLabel()
    assert "30 minutes" in screen.body_text.GetLabel()

    screen.set_section(SectionType.QUANT_S1)
    assert "Quantitative Reasoning" in screen.title_label.GetLabel()


# ── set_section: mixed-drill display_label override ────────────────────


def test_mixed_drill_override_uses_display_label(screen):
    """A section_state with display_label overrides the canonical title
    and substitutes the drill body (count/time derived from the state)."""
    state = _FakeDrillState(
        display_label="Quick Drill — Mixed (5 verbal, 5 quant)",
        question_ids=list(range(10)),
        time_limit=900,  # 15 minutes
    )
    # Pass a verbal section type to prove the override wins over the dict.
    screen.set_section(SectionType.VERBAL_S1, state)
    assert screen.title_label.GetLabel() == \
        "Quick Drill — Mixed (5 verbal, 5 quant)"
    body = screen.body_text.GetLabel()
    assert "10 questions" in body
    assert "~15 minutes" in body
    assert "mixed across Verbal Reasoning and Quantitative" in body
    # The canonical Verbal title must NOT leak through.
    assert "Section 1" not in screen.title_label.GetLabel()


def test_mixed_drill_override_does_not_force_figures_caveat(screen):
    """Even with a Quant section type, the drill override path produces the
    drill body (no spurious figures caveat appended)."""
    state = _FakeDrillState(
        display_label="Due for Review (7 items)",
        question_ids=list(range(7)),
        time_limit=630,
    )
    screen.set_section(SectionType.QUANT_S1, state)
    assert screen.title_label.GetLabel() == "Due for Review (7 items)"
    assert FIGURES_CAVEAT not in screen.body_text.GetLabel()


# ── callbacks fire on EVT_BUTTON ──────────────────────────────────────


def test_begin_callback_fires_on_continue_button(screen):
    """set_on_begin's callback runs when the Continue button emits EVT_BUTTON."""
    calls = []
    screen.set_on_begin(lambda: calls.append("begin"))
    _click(screen.begin_btn)
    assert calls == ["begin"]


def test_cancel_callback_fires_on_back_button(screen):
    """set_on_cancel's callback runs when the Back button emits EVT_BUTTON."""
    calls = []
    screen.set_on_cancel(lambda: calls.append("cancel"))
    _click(screen.cancel_btn)
    assert calls == ["cancel"]


def test_no_callback_no_crash(screen):
    """Clicking with no callbacks registered is a harmless no-op."""
    _click(screen.begin_btn)
    _click(screen.cancel_btn)


# ── ExamChrome re-skin ────────────────────────────────────────────────


def test_mounts_examchrome_with_help_continue_ribbon(screen):
    """The section-intro page mounts ExamChrome with the [help, continue]
    ribbon (Continue is the live action)."""
    assert screen.chrome is not None
    assert set(screen.chrome._btns.keys()) == {"help", "continue"}
    # begin_btn IS the ribbon Continue button (so callers/tests can click it).
    assert screen.begin_btn is screen.chrome._btns["continue"]


def test_section_bar_reflects_measure(screen):
    """The pink section bar names the measure about to start."""
    screen.set_section(SectionType.QUANT_S1)
    assert "Quantitative Reasoning" == screen.chrome.section_label.GetLabel()
    screen.set_section(SectionType.VERBAL_S1)
    assert "Verbal Reasoning" == screen.chrome.section_label.GetLabel()


def test_subtitle_shows_count_and_time(screen):
    """The serif subheading shows the section's question count and minutes."""
    screen.set_section(SectionType.QUANT_S1)
    sub = screen.subtitle_label.GetLabel()
    assert "Questions" in sub
    assert "Minutes" in sub
