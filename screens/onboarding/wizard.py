"""
Onboarding wizard — a single Panel that swaps between 3 steps.

Lives in `screens/onboarding/wizard.py` instead of three separate screens
because (a) only one is visible at a time, (b) sharing state (target score,
test date) is simpler in one class, and (c) the wizard is itself a screen
in `MainFrame.screens` (`name="onboarding"`).

State is persisted to `UserStats` only after the user finishes (or skips)
the final step, so a partial-onboarding user who quits mid-wizard re-enters
on the next launch.
"""
from datetime import datetime, timedelta

import wx
import wx.adv

from widgets import ui_scale
from widgets.card import Card
from widgets.primary_button import PrimaryButton
from widgets.secondary_button import SecondaryButton
from widgets.theme import Color


class OnboardingWizard(wx.Panel):
    """3-step wizard: Welcome → Goal → Diagnostic offer."""

    STEP_WELCOME    = 0
    STEP_GOAL       = 1
    STEP_DIAGNOSTIC = 2

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)
        self._on_finish = None         # callback(do_diagnostic: bool, params: dict | None)
        self._on_skip = None           # callback() — fully skip
        self._step = self.STEP_WELCOME
        self._goal_data = None         # populated after step 2

        self._sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self._sizer)

        self._render_step()

    # ── public API ────────────────────────────────────────────────────

    def set_on_finish(self, cb):
        """callback(do_diagnostic: bool, goal_params: dict | None)."""
        self._on_finish = cb

    def set_on_skip(self, cb):
        """callback() — user clicked skip on any step (fully exits wizard)."""
        self._on_skip = cb

    # ── internals ─────────────────────────────────────────────────────

    def _render_step(self):
        self._sizer.Clear(True)

        # Centered card with breathing room.
        spacer_top = wx.Panel(self, size=(-1, ui_scale.space(12)))
        spacer_top.SetBackgroundColour(Color.BG_PAGE)
        self._sizer.Add(spacer_top, 0, wx.EXPAND)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.AddStretchSpacer(1)
        if self._step == self.STEP_WELCOME:
            row.Add(self._build_welcome(), 0, wx.EXPAND)
        elif self._step == self.STEP_GOAL:
            row.Add(self._build_goal(), 0, wx.EXPAND)
        else:
            row.Add(self._build_diagnostic(), 0, wx.EXPAND)
        row.AddStretchSpacer(1)
        self._sizer.Add(row, 1, wx.EXPAND)

        self.Layout()

    def _step_label(self, n: int) -> wx.StaticText:
        lbl = wx.StaticText(self, label=f"Step {n} of 3")
        lbl.SetForegroundColour(Color.TEXT_TERTIARY)
        lbl.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        return lbl

    def _heading(self, text: str) -> wx.StaticText:
        h = wx.StaticText(self, label=text)
        h.SetForegroundColour(Color.TEXT_PRIMARY)
        h.SetFont(wx.Font(
            ui_scale.text_2xl(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        return h

    def _body(self, text: str, max_width: int = 520) -> wx.StaticText:
        b = wx.StaticText(self, label=text)
        b.SetForegroundColour(Color.TEXT_SECONDARY)
        b.SetFont(wx.Font(
            ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        ))
        b.Wrap(ui_scale.font_size(max_width))
        return b

    # ── Step 1: Welcome ───────────────────────────────────────────────

    def _build_welcome(self) -> wx.Sizer:
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.SetMinSize((ui_scale.font_size(560), -1))

        sizer.Add(self._heading("Welcome to GRE prep with AI"), 0,
                  wx.BOTTOM, ui_scale.space(3))
        sizer.Add(self._body(
            "In 3 quick steps we'll set up a study plan tailored to your "
            "goal score, test date, and current strengths."
        ), 0, wx.BOTTOM, ui_scale.space(5))

        bullets = [
            "▸  Set your target score and test date",
            "▸  Take a 30-min diagnostic (or skip and explore)",
            "▸  Get a week-by-week plan you can follow",
        ]
        for line in bullets:
            sizer.Add(self._body(line), 0, wx.BOTTOM, ui_scale.space(2))

        sizer.AddSpacer(ui_scale.space(8))

        row = wx.BoxSizer(wx.HORIZONTAL)
        skip = SecondaryButton(self, label="Skip — I'll explore")
        skip.SetMinSize((ui_scale.font_size(220), ui_scale.space(11)))
        skip.Bind(wx.EVT_BUTTON, lambda _: self._skip())
        row.Add(skip, 0)
        row.AddStretchSpacer(1)
        cont = PrimaryButton(self, label="Continue →",
                             height=ui_scale.space(11))
        cont.SetMinSize((ui_scale.font_size(220), -1))
        cont.Bind(wx.EVT_BUTTON, lambda _: self._goto_step(self.STEP_GOAL))
        row.Add(cont, 0)
        sizer.Add(row, 0, wx.EXPAND)

        return sizer

    # ── Step 2: Goal ──────────────────────────────────────────────────

    def _build_goal(self) -> wx.Sizer:
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.SetMinSize((ui_scale.font_size(560), -1))

        sizer.Add(self._step_label(2), 0, wx.BOTTOM, ui_scale.space(2))
        sizer.Add(self._heading("Set your goal"), 0,
                  wx.BOTTOM, ui_scale.space(3))
        sizer.Add(self._body(
            "Don't worry about getting these exactly right — you can "
            "adjust them anytime from the Insights tab."
        ), 0, wx.BOTTOM, ui_scale.space(5))

        # Target combined score.
        target_lbl = wx.StaticText(
            self, label="Target combined score (260–340):")
        target_lbl.SetForegroundColour(Color.TEXT_SECONDARY)
        target_lbl.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        sizer.Add(target_lbl, 0, wx.BOTTOM, ui_scale.space(1))
        self._target_spin = wx.SpinCtrl(self, min=260, max=340, initial=320,
                                        size=(ui_scale.font_size(120), -1))
        sizer.Add(self._target_spin, 0, wx.BOTTOM, ui_scale.space(4))

        # Test date.
        date_lbl = wx.StaticText(self, label="Test date:")
        date_lbl.SetForegroundColour(Color.TEXT_SECONDARY)
        date_lbl.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        sizer.Add(date_lbl, 0, wx.BOTTOM, ui_scale.space(1))
        default_date = wx.DateTime.Today() + wx.DateSpan(months=2)
        self._date_picker = wx.adv.DatePickerCtrl(
            self, dt=default_date,
            style=wx.adv.DP_DEFAULT | wx.adv.DP_SHOWCENTURY,
        )
        sizer.Add(self._date_picker, 0, wx.BOTTOM, ui_scale.space(1))
        # Locale hint — DatePickerCtrl renders in the OS's date format,
        # which catches non-US users off-guard.
        date_hint = wx.StaticText(self, label="(format follows your system locale)")
        date_hint.SetForegroundColour(Color.TEXT_TERTIARY)
        date_hint.SetFont(wx.Font(
            ui_scale.text_xs(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL,
        ))
        sizer.Add(date_hint, 0, wx.BOTTOM, ui_scale.space(4))

        # Hours per week.
        hpw_lbl = wx.StaticText(self, label="Study hours per week:")
        hpw_lbl.SetForegroundColour(Color.TEXT_SECONDARY)
        hpw_lbl.SetFont(wx.Font(
            ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD,
        ))
        sizer.Add(hpw_lbl, 0, wx.BOTTOM, ui_scale.space(1))
        self._hpw_spin = wx.SpinCtrl(self, min=1, max=80, initial=10,
                                     size=(ui_scale.font_size(120), -1))
        sizer.Add(self._hpw_spin, 0, wx.BOTTOM, ui_scale.space(8))

        row = wx.BoxSizer(wx.HORIZONTAL)
        skip = SecondaryButton(self, label="Skip for now")
        skip.SetMinSize((ui_scale.font_size(220), ui_scale.space(11)))
        skip.Bind(wx.EVT_BUTTON, lambda _: self._skip())
        row.Add(skip, 0)
        row.AddStretchSpacer(1)
        cont = PrimaryButton(self, label="Continue →",
                             height=ui_scale.space(11))
        cont.SetMinSize((ui_scale.font_size(220), -1))
        cont.Bind(wx.EVT_BUTTON, lambda _: self._step2_continue())
        row.Add(cont, 0)
        sizer.Add(row, 0, wx.EXPAND)

        return sizer

    def _step2_continue(self):
        date_picker_value = self._date_picker.GetValue()
        py_date = datetime(
            date_picker_value.GetYear(),
            date_picker_value.GetMonth() + 1,   # wx month is 0-indexed
            date_picker_value.GetDay(),
        )
        self._goal_data = {
            "target_score": self._target_spin.GetValue(),
            "test_date": py_date,
            "hours_per_week": self._hpw_spin.GetValue(),
        }
        self._goto_step(self.STEP_DIAGNOSTIC)

    # ── Step 3: Diagnostic offer ──────────────────────────────────────

    def _build_diagnostic(self) -> wx.Sizer:
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.SetMinSize((ui_scale.font_size(560), -1))

        sizer.Add(self._step_label(3), 0, wx.BOTTOM, ui_scale.space(2))
        sizer.Add(self._heading("Take a 30-minute diagnostic now?"), 0,
                  wx.BOTTOM, ui_scale.space(3))
        sizer.Add(self._body(
            "The diagnostic calibrates your weakness ranking and unlocks "
            "the AI study-plan generator. ~30 questions, untimed, you can "
            "pause anytime. You can always take it later from the "
            "Practice tab."
        ), 0, wx.BOTTOM, ui_scale.space(8))

        row = wx.BoxSizer(wx.HORIZONTAL)
        skip = SecondaryButton(self, label="Skip — take it later")
        skip.SetMinSize((ui_scale.font_size(240), ui_scale.space(11)))
        skip.Bind(wx.EVT_BUTTON, lambda _: self._finish(do_diagnostic=False))
        row.Add(skip, 0)
        row.AddStretchSpacer(1)
        cont = PrimaryButton(self, label="Take diagnostic now →",
                             height=ui_scale.space(11))
        cont.SetMinSize((ui_scale.font_size(240), -1))
        cont.Bind(wx.EVT_BUTTON, lambda _: self._finish(do_diagnostic=True))
        row.Add(cont, 0)
        sizer.Add(row, 0, wx.EXPAND)

        return sizer

    # ── transitions ───────────────────────────────────────────────────

    def _goto_step(self, step: int):
        self._step = step
        self._render_step()

    def _skip(self):
        # Caller is responsible for marking onboarding complete.
        if self._on_skip:
            self._on_skip()

    def _finish(self, do_diagnostic: bool):
        if self._on_finish:
            self._on_finish(do_diagnostic, self._goal_data)
