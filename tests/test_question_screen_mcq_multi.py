"""
Tests for mcq_multi selection handling in QuestionScreen.

These cover the regression scenario reported on Q4472 (7-option
Indicate-All-That-Apply): the user clicked A, C, G but the submitted
payload was A, C, D, G. We exercise the full click-to-submission
round-trip on a programmatically-built 7-option mcq_multi question to
prove there's no off-by-one or closure-capture bug between the rendered
checkbox rows and ``_get_current_response()``.

Also verifies the live "Your selections: …" indicator the screen now
shows below the options for mcq_multi / rc_multi / se — the label text
in that indicator is pulled from the SAME ``_answer_controls`` tuple
that ``_get_current_response`` uses at submit, so it's the ground truth
the user can verify against before moving on.

Whole file is skipped when wxPython isn't importable (headless CI).
"""
import pytest

pytest.importorskip("wx", reason="QuestionScreen requires wxPython")

import wx  # noqa: E402


@pytest.fixture(scope="module")
def wx_app():
    """One wx.App per module — creating many leaks resources on macOS."""
    app = wx.App(False)
    yield app
    # Don't MainLoop; just let the app go out of scope.


@pytest.fixture
def question_screen(wx_app):
    """Build a QuestionScreen attached to a hidden frame.

    We don't call ``configure()`` (which would stand up a whole
    ExamSession); instead we stub the tiny slice of SectionState that
    ``_on_answer_change`` touches so the screen can be driven directly.
    """
    from screens.question_screen import QuestionScreen

    frame = wx.Frame(None)
    screen = QuestionScreen(frame)

    # Minimal SectionState stub so _on_answer_change doesn't no-op
    # early (qid lookup happens through it).
    class _FakeSectionState:
        def __init__(self, qid):
            self.current_question_id = qid
            self.question_ids = [qid]
            self.current_index = 0
            self.total_questions = 1
            self.responses = {}
            self.marked = set()

        def set_response(self, qid, payload):
            self.responses[qid] = payload

        def get_response(self, qid):
            return self.responses.get(qid)

    screen._fake_ss_cls = _FakeSectionState
    yield screen
    frame.Destroy()


def _make_7_option_mcq_multi():
    """Build the Q4472-shaped question: 7 options A–G, A+C+G correct."""
    labels = ["A", "B", "C", "D", "E", "F", "G"]
    options = [
        {
            "label": lbl,
            "text": f"Option text for {lbl}",
            "is_correct": lbl in ("A", "C", "G"),
        }
        for lbl in labels
    ]
    return {
        "id": 4472,
        "subtype": "mcq_multi",
        "prompt": "Select all that apply.",
        "options": options,
    }


def _click_checkbox(screen, label):
    """Programmatically toggle the checkbox whose label-tuple is ``label``.

    Mirrors the click path: flip GetValue(), then post the
    EVT_CHECKBOX so the screen's _on_answer_change handler runs — the
    same sequence used by the real UI's `_toggle_from_text` helper.
    """
    for ct, lbl, ctrl in screen._answer_controls:
        if ct == "check" and lbl == label:
            ctrl.SetValue(not ctrl.GetValue())
            evt = wx.PyCommandEvent(wx.EVT_CHECKBOX.typeId, ctrl.GetId())
            evt.SetEventObject(ctrl)
            ctrl.ProcessEvent(evt)
            return
    raise AssertionError(f"No checkbox with label {label!r}")


# ── Tests ─────────────────────────────────────────────────────────────


def test_mcq_multi_seven_options_round_trip(question_screen):
    """Clicking A, C, G on a 7-option mcq_multi must submit exactly {A,C,G}.

    This is the exact scenario reported on Q4472 — user claimed they
    clicked A, C, G but the payload came through as A, C, D, G. If a
    click-to-control binding bug ever resurfaces (closure capture, row
    offset, widget-id collision), this test fails first.
    """
    q = _make_7_option_mcq_multi()
    screen = question_screen
    screen._current_q = q
    screen._section_state = screen._fake_ss_cls(q["id"])
    screen._build_answer_controls(q)

    # Sanity: 7 checkboxes were rendered in the documented A..G order
    labels = [lbl for ct, lbl, _ in screen._answer_controls if ct == "check"]
    assert labels == ["A", "B", "C", "D", "E", "F", "G"]

    _click_checkbox(screen, "A")
    _click_checkbox(screen, "C")
    _click_checkbox(screen, "G")

    resp = screen._get_current_response()
    assert resp == {"selected": ["A", "C", "G"]}, (
        f"Expected only A,C,G in payload; got {resp!r}. A regression in "
        "the click→control binding (e.g. closure capture, off-by-one, "
        "shared id) would surface here."
    )


def test_mcq_multi_no_phantom_d_selection(question_screen):
    """Negative regression: clicking G must NOT also toggle D.

    Explicitly guards the Q4472 failure mode — with the reported bug
    present, clicking G would cause the adjacent D checkbox to also
    appear in the submitted set.
    """
    q = _make_7_option_mcq_multi()
    screen = question_screen
    screen._current_q = q
    screen._section_state = screen._fake_ss_cls(q["id"])
    screen._build_answer_controls(q)

    _click_checkbox(screen, "G")
    resp = screen._get_current_response()
    assert resp == {"selected": ["G"]}
    # D in particular must be untouched — it's the letter reported as
    # spuriously added in the bug report.
    d_ctrl = next(
        ctrl for ct, lbl, ctrl in screen._answer_controls
        if ct == "check" and lbl == "D"
    )
    assert d_ctrl.GetValue() is False


