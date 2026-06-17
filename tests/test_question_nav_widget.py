"""
QuestionNav (ETS footer navigator) tests — spec §3.6, §1 navigator states.

Headless wx tests that construct the widget on a hidden frame and exercise:
  * `set_state` renders a mix of current / answered / unanswered / marked
    without error.
  * Clicking a circle calls the navigate callback with the right index.
  * `set_progress_hidden(True/False)` hides / shows the circle strip and flips
    the toggle label.
  * `marked` is an independent axis — a question can be Answered+Marked or
    Unanswered+Marked, and the marked set is tracked separately from answered.
  * The preserved public API (constructor, set_state, set_on_navigate, rebuild)
    keeps working.
"""
import pytest


@pytest.fixture
def wx_app():
    """Instantiate a wx.App once per test; skip on headless envs."""
    wx = pytest.importorskip("wx")
    try:
        app = wx.App(False)
    except SystemExit as exc:  # pragma: no cover — headless CI
        pytest.skip(f"wx.App cannot start: {exc}")
    except Exception as exc:  # pragma: no cover — headless CI
        pytest.skip(f"wx.App cannot start: {exc}")
    yield app


def _make_nav(wx, total=15):
    """Build a QuestionNav on a hidden frame, sized so circles lay out."""
    from widgets.question_nav import QuestionNav
    frame = wx.Frame(None)
    nav = QuestionNav(frame, total)
    # Give the panel a real size so the owner-drawn strip can lay out circles
    # across a row (no Show() needed — we drive geometry directly).
    frame.SetSize((900, 140))
    frame.Layout()
    nav.SetSize((880, 110))
    nav.Layout()
    return frame, nav


def test_set_state_renders_mixed_states(wx_app):
    """A mix of current / answered / unanswered / marked renders without error."""
    import wx
    frame, nav = _make_nav(wx, total=15)
    try:
        # current=2, answered {0,1,2,5}, marked {1,7,2} (marked overlaps
        # answered at 1 and 2, and stands alone at 7 -> unanswered+marked).
        nav.set_state(2, {0, 1, 2, 5}, {1, 2, 7})

        # State propagated to the owner-drawn strip.
        assert nav.strip.current_index == 2
        assert nav.strip.answered == {0, 1, 2, 5}
        assert nav.strip.marked == {1, 2, 7}

        # Force a paint cycle to confirm the draw path doesn't raise.
        nav.strip.Refresh()
        nav.strip.Update()
        wx.SafeYield()
    finally:
        frame.Destroy()


def test_click_circle_calls_navigate_with_index(wx_app):
    """Clicking a circle invokes the navigate callback with that index."""
    import wx
    frame, nav = _make_nav(wx, total=15)
    try:
        clicked = []
        nav.set_on_navigate(lambda idx: clicked.append(idx))
        nav.set_state(0, set(), set())

        # Trigger a layout/paint so hit-rects exist, then locate circle #4.
        nav.strip.Refresh()
        nav.strip.Update()
        rect = nav.strip.rect_for(4)
        assert rect is not None, "circle 4 should have a hit rectangle"

        # Simulate a left-up inside circle #4's rectangle.
        center = wx.Point(rect.x + rect.width // 2, rect.y + rect.height // 2)
        evt = wx.MouseEvent(wx.wxEVT_LEFT_UP)
        evt.SetPosition(center)
        nav.strip._on_left_up(evt)

        assert clicked == [4], f"expected navigate(4), got {clicked}"

        # index_at hit-testing agrees with the painted layout.
        assert nav.strip.index_at(center) == 4
    finally:
        frame.Destroy()


def test_set_progress_hidden_hides_strip(wx_app):
    """set_progress_hidden(True) hides the strip and flips the toggle label."""
    import wx
    frame, nav = _make_nav(wx, total=12)
    try:
        # Visible by default.
        assert nav.is_progress_hidden() is False
        assert nav.strip.IsShown() is True
        assert nav.toggle_btn.GetLabel() == "Hide Progress"

        nav.set_progress_hidden(True)
        assert nav.is_progress_hidden() is True
        assert nav.strip.IsShown() is False
        assert nav.toggle_btn.GetLabel() == "Show Progress"

        # And back.
        nav.set_progress_hidden(False)
        assert nav.is_progress_hidden() is False
        assert nav.strip.IsShown() is True
        assert nav.toggle_btn.GetLabel() == "Hide Progress"
    finally:
        frame.Destroy()


def test_toggle_button_toggles_progress(wx_app):
    """Clicking the toggle button flips hidden state and label."""
    import wx
    frame, nav = _make_nav(wx, total=12)
    try:
        nav._on_toggle(wx.CommandEvent(wx.wxEVT_BUTTON))
        assert nav.is_progress_hidden() is True
        assert nav.toggle_btn.GetLabel() == "Show Progress"

        nav._on_toggle(wx.CommandEvent(wx.wxEVT_BUTTON))
        assert nav.is_progress_hidden() is False
        assert nav.toggle_btn.GetLabel() == "Hide Progress"
    finally:
        frame.Destroy()


def test_marked_independent_of_answered(wx_app):
    """Marked is an independent axis: Answered+Marked and Unanswered+Marked
    both coexist, and clearing answered does not clear marked."""
    import wx
    frame, nav = _make_nav(wx, total=10)
    try:
        # q3 = answered+marked; q6 = unanswered+marked; q1 = answered only.
        nav.set_state(0, {1, 3}, {3, 6})
        assert 3 in nav.answered and 3 in nav.marked   # answered + marked
        assert 6 not in nav.answered and 6 in nav.marked  # unanswered + marked
        assert 1 in nav.answered and 1 not in nav.marked  # answered only

        # Re-set with NO answered indices but same marked set: marked persists
        # entirely independent of the answered axis.
        nav.set_state(0, set(), {3, 6})
        assert nav.answered == set()
        assert nav.marked == {3, 6}
        assert nav.strip.marked == {3, 6}

        nav.strip.Refresh()
        nav.strip.Update()
        wx.SafeYield()
    finally:
        frame.Destroy()


def test_rebuild_resets_state_and_count(wx_app):
    """rebuild(n) resets state and re-sizes the strip to n circles."""
    import wx
    frame, nav = _make_nav(wx, total=15)
    try:
        nav.set_state(4, {0, 1}, {2})
        nav.rebuild(12)
        assert nav.total == 12
        assert nav.strip.total == 12
        assert nav.current_index == 0
        assert nav.answered == set()
        assert nav.marked == set()
    finally:
        frame.Destroy()


def test_zero_questions_constructs(wx_app):
    """The zero-question edge case constructs without raising (as callers do:
    QuestionNav(self, 0) then rebuild later)."""
    import wx
    from widgets.question_nav import QuestionNav
    frame = wx.Frame(None)
    try:
        nav = QuestionNav(frame, 0)
        assert nav.total == 0
        nav.SetSize((800, 100))
        nav.strip.Refresh()
        nav.strip.Update()
        wx.SafeYield()
        nav.rebuild(15)
        assert nav.strip.total == 15
    finally:
        frame.Destroy()
