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


def test_live_selection_payload_tracks_clicks(question_screen):
    """The submission payload tracks the checkbox state 1:1.

    (The ETS UI has no 'Your selections' readout — fidelity is now
    guaranteed by reading the payload straight from the same
    ``_answer_controls`` the scorer sees.)"""
    q = _make_7_option_mcq_multi()
    screen = question_screen
    screen._current_q = q
    screen._section_state = screen._fake_ss_cls(q["id"])
    screen._build_answer_controls(q)

    assert screen._get_current_response() == {}

    _click_checkbox(screen, "A")
    assert screen._get_current_response() == {"selected": ["A"]}

    _click_checkbox(screen, "C")
    assert screen._get_current_response() == {"selected": ["A", "C"]}

    _click_checkbox(screen, "G")
    assert screen._get_current_response() == {"selected": ["A", "C", "G"]}

    # Uncheck C — payload drops it and stays in rendered order.
    _click_checkbox(screen, "C")
    assert screen._get_current_response() == {"selected": ["A", "G"]}

    # Uncheck everything — back to empty.
    _click_checkbox(screen, "A")
    _click_checkbox(screen, "G")
    assert screen._get_current_response() == {}


def test_payload_order_matches_rendered_order(question_screen):
    """Whatever is checked is submitted in rendered (A..G) order — the
    UX-safeguard guarantee that survives the ETS re-skin."""
    q = _make_7_option_mcq_multi()
    screen = question_screen
    screen._current_q = q
    screen._section_state = screen._fake_ss_cls(q["id"])
    screen._build_answer_controls(q)

    _click_checkbox(screen, "G")
    _click_checkbox(screen, "A")
    _click_checkbox(screen, "C")

    # Even though clicked G,A,C the payload is in rendered order A,C,G.
    assert screen._get_current_response() == {"selected": ["A", "C", "G"]}


def test_single_select_uses_radios(question_screen):
    """Radio (single-select) questions render no checkboxes."""
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
    assert all(ct != "check" for ct, _l, _c in screen._answer_controls)


def test_multi_select_restored_on_navigation(question_screen):
    """Navigating back to a partly-answered mcq_multi restores the checks
    so the payload reflects them without a click."""
    q = _make_7_option_mcq_multi()
    screen = question_screen
    screen._current_q = q
    screen._section_state = screen._fake_ss_cls(q["id"])
    screen._build_answer_controls(q)

    screen._restore_response({"selected": ["A", "C", "G"]})
    assert screen._get_current_response() == {"selected": ["A", "C", "G"]}


def test_se_subtype_multi_select(question_screen):
    """Sentence-equivalence is multi-select (pick exactly two)."""
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
    assert sum(1 for ct, _l, _c in screen._answer_controls if ct == "check") == 6

    _click_checkbox(screen, "B")
    _click_checkbox(screen, "D")
    assert screen._get_current_response() == {"selected": ["B", "D"]}