def test_mcq_multi_uncheck_removes_from_payload(question_screen):
    """Clicking G twice should leave G out of the submission."""
    q = _make_7_option_mcq_multi()
    screen = question_screen
    screen._current_q = q
    screen._section_state = screen._fake_ss_cls(q["id"])
    screen._build_answer_controls(q)

    _click_checkbox(screen, "A")
    _click_checkbox(screen, "G")
    _click_checkbox(screen, "G")  # uncheck

    assert screen._get_current_response() == {"selected": ["A"]}


def test_live_selection_indicator_updates(question_screen):
    """The 'Your selections: …' readout tracks the checkbox state 1:1."""
    q = _make_7_option_mcq_multi()
    screen = question_screen
    screen._current_q = q
    screen._section_state = screen._fake_ss_cls(q["id"])
    screen._build_answer_controls(q)

    ind = screen._selection_indicator
    assert ind is not None, "mcq_multi must render a selection indicator"
    assert ind.GetLabel() == "Your selections: (none)"

    _click_checkbox(screen, "A")
    assert ind.GetLabel() == "Your selections: A"

    _click_checkbox(screen, "C")
    assert ind.GetLabel() == "Your selections: A, C"

    _click_checkbox(screen, "G")
    assert ind.GetLabel() == "Your selections: A, C, G"

    # Uncheck C — indicator should drop it and stay in rendered order.
    _click_checkbox(screen, "C")
    assert ind.GetLabel() == "Your selections: A, G"

    # Uncheck everything — back to the (none) placeholder.
    _click_checkbox(screen, "A")
    _click_checkbox(screen, "G")
    assert ind.GetLabel() == "Your selections: (none)"


def test_indicator_matches_submission_payload(question_screen):
    """The labels in the live readout must match the submission exactly.

    This is the UX-safeguard guarantee: whatever the user sees in the
    readout is literally what the scorer will receive. If a future
    refactor ever splits the two code paths, this test fails.
    """
    q = _make_7_option_mcq_multi()
    screen = question_screen
    screen._current_q = q
    screen._section_state = screen._fake_ss_cls(q["id"])
    screen._build_answer_controls(q)

    _click_checkbox(screen, "A")
    _click_checkbox(screen, "C")
    _click_checkbox(screen, "G")

    # Parse labels out of the indicator and compare to the payload.
    readout = screen._selection_indicator.GetLabel()
    assert readout.startswith("Your selections: ")
    indicator_labels = [
        s.strip() for s in readout[len("Your selections: "):].split(",")
    ]

    payload = screen._get_current_response()
    assert payload["selected"] == indicator_labels


def test_indicator_not_rendered_for_single_select(question_screen):
    """Radio (single-select) questions don't need the safeguard."""
    q = {
        "id": 1,
        "subtype": "mcq_single",
        "prompt": "Pick one.",
        "options": [
            {"label": "A", "text": "Alpha", "is_correct": True},
            {"label": "B", "text": "Beta", "is_correct": False},
            {"label": "C", "text": "Gamma", "is_correct": False},
        ],
    }
    screen = question_screen
    screen._current_q = q
    screen._section_state = screen._fake_ss_cls(q["id"])
    screen._build_answer_controls(q)
    assert screen._selection_indicator is None


def test_indicator_restored_on_navigation(question_screen):
    """Simulate navigating back to a partly-answered mcq_multi: the
    indicator must reflect the restored checks without needing a click.
    """
    q = _make_7_option_mcq_multi()
    screen = question_screen
    screen._current_q = q
    screen._section_state = screen._fake_ss_cls(q["id"])
    screen._build_answer_controls(q)

    # Emulate what `_load_question` does after `_build_answer_controls`
    screen._restore_response({"selected": ["A", "C", "G"]})
    screen._update_selection_indicator()

    assert screen._selection_indicator.GetLabel() == "Your selections: A, C, G"


def test_se_subtype_also_gets_indicator(question_screen):
    """Sentence-equivalence is also a multi-select and benefits from
    the same safeguard (user must pick exactly two)."""
    q = {
        "id": 2,
        "subtype": "se",
        "prompt": "Pick two.",
        "options": [
            {"label": lbl, "text": f"t{lbl}", "is_correct": lbl in ("B", "D")}
            for lbl in ["A", "B", "C", "D", "E", "F"]
        ],
    }
    screen = question_screen
    screen._current_q = q
    screen._section_state = screen._fake_ss_cls(q["id"])
    screen._build_answer_controls(q)
    assert screen._selection_indicator is not None
    assert screen._selection_indicator.GetLabel() == "Your selections: (none)"

    _click_checkbox(screen, "B")
    _click_checkbox(screen, "D")
    assert screen._selection_indicator.GetLabel() == "Your selections: B, D"
