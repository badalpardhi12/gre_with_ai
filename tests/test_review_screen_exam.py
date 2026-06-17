"""
Headless tests for the re-skinned ETS "Review Your Answers" screen
(screens/review_screen.py, spec §6).

Builds the screen on a hidden frame under a wx.App, loads a mix of
answered/not-answered/marked items, and verifies:
- one table row per item, with the right Status strings;
- columns are Question Number | Status | Marked, with NO correctness column;
- row activation fires set_on_goto with the 0-based index;
- the numeric "Go to Question N" jump fires set_on_goto with N-1;
- set_on_end_section fires when the Submit Section confirm dialog is accepted.
"""
import pytest

wx = pytest.importorskip("wx")

from screens.review_screen import ReviewScreen  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return wx.App(False)


@pytest.fixture
def screen(app):
    frame = wx.Frame(None)
    frame.Hide()
    scr = ReviewScreen(frame)
    yield scr
    frame.Destroy()


def _sample_data():
    # index 0 answered, 1 not answered, 2 answered+marked, 3 not answered+marked
    return [
        {"index": 0, "question_id": "q0", "answered": True, "marked": False},
        {"index": 1, "question_id": "q1", "answered": False, "marked": False},
        {"index": 2, "question_id": "q2", "answered": True, "marked": True},
        {"index": 3, "question_id": "q3", "answered": False, "marked": True},
    ]


def test_row_count_and_statuses(screen):
    screen.load_review(_sample_data())
    lc = screen.list_ctrl
    assert lc.GetItemCount() == 4

    def status(row):
        return lc.GetItemText(row, 1)

    assert status(0) == "Answered"
    assert status(1) == "Not Answered"
    assert status(2) == "Answered"
    assert status(3) == "Not Answered"

    # Question Number column is 1-based.
    assert lc.GetItemText(0, 0) == "1"
    assert lc.GetItemText(3, 0) == "4"

    # Marked glyph present on marked rows, empty otherwise.
    assert lc.GetItemText(0, 2) == ""
    assert lc.GetItemText(2, 2) != ""
    assert lc.GetItemText(3, 2) != ""


def test_columns_are_ets_no_correctness(screen):
    lc = screen.list_ctrl
    assert lc.GetColumnCount() == 3
    headers = [lc.GetColumn(i).GetText() for i in range(lc.GetColumnCount())]
    assert headers == ["Question Number", "Status", "Marked"]
    # Simulation fidelity: no correctness / score column anywhere.
    for h in headers:
        low = h.lower()
        assert "correct" not in low
        assert "score" not in low


def test_optional_status_override(screen):
    data = [
        {"index": 0, "question_id": "q0", "answered": True, "marked": False,
         "status": "Not Seen"},
        {"index": 1, "question_id": "q1", "answered": True, "marked": False,
         "status": "Incomplete"},
        {"index": 2, "question_id": "q2", "answered": True, "marked": False},
    ]
    screen.load_review(data)
    lc = screen.list_ctrl
    assert lc.GetItemText(0, 1) == "Not Seen"
    assert lc.GetItemText(1, 1) == "Incomplete"
    assert lc.GetItemText(2, 1) == "Answered"


def test_goto_fires_on_row_activation(screen):
    screen.load_review(_sample_data())
    got = []
    screen.set_on_goto(lambda idx: got.append(idx))

    evt = wx.ListEvent(wx.wxEVT_COMMAND_LIST_ITEM_ACTIVATED,
                       screen.list_ctrl.GetId())
    evt.SetIndex(2)
    screen._on_item_activated(evt)
    assert got == [2]


def test_numeric_goto_fires_with_index_minus_one(screen):
    screen.load_review(_sample_data())
    got = []
    screen.set_on_goto(lambda idx: got.append(idx))

    # User types "3" (1-based) → callback gets index 2.
    screen.goto_spin.SetValue(3)
    screen._on_goto_number(None)
    assert got == [2]

    # Boundary: "1" → index 0.
    screen.goto_spin.SetValue(1)
    screen._on_goto_number(None)
    assert got == [2, 0]


def test_numeric_goto_range_clamped_to_rows(screen):
    screen.load_review(_sample_data())
    # SpinCtrl max should track the row count so out-of-range can't be typed.
    assert screen.goto_spin.GetMax() == 4
    assert screen.goto_spin.GetMin() == 1


def test_end_section_fires_on_confirm(screen, monkeypatch):
    screen.load_review(_sample_data())
    fired = []
    screen.set_on_end_section(lambda: fired.append(True))

    class _FakeDlg:
        def __init__(self, *a, **k):
            pass

        def ShowModal(self):
            return wx.ID_YES

        def Destroy(self):
            pass

    monkeypatch.setattr(wx, "MessageDialog", _FakeDlg)
    screen._on_end_click(None)
    assert fired == [True]


def test_end_section_not_fired_on_cancel(screen, monkeypatch):
    screen.load_review(_sample_data())
    fired = []
    screen.set_on_end_section(lambda: fired.append(True))

    class _FakeDlg:
        def __init__(self, *a, **k):
            pass

        def ShowModal(self):
            return wx.ID_NO

        def Destroy(self):
            pass

    monkeypatch.setattr(wx, "MessageDialog", _FakeDlg)
    screen._on_end_click(None)
    assert fired == []


def test_return_callback_fires(screen):
    fired = []
    screen.set_on_return(lambda: fired.append(True))
    screen._on_return_click(None)
    assert fired == [True]


# ── ExamChrome re-skin ────────────────────────────────────────────────


def test_mounts_examchrome_with_minimal_ribbon(screen):
    """Review re-skins onto ExamChrome with a minimal [exit, return, continue]
    ribbon (Exit/Continue both end the section, Return goes back)."""
    assert screen.chrome is not None
    assert list(screen.chrome._btns.keys()) == ["exit", "return", "continue"]


def test_ribbon_exit_ends_section(screen, monkeypatch):
    screen.load_review(_sample_data())
    fired = []
    screen.set_on_end_section(lambda: fired.append(True))

    class _FakeDlg:
        def __init__(self, *a, **k):
            pass

        def ShowModal(self):
            return wx.ID_YES

        def Destroy(self):
            pass

    monkeypatch.setattr(wx, "MessageDialog", _FakeDlg)
    screen.chrome._fire("exit")
    assert fired == [True]


def test_ribbon_return_returns(screen):
    fired = []
    screen.set_on_return(lambda: fired.append(True))
    screen.chrome._fire("return")
    assert fired == [True]
