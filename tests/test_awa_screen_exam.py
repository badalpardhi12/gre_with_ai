"""
Headless tests for the ETS "Test Preview Tool" AWA writing screen
(screens/awa_screen.py).

Builds the screen on a hidden frame under a wx.App and verifies:
- construction mounts ExamChrome with the writing-page ribbon [help, next];
- load_prompt populates the Issue statement, the task instructions, and clears
  the editor + word count;
- a prompt with no instructions falls back to the canonical ETS Issue task;
- the editor toolbar exposes Cut / Paste / Undo / Redo buttons, with Undo/Redo
  greyed when there is nothing to undo/redo;
- the preserved public API (get_essay / get_word_count / start_timer / the
  set_on_* callbacks / self.timer / self.editor) is intact;
- Next (chrome ribbon) submits, firing set_on_submit with (essay, word_count).

Whole file is skipped when wxPython isn't importable (headless CI).
"""
import pytest

pytest.importorskip("wx", reason="AWAScreen requires wxPython")

import wx  # noqa: E402

from screens.awa_screen import AWAScreen, DEFAULT_ISSUE_INSTRUCTIONS  # noqa: E402


def _flat(text):
    """Collapse the newlines wx.StaticText.Wrap() injects so substring/equality
    checks compare the logical text, not the wrapped layout."""
    return " ".join(text.split())


@pytest.fixture(scope="module")
def wx_app():
    app = wx.App(False)
    yield app


@pytest.fixture
def screen(wx_app):
    frame = wx.Frame(None)
    frame.Hide()
    scr = AWAScreen(frame)
    yield scr
    frame.Destroy()


def _ribbon_ids(chrome):
    return set(chrome._btns.keys())


# ── construction / chrome ─────────────────────────────────────────────


def test_construction_mounts_chrome_with_writing_ribbon(screen):
    assert screen.chrome is not None
    # The writing page shows only Help + Next (no Exit/Calc/Mark/Review).
    assert _ribbon_ids(screen.chrome) == {"help", "next"}
    # The chrome owns the timer and the screen exposes it as self.timer.
    assert screen.timer is not None
    assert screen.timer is screen.chrome.timer


def test_editor_and_toolbar_exist(screen):
    # Editor is a real multiline TextCtrl.
    assert isinstance(screen.editor, wx.TextCtrl)
    # Cut / Paste / Undo / Redo toolbar buttons all present.
    for attr in ("cut_btn", "paste_btn", "undo_btn", "redo_btn"):
        assert hasattr(screen, attr), "missing toolbar button: " + attr


# ── load_prompt ───────────────────────────────────────────────────────


def test_load_prompt_populates_statement_and_instructions(screen):
    screen.load_prompt({
        "prompt_text": "Governments should focus on solving immediate problems.",
        "instructions": "Discuss the extent to which you agree or disagree.",
    })
    assert "immediate problems" in _flat(screen.statement_text.GetLabel())
    assert "agree or disagree" in _flat(screen.instructions_text.GetLabel())
    # Editor starts empty with a zero word count.
    assert screen.get_essay() == ""
    assert screen.word_count_label.GetLabel() == "Words: 0"


def test_load_prompt_without_instructions_uses_canonical_task(screen):
    screen.load_prompt({"prompt_text": "Some issue statement.", "instructions": ""})
    assert _flat(screen.instructions_text.GetLabel()) == DEFAULT_ISSUE_INSTRUCTIONS


def test_load_prompt_clears_previous_essay(screen):
    screen.load_prompt({"prompt_text": "A", "instructions": "B"})
    screen.editor.SetValue("a draft from before")
    screen.load_prompt({"prompt_text": "C", "instructions": "D"})
    assert screen.get_essay() == ""


# ── undo / redo greying ───────────────────────────────────────────────


def test_undo_redo_greyed_when_nothing_to_do(screen):
    screen.load_prompt({"prompt_text": "x", "instructions": "y"})
    # Fresh editor: nothing to undo or redo.
    assert screen.undo_btn.IsEnabled() is False
    assert screen.redo_btn.IsEnabled() is False


# ── word count ────────────────────────────────────────────────────────


def test_word_count_tracks_editor(screen):
    screen.load_prompt({"prompt_text": "x", "instructions": "y"})
    screen.editor.SetValue("one two three four five")
    assert screen.get_word_count() == 5


# ── timer / callbacks preserved ───────────────────────────────────────


def test_start_timer_runs_the_chrome_timer(screen):
    screen.start_timer()
    assert screen.timer._running is True
    screen.timer.stop()


def test_set_on_time_expire_wires_chrome_timer(screen):
    fired = []
    screen.set_on_time_expire(lambda: fired.append(True))
    # The timer's expire callback is what start()'s countdown fires.
    screen.timer._on_expire()
    assert fired == [True]


def test_next_submits_essay_with_word_count(screen, monkeypatch):
    screen.load_prompt({"prompt_text": "x", "instructions": "y"})
    screen.editor.SetValue("one two three four five six seven eight nine ten eleven")
    captured = []
    screen.set_on_submit(lambda essay, wc: captured.append((essay, wc)))
    # Long enough to skip the short-essay confirm dialog.
    screen._on_submit_click()
    assert len(captured) == 1
    essay, wc = captured[0]
    assert "eleven" in essay
    assert wc == 11


def test_short_essay_submit_confirm_cancel_aborts(screen, monkeypatch):
    screen.load_prompt({"prompt_text": "x", "instructions": "y"})
    screen.editor.SetValue("too short")
    captured = []
    screen.set_on_submit(lambda essay, wc: captured.append((essay, wc)))

    class _FakeDlg:
        def __init__(self, *a, **k):
            pass

        def ShowModal(self):
            return wx.ID_NO

        def Destroy(self):
            pass

    monkeypatch.setattr(wx, "MessageDialog", _FakeDlg)
    screen._on_submit_click()
    assert captured == []  # user declined the short-essay confirm


def test_exit_callback_setter_preserved(screen):
    # set_on_exit exists and stores the callback (wired by main_frame).
    cb = lambda: None
    screen.set_on_exit(cb)
    assert screen._on_exit is cb
