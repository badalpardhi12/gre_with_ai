"""
Main application frame — orchestrates screen switching, exam flow, and scoring.

The window is split into a persistent left sidebar (`widgets/sidebar.py`)
and a content area. The content area hosts every screen panel and shows
exactly one at a time using the legacy Show/Hide pattern. Sidebar tabs
map to canonical "home" screens; in-flow screens (Question, AWA, Review,
Results, Instructions, DiagnosticResults) take over the content area
without changing the sidebar selection.
"""
import json
from datetime import datetime

import wx

from config import MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT
from models.database import (
    db, init_db, Session as DBSession, SectionResult, Response,
    ScoringResult, AWASubmission, AWAResult,
)
from models.exam_session import (
    ExamSession, ExamState, SectionType, SECTION_META,
)
from services.question_bank import QuestionBankService
from services.scoring import ScoringEngine
from services.awa_scorer import AWAScoringService
from services.llm_service import llm_service
from services.analytics import AnalyticsService

from screens.instructions_screen import InstructionsScreen
from screens.awa_screen import AWAScreen
from screens.question_screen import QuestionScreen
from screens.review_screen import ReviewScreen
from screens.results_screen import ResultsScreen
from screens.llm_settings import LLMSettingsDialog
from screens.study_plan_dialog import StudyPlanDialog
from screens.vocab_screen import VocabScreen
from screens.diagnostic_results_screen import DiagnosticResultsScreen
from screens.onboarding.wizard import OnboardingWizard
from screens.today_screen import TodayScreen
from screens.learn_screen import LearnScreen
from screens.practice_screen import PracticeScreen
from screens.insights_screen import InsightsScreen

from widgets.sidebar import Sidebar
from widgets.theme import Color


# Sidebar tab id → name of the "home" screen for that tab. PRs 4-5 will
# replace the topics / progress entries with purpose-built Learn / Practice
# / Insights screens; this mapping keeps every tab functional during the
# transition.
TAB_HOME_SCREEN = {
    "today":    "today",
    "learn":    "learn",
    "practice": "practice",
    "vocab":    "vocab",
    "insights": "insights",
}

# Reverse lookup: which sidebar tab should be highlighted when a given
# screen is showing? Non-tab in-flow screens preserve the previous selection.
SCREEN_TO_TAB = {
    "today":     "today",
    "learn":     "learn",
    "practice":  "practice",
    "vocab":     "vocab",
    "insights":  "insights",
    "diagnostic_results": "insights",
}


class MainFrame(wx.Frame):
    """
    Top-level window. Manages panel switching and exam orchestration.
    """

    def __init__(self):
        super().__init__(
            None,
            title="GRE Mock Test Platform",
            size=(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        self.SetMinSize((MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT))
        self.Centre()

        # Core services
        init_db()
        self.question_bank = QuestionBankService()
        self.scoring_engine = ScoringEngine()

        # Recover any abandoned in-progress sessions and surface stale
        # autosave-journal data from the previous launch.
        self._recover_orphaned_state()

        # Best-effort audit at launch: log the live-bank corruption
        # summary so future regressions show up in setup.log without
        # interrupting the user.
        self._log_audit_summary_at_launch()

        # Exam state
        self.exam = None
        self.db_session = None

        # ── Layout: sidebar (left) | content (right) ──────────────────
        self.SetBackgroundColour(Color.BG_PAGE)
        root_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.sidebar = Sidebar(self)
        self.sidebar.set_on_select(self._on_sidebar_select)
        root_sizer.Add(self.sidebar, 0, wx.EXPAND)

        self.panel_container = wx.Panel(self)
        self.panel_container.SetBackgroundColour(Color.BG_PAGE)
        self.main_sizer = wx.BoxSizer(wx.VERTICAL)
        self.panel_container.SetSizer(self.main_sizer)
        root_sizer.Add(self.panel_container, 1, wx.EXPAND)

        self.SetSizer(root_sizer)

        # ── Create all screen panels ──────────────────────────────────
        self.screens = {}
        self._create_screens()

        # Seed sidebar with the current streak (empty string if 0).
        try:
            from services.streak import streak_label
            self.sidebar.set_streak(streak_label())
        except Exception:
            pass

        # First-launch wizard: if the user hasn't been through onboarding,
        # land them on the wizard instead of Today.
        try:
            from services.streak import is_onboarded
            needs_onboarding = not is_onboarded()
        except Exception:
            needs_onboarding = False

        if needs_onboarding:
            self._show_screen("onboarding")
        else:
            self._on_sidebar_select("today")

        # ── Native menu bar ──────────────────────────────────────────
        self._build_menu_bar()

        # ── Keyboard shortcuts (ESC = back to dashboard) ─────────────
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

        # Drop the UI-scale cache when the user moves the window between
        # displays of different DPI so fonts re-scale on the next layout.
        self.Bind(wx.EVT_DISPLAY_CHANGED, self._on_display_changed)

        # Close confirmation
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _on_display_changed(self, event):
        from widgets import ui_scale
        ui_scale.invalidate_scale_cache()
        self.Layout()
        event.Skip()

    def _build_menu_bar(self):
        """Build a native macOS menu bar with standard IDs."""
        menubar = wx.MenuBar()

        # File menu
        file_menu = wx.Menu()
        file_menu.Append(wx.ID_NEW, "&New Test\tCtrl+N", "Start a new test")
        file_menu.AppendSeparator()
        file_menu.Append(wx.ID_PREFERENCES, "&Preferences\tCtrl+,",
                         "LLM and app settings")
        file_menu.AppendSeparator()
        file_menu.Append(wx.ID_EXIT, "E&xit\tCtrl+Q", "Quit GRE with AI")
        menubar.Append(file_menu, "&File")

        # View menu — sidebar tabs reachable as Cmd+1..5 plus the legacy
        # named shortcuts so muscle memory still works.
        view_menu = wx.Menu()
        view_menu.Append(2010, "&Today\tCtrl+1", "Today's plan and quick start")
        view_menu.Append(2011, "&Learn\tCtrl+2", "Lessons and topic mastery")
        view_menu.Append(2012, "&Practice\tCtrl+3", "Drills, section tests, full mocks")
        view_menu.Append(2013, "&Vocab\tCtrl+4", "Daily vocabulary review")
        view_menu.Append(2014, "&Insights\tCtrl+5", "Forecast, history, study plan")
        menubar.Append(view_menu, "&View")

        # Help menu
        help_menu = wx.Menu()
        help_menu.Append(wx.ID_ABOUT, "&About GRE with AI",
                         "About this application")
        menubar.Append(help_menu, "&Help")

        self.SetMenuBar(menubar)

        # Bind menu actions
        self.Bind(wx.EVT_MENU, lambda _: self._on_sidebar_select("today"), id=2010)
        self.Bind(wx.EVT_MENU, lambda _: self._on_sidebar_select("learn"), id=2011)
        self.Bind(wx.EVT_MENU, lambda _: self._on_sidebar_select("practice"), id=2012)
        self.Bind(wx.EVT_MENU, lambda _: self._on_sidebar_select("vocab"), id=2013)
        self.Bind(wx.EVT_MENU, lambda _: self._on_sidebar_select("insights"), id=2014)
        self.Bind(wx.EVT_MENU, lambda _: self._show_settings(), id=wx.ID_PREFERENCES)
        self.Bind(wx.EVT_MENU, lambda _: self._show_about(), id=wx.ID_ABOUT)
        self.Bind(wx.EVT_MENU, lambda _: self.Close(), id=wx.ID_EXIT)
        self.Bind(wx.EVT_MENU, lambda _: self._new_test(), id=wx.ID_NEW)

    def _show_about(self):
        wx.MessageBox(
            "GRE with AI\n\n"
            "A comprehensive GRE prep platform powered by Claude Opus 4.7.\n\n"
            "Features:\n"
            "• 1,500+ practice questions across all GRE subtopics\n"
            "• AI-generated lessons and study plans\n"
            "• Spaced-repetition vocabulary flashcards\n"
            "• Diagnostic test + adaptive practice\n"
            "• AI tutor for per-question Q&A\n"
            "• AWA essay scoring with rubric feedback",
            "About GRE with AI",
            wx.OK | wx.ICON_INFORMATION,
        )

    def _new_test(self):
        """Cmd+N — go to dashboard so user can pick a test type."""
        self._go_home()

    def _on_char_hook(self, event):
        """Global keyboard shortcuts."""
        key = event.GetKeyCode()
        # ESC: smart back navigation (only if not in a modal or active test)
        if key == wx.WXK_ESCAPE:
            current = None
            for name, panel in self.screens.items():
                if panel.IsShown():
                    current = name
                    break
            # From these screens, ESC goes back to Today
            if current in ("vocab", "learn", "practice", "insights",
                           "diagnostic_results"):
                self._go_home()
                return
            # From instructions, abort the test
            if current == "instructions":
                self._abort_test()
                return
        event.Skip()

    def _create_screens(self):
        """Instantiate all screen panels (hidden by default).

        WelcomeScreen was deleted in the UI overhaul; the sidebar + Today tab
        replaces it. PRs 3-5 will add today_screen / learn_screen /
        practice_screen / insights_screen and remove dashboard / topics /
        lesson / progress here.
        """
        screen_classes = {
            "today": TodayScreen,
            "learn": LearnScreen,
            "practice": PracticeScreen,
            "vocab": VocabScreen,
            "insights": InsightsScreen,
            "instructions": InstructionsScreen,
            "awa": AWAScreen,
            "question": QuestionScreen,
            "review": ReviewScreen,
            "results": ResultsScreen,
            "diagnostic_results": DiagnosticResultsScreen,
            "onboarding": OnboardingWizard,
        }

        for name, cls in screen_classes.items():
            panel = cls(self.panel_container)
            panel.Hide()
            self.main_sizer.Add(panel, 1, wx.EXPAND)
            self.screens[name] = panel

        # Wire callbacks
        self.screens["instructions"].set_on_begin(self._begin_section)
        self.screens["instructions"].set_on_cancel(self._abort_test)
        self.screens["awa"].set_on_submit(self._submit_awa)
        self.screens["awa"].set_on_time_expire(self._awa_time_expired)
        self.screens["awa"].set_on_exit(self._abort_test)
        self.screens["question"].set_on_end_section(self._end_current_section)
        self.screens["question"].set_on_time_expire(self._end_current_section)
        self.screens["question"].set_on_review(self._show_review)
        self.screens["question"].set_on_exit_to_dashboard(self._abort_test)
        self.screens["review"].set_on_goto(self._goto_question)
        self.screens["review"].set_on_return(self._return_to_questions)
        self.screens["review"].set_on_end_section(self._end_current_section)
        self.screens["results"].set_on_home(self._go_home)

        self.screens["vocab"].set_on_back(lambda: self._on_sidebar_select("today"))
        self.screens["learn"].set_on_start_drill(self._on_practice_topic)
        self.screens["practice"].set_handlers(
            quick_drill=self._on_start_quick_drill,
            section_test=self._start_section_test,
            full_mock=lambda: self._start_test("full_mock", "simulation"),
        )
        self.screens["insights"].set_handlers(
            update_plan=self._open_plan_dialog,
            run_coach=self._run_coach_now,
        )
        self.screens["diagnostic_results"].set_on_back(self._go_home)
        self.screens["diagnostic_results"].set_on_build_plan(
            lambda: (self._go_home(), self._open_plan_dialog()))

        # Onboarding wizard wiring
        self.screens["onboarding"].set_on_skip(self._on_onboarding_skip)
        self.screens["onboarding"].set_on_finish(self._on_onboarding_finish)

        # Today screen wiring (the new home tab).
        self.screens["today"].set_handlers(
            take_diagnostic=self._on_take_diagnostic,
            start_drill=self._on_start_quick_drill,
            start_vocab=self._on_start_vocab,
            start_full_mock=lambda: self._start_test("full_mock", "simulation"),
            open_plan_dialog=self._open_plan_dialog,
            browse_topics=lambda: self._on_sidebar_select("learn"),
            open_tutor=self._open_tutor,
            open_insights=lambda: self._on_sidebar_select("insights"),
        )

    def _show_screen(self, name):
        """Show one screen, hide all others.

        Sidebar selection is auto-synced when the screen has a known tab
        mapping. In-flow screens (question, awa, review, results,
        instructions) preserve the previous sidebar selection so the
        active tab still reflects "where the user is conceptually" even
        while taking a test.
        """
        for sname, panel in self.screens.items():
            panel.Show(sname == name)
        target_tab = SCREEN_TO_TAB.get(name)
        if target_tab:
            self.sidebar.set_active(target_tab)
        self.panel_container.Layout()

    def _on_sidebar_select(self, tab_id: str):
        """Sidebar callback. Routes to the canonical home screen for the tab.

        The Settings cog uses a sentinel id and opens the modal dialog rather
        than activating a tab.
        """
        if tab_id == self.sidebar.SETTINGS_ID:
            self._show_settings()
            return
        screen_name = TAB_HOME_SCREEN.get(tab_id, "today")
        if screen_name in self.screens:
            self.sidebar.set_active(tab_id)
            self._show_screen(screen_name)
            # Refresh the home screens that have a refresh() so dynamic
            # data (forecast, mastery, vocab counts) is up-to-date when
            # the user clicks back into the tab.
            screen = self.screens[screen_name]
            if screen_name == "vocab":
                # Vocab needs a fresh session built each visit (or the
                # screen renders an empty card with no controls). The
                # method is idempotent — calls _show_empty_state if no
                # cards are due.
                try:
                    screen.start_session(new_count=20)
                except Exception:
                    from services.log import get_logger
                    get_logger("main_frame").exception(
                        "vocab start_session failed")
            elif hasattr(screen, "refresh"):
                try:
                    screen.refresh()
                except Exception:
                    pass

    # ── Test Flow ─────────────────────────────────────────────────────

    def _start_test(self, test_type, mode):
        """Start a new test session."""
        # Validate question availability
        if test_type in ("full_mock", "verbal"):
            v_count = self.question_bank.get_question_count("verbal")
            if v_count < 12:
                wx.MessageBox(
                    f"Not enough verbal questions ({v_count} available, need at least 12). "
                    "Please run the data import script first.",
                    "Insufficient Questions", wx.OK | wx.ICON_WARNING, self)
                return
        if test_type in ("full_mock", "quant"):
            q_count = self.question_bank.get_question_count("quant")
            if q_count < 12:
                wx.MessageBox(
                    f"Not enough quant questions ({q_count} available, need at least 12). "
                    "Please run the data import script first.",
                    "Insufficient Questions", wx.OK | wx.ICON_WARNING, self)
                return

        # Create exam session
        self.exam = ExamSession(test_type=test_type, mode=mode)

        if test_type == "full_mock":
            self.exam.build_full_mock(self.question_bank)
        elif test_type == "verbal":
            self.exam.build_section_test("verbal", self.question_bank)
        elif test_type == "quant":
            self.exam.build_section_test("quant", self.question_bank)

        # Create DB session — mark in-progress so abandoned-session cleanup
        # can find it.
        db.connect(reuse_if_open=True)
        self.db_session = DBSession.create(
            test_type=test_type,
            mode=mode,
            state="in_progress",
            started_at=datetime.now(),
            section_order=json.dumps([s.value for s in self.exam.section_order]),
        )

        # Show instructions for first section
        self._show_section_instructions()

    def _show_section_instructions(self):
        """Show instructions for the current section."""
        sec_type = self.exam.current_section_type
        if sec_type is None:
            self._finish_test()
            return

        self.screens["instructions"].set_section(
            sec_type, self.exam.sections.get(sec_type),
        )
        self._show_screen("instructions")

    def _begin_section(self):
        """Begin the current section after instructions."""
        sec_type = self.exam.current_section_type
        if sec_type is None:
            return

        self.exam.start()

        measure, sec_idx, time_limit, q_count = SECTION_META[sec_type]

        if measure == "awa":
            self._start_awa_section()
        else:
            self._start_question_section(sec_type, measure)

    def _start_awa_section(self):
        """Start the AWA section."""
        section = self.exam.current_section
        if not section.question_ids:
            # No AWA prompts available, skip
            self._end_current_section()
            return

        prompt_data = self.question_bank.get_awa_prompt(section.question_ids[0])
        if prompt_data is None:
            prompt_data = {
                "prompt_text": "No AWA prompt available. Please import AWA prompts.",
                "instructions": "",
            }

        # Pass session_id so the editor can autosave drafts to disk.
        self.screens["awa"].load_prompt(
            prompt_data,
            session_id=self.db_session.id if self.db_session else None,
        )
        self.screens["awa"].start_timer()
        self._show_screen("awa")

    def _start_question_section(self, sec_type, measure):
        """Start a Verbal or Quant section."""
        section = self.exam.current_section
        self.screens["question"].configure(
            section, self.question_bank, measure, self.exam.mode,
            exam=self.exam)
        self.screens["question"].start_timer()
        self._show_screen("question")
        self.exam.log_event("section_started", {"section": sec_type.value})

    def _submit_awa(self, essay_text, word_count):
        """Handle AWA essay submission."""
        section = self.exam.current_section
        section.finish()

        # Save submission to DB and capture its id so the async scorer can
        # find it later even if the user has navigated away.
        submission_id = None
        if self.db_session and section.question_ids:
            prompt_id = section.question_ids[0]
            sub = AWASubmission.create(
                session=self.db_session,
                prompt=prompt_id,
                essay_text=essay_text,
                word_count=word_count,
            )
            submission_id = sub.id

        # Score async (non-blocking)
        prompt_data = self.question_bank.get_awa_prompt(section.question_ids[0]) if section.question_ids else {}
        prompt_text = prompt_data.get("prompt_text", "") if prompt_data else ""

        self._awa_essay = essay_text
        self._awa_prompt_text = prompt_text
        self._awa_score = None

        scorer = AWAScoringService(llm_service)
        scorer.score_essay_async(
            essay_text, prompt_text,
            lambda result, err: wx.CallAfter(
                self._on_awa_scored, submission_id, result, err)
        )

        # Move to next section immediately (scoring happens in background)
        self._advance_to_next()

    def _on_awa_scored(self, submission_id, result, error):
        """Callback when AWA scoring completes.

        Uses the captured `submission_id` rather than `self.db_session` so
        a result fired after the user starts a new test is still attributed
        to the correct submission.
        """
        if error:
            self._awa_score = {"score_estimate": None, "error": str(error)}
        else:
            self._awa_score = result

        if not result or result.get("score_estimate") is None:
            return
        if submission_id is None:
            return

        sub = AWASubmission.get_or_none(AWASubmission.id == submission_id)
        if sub is None:
            from services.log import get_logger
            get_logger("main_frame").warning(
                "AWA submission %s vanished before scoring completed; "
                "discarding result", submission_id)
            return

        import json as _json
        from config import load_llm_config
        AWAResult.create(
            submission=sub,
            score_estimate=result["score_estimate"],
            score_confidence_low=result.get("score_confidence_low", 0),
            score_confidence_high=result.get("score_confidence_high", 0),
            rubric_json=_json.dumps(result.get("dimensions", {})),
            feedback_text=result.get("summary", ""),
            model_used=load_llm_config().get("model", "openrouter"),
        )

    def _awa_time_expired(self):
        """Auto-submit AWA when time runs out."""
        essay = self.screens["awa"].get_essay()
        wc = self.screens["awa"].get_word_count()
        self._submit_awa(essay, wc)

    def _end_current_section(self):
        """End the current V/Q section, score it, persist results, and advance."""
        from services.mastery import update_mastery

        section = self.exam.current_section
        if section is None:
            return

        section.finish()

        # Score questions and update per-subtopic mastery as we go.
        # Mastery only counts questions where the user actually submitted an
        # answer; skipped questions are ignored (don't punish for time-out).
        for qid in section.question_ids:
            resp = section.get_response(qid)
            if resp and resp != {}:
                q_data = self.question_bank.get_question(qid)
                if q_data:
                    is_correct = self.scoring_engine.check_answer(q_data, resp)
                    if not hasattr(section, '_correctness'):
                        section._correctness = {}
                    section._correctness[qid] = is_correct
                    # Wire mastery update — this was dead code before PR 2.
                    # Subtopic comes from the indexed Question.subtopic field
                    # (newer rows) or falls back to the first concept tag.
                    subtopic = ""
                    try:
                        from models.database import Question as _Q
                        q_row = _Q.get_or_none(_Q.id == qid)
                        if q_row:
                            subtopic = q_row.subtopic or ""
                            if not subtopic:
                                tags = q_row.get_tags()
                                if tags:
                                    subtopic = tags[0]
                    except Exception:
                        subtopic = ""
                    if subtopic:
                        try:
                            update_mastery(subtopic, is_correct,
                                           q_data.get("difficulty", 3))
                        except Exception:
                            # Mastery update should never break scoring/persistence.
                            from services.log import get_logger
                            get_logger("main_frame").exception(
                                "update_mastery failed for qid=%s", qid)

        # Persist SectionResult and Response rows
        sec_type = self.exam.current_section_type
        if self.db_session and sec_type:
            measure, sec_idx, time_limit, _ = SECTION_META[sec_type]
            correctness = getattr(section, '_correctness', {})
            per_q_time = getattr(section, '_per_question_time', {})
            # All section persistence in one transaction so a crash mid-loop
            # can't leave a SectionResult with a partial set of Response rows.
            with db.atomic():
                sr = SectionResult.create(
                    session=self.db_session,
                    section_name=sec_type.value,
                    measure=measure,
                    section_index=sec_idx,
                    difficulty_band=getattr(section, 'difficulty_band', 'medium'),
                    time_limit_seconds=time_limit,
                    time_used_seconds=section.time_used,
                    started_at=section.started_at,
                    ended_at=section.ended_at,
                    question_ids=json.dumps(section.question_ids),
                )
                for qid in section.question_ids:
                    resp = section.get_response(qid)
                    Response.create(
                        session=self.db_session,
                        section_result=sr,
                        question=qid,
                        response_payload=json.dumps(resp) if resp else "{}",
                        is_marked=qid in section.marked,
                        is_correct=correctness.get(qid),
                        time_spent_seconds=per_q_time.get(qid, 0),
                    )

        # Section-level adaptation
        self.exam.end_current_section()

        # Streak: count this section finish as today's activity.
        try:
            from services.streak import record_activity
            record_activity()
            if hasattr(self, "sidebar"):
                from services.streak import streak_label
                self.sidebar.set_streak(streak_label())
        except Exception:
            from services.log import get_logger
            get_logger("main_frame").exception("streak update failed")

        # Mistake-pattern coach trigger: every 50 wrong answers across the
        # user's full Response history, fire the LLM coach asynchronously
        # and surface a non-blocking notification when it returns.
        self._maybe_trigger_mistake_coach()

        self._advance_to_next()

    def _maybe_trigger_mistake_coach(self):
        """Async mistake-coach if the user has just crossed a 50-mistake mark.

        Uses Response history (not session-scoped) so the trigger fires once
        per 50 lifetime mistakes. Silent no-op if the LLM key isn't configured.
        """
        try:
            from models.database import Response
            wrong_count = (Response
                           .select()
                           .where(Response.is_correct == False)
                           .count())
        except Exception:
            return
        if wrong_count == 0 or wrong_count % 50 != 0:
            return

        # Don't trigger more than once per app session.
        if getattr(self, "_mistake_coach_last_count", -1) == wrong_count:
            return
        self._mistake_coach_last_count = wrong_count

        from services.mistake_coach import analyze_mistakes
        from services.llm_service import llm_service
        import threading

        def _worker():
            try:
                report = analyze_mistakes()
            except Exception:
                from services.log import get_logger
                get_logger("main_frame").exception("analyze_mistakes failed")
                return
            wx.CallAfter(self._show_mistake_coach_report, report)

        threading.Thread(target=_worker, daemon=True).start()

    def _show_mistake_coach_report(self, report: str):
        """Modal showing the mistake-coach diagnosis (Opus output)."""
        if not report:
            return
        dlg = wx.MessageDialog(
            self,
            f"You've reached a 50-mistake checkpoint. Here's what the coach noticed:\n\n{report}",
            "Mistake Pattern Coach",
            wx.OK | wx.ICON_INFORMATION,
        )
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def _advance_to_next(self):
        """Move to the next section or finish."""
        has_more = self.exam.advance_section()
        if has_more:
            self._show_section_instructions()
        else:
            self._finish_test()

    def _finish_test(self):
        """Compute final scores and show results.

        For diagnostic sessions, runs `grade_diagnostic` to produce a
        DiagnosticResult row and routes to the diagnostic-results screen.
        """
        if self.exam and getattr(self.exam, "_is_diagnostic", False):
            self._finish_diagnostic()
            return

        scores = self._compute_final_scores()
        section_summaries = self._build_section_summaries()
        question_details = self._build_question_details()

        # Clean up the autosave journal — the test ended normally; no resume
        # needed.
        if self.exam is not None:
            self.exam.log_event("session_completed", {})
            self.exam.clear_journal()

        self.screens["results"].load_results(scores, section_summaries, question_details)
        self._show_screen("results")

    def _finish_diagnostic(self):
        """End the diagnostic, persist DiagnosticResult, route to results screen."""
        from services.diagnostic import grade_diagnostic

        # Mark the underlying DB session completed even though we're skipping
        # the regular ScoringResult write (the DiagnosticResult is the
        # authoritative artifact for this session).
        if self.db_session:
            self.db_session.state = "completed"
            self.db_session.ended_at = datetime.now()
            self.db_session.save()

        # Build {qid: response_payload} across both diagnostic sections.
        responses = {}
        all_qids = []
        for sec_type, section in self.exam.sections.items():
            for qid in section.question_ids:
                all_qids.append(qid)
                resp = section.get_response(qid)
                if resp:
                    responses[qid] = resp

        try:
            diag = grade_diagnostic(
                question_ids=getattr(self.exam, "_diagnostic_qids", all_qids),
                responses=responses,
            )
        except Exception:
            from services.log import get_logger
            get_logger("main_frame").exception("grade_diagnostic failed")
            wx.MessageBox("Failed to grade diagnostic. See data/gre_app.log for details.",
                          "Diagnostic", wx.OK | wx.ICON_ERROR)
            self._go_home()
            return

        # Diagnostic ended normally — clear the autosave journal.
        if self.exam is not None:
            self.exam.log_event("session_completed", {})
            self.exam.clear_journal()

        self.screens["diagnostic_results"].load(diag)
        self._show_screen("diagnostic_results")

    def _compute_final_scores(self):
        """Aggregate scores across sections."""
        verbal_raw = 0
        quant_raw = 0
        verbal_band = "medium"
        quant_band = "medium"
        has_verbal = False
        has_quant = False

        for sec_type, section in self.exam.sections.items():
            measure = SECTION_META[sec_type][0]
            correctness = getattr(section, '_correctness', {})

            correct_count = sum(1 for v in correctness.values() if v)

            if measure == "verbal":
                has_verbal = True
                verbal_raw += correct_count
                if sec_type == SectionType.VERBAL_S2:
                    verbal_band = getattr(section, 'difficulty_band', 'medium')
            elif measure == "quant":
                has_quant = True
                quant_raw += correct_count
                if sec_type == SectionType.QUANT_S2:
                    quant_band = getattr(section, 'difficulty_band', 'medium')

        scores = self.scoring_engine.compute_session_scores(
            verbal_raw, verbal_band, quant_raw, quant_band
        )

        # Null out scores for sections not taken
        if not has_verbal:
            scores["verbal_estimated_low"] = None
            scores["verbal_estimated_high"] = None
        if not has_quant:
            scores["quant_estimated_low"] = None
            scores["quant_estimated_high"] = None

        # AWA score
        if hasattr(self, '_awa_score') and self._awa_score:
            scores["awa_estimated"] = self._awa_score.get("score_estimate")
        else:
            scores["awa_estimated"] = None

        # Save to DB
        if self.db_session:
            with db.atomic():
                ScoringResult.create(
                    session=self.db_session,
                    verbal_raw=verbal_raw,
                    quant_raw=quant_raw,
                    verbal_estimated_low=scores.get("verbal_estimated_low"),
                    verbal_estimated_high=scores.get("verbal_estimated_high"),
                    quant_estimated_low=scores.get("quant_estimated_low"),
                    quant_estimated_high=scores.get("quant_estimated_high"),
                    awa_estimated=scores.get("awa_estimated"),
                )
                self.db_session.state = "completed"
                self.db_session.save()

        return scores

    def _build_section_summaries(self):
        """Build section summary data for results screen."""
        summaries = []
        for sec_type in self.exam.section_order:
            section = self.exam.sections[sec_type]
            measure, sec_idx, time_limit, _ = SECTION_META[sec_type]
            correctness = getattr(section, '_correctness', {})
            correct = sum(1 for v in correctness.values() if v)
            total = section.total_questions

            summaries.append({
                "section_name": sec_type.value,
                "measure": measure,
                "difficulty_band": getattr(section, 'difficulty_band', 'medium'),
                "total_questions": total,
                "correct": correct,
                "accuracy": correct / total if total > 0 else 0,
                "time_used": section.time_used,
                "time_limit": time_limit,
            })
        return summaries

    def _build_question_details(self):
        """Build per-question detail data for results screen."""
        details = []
        for sec_type in self.exam.section_order:
            section = self.exam.sections[sec_type]
            measure = SECTION_META[sec_type][0]
            if measure == "awa":
                continue
            correctness = getattr(section, '_correctness', {})
            for qid in section.question_ids:
                q = self.question_bank.get_question(qid)
                details.append({
                    "question_id": qid,
                    "measure": measure,
                    "subtype": q["subtype"] if q else "unknown",
                    "difficulty": q["difficulty"] if q else 0,
                    "is_correct": correctness.get(qid),
                    "is_marked": qid in section.marked,
                    "time_spent": getattr(section, '_per_question_time', {}).get(qid, 0),
                })
        return details

    # ── Review ────────────────────────────────────────────────────────

    def _show_review(self):
        """Show review screen for current section."""
        section = self.exam.current_section
        if section:
            self.screens["review"].load_review(section.get_review_data())
            self._show_screen("review")

    def _goto_question(self, index):
        """Jump to a question from review."""
        section = self.exam.current_section
        if section:
            section.navigate_to(index)
            self.screens["question"]._load_question(index)
            self._show_screen("question")

    def _return_to_questions(self):
        """Return from review to question screen."""
        self._show_screen("question")

    # ── Navigation ────────────────────────────────────────────────────

    def _go_home(self):
        """Return to the Today tab (the canonical home)."""
        self.exam = None
        self.db_session = None
        self._on_sidebar_select("today")

    def _on_onboarding_skip(self):
        """User skipped the wizard — mark onboarded and land on Today."""
        try:
            from services.streak import mark_onboarding_complete
            mark_onboarding_complete()
        except Exception:
            pass
        self._on_sidebar_select("today")

    def _on_onboarding_finish(self, do_diagnostic: bool, params):
        """Onboarding complete. Persist goal + (optionally) start diagnostic.

        `params` is `{"target_score", "test_date", "hours_per_week"}` from
        Step 2, or None if the user skipped Step 2 directly.
        """
        try:
            from services.streak import mark_onboarding_complete
            mark_onboarding_complete()
        except Exception:
            pass

        if do_diagnostic:
            # Stash params so the post-diagnostic plan dialog can pre-fill.
            self._pending_plan_params = params
            self._on_take_diagnostic()
        else:
            self._on_sidebar_select("today")

    def _show_settings(self):
        """Show LLM settings dialog."""
        dlg = LLMSettingsDialog(self)
        dlg.ShowModal()
        dlg.Destroy()

    # ── New screen handlers ──────────────────────────────────────────

    def _on_take_diagnostic(self):
        """Launch the diagnostic test (verbal section + quant section)."""
        from services.diagnostic import assemble_diagnostic
        from models.exam_session import ExamSession, SectionType, SectionState

        qids = assemble_diagnostic()
        if not qids or len(qids) < 5:
            wx.MessageBox(
                "Not enough questions to run diagnostic. Build the question bank first.",
                "Insufficient Questions", wx.OK | wx.ICON_WARNING)
            return

        dlg = wx.MessageDialog(
            self,
            f"Diagnostic test: {len(qids)} questions across all topics.\n"
            "Untimed but recorded. After completion you'll see your weakness ranking "
            "and predicted score band.\n\nReady to start?",
            "Diagnostic Test",
            wx.OK | wx.CANCEL | wx.ICON_INFORMATION,
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        dlg.Destroy()

        from models.database import Question
        verbal_ids = [q.id for q in Question.select().where(
            (Question.id.in_(qids)) & (Question.measure == "verbal"))]
        quant_ids = [q.id for q in Question.select().where(
            (Question.id.in_(qids)) & (Question.measure == "quant"))]

        if not verbal_ids and not quant_ids:
            wx.MessageBox("No diagnostic questions available.", "Error",
                          wx.OK | wx.ICON_ERROR)
            return

        # Build a session with up to TWO sections so verbal questions live in
        # a verbal-typed container and quant in a quant-typed container.
        # `_compute_final_scores` derives measure from the section type, so
        # mixing the two would attribute all scores to a single measure.
        self.exam = ExamSession(test_type="custom", mode="learning")
        self.exam.section_order = []
        self.exam.sections = {}
        if verbal_ids:
            self.exam.section_order.append(SectionType.VERBAL_S1)
            self.exam.sections[SectionType.VERBAL_S1] = SectionState(
                section_type=SectionType.VERBAL_S1,
                question_ids=verbal_ids,
                time_limit=len(verbal_ids) * 120,
            )
        if quant_ids:
            self.exam.section_order.append(SectionType.QUANT_S1)
            self.exam.sections[SectionType.QUANT_S1] = SectionState(
                section_type=SectionType.QUANT_S1,
                question_ids=quant_ids,
                time_limit=len(quant_ids) * 120,
            )
        self.exam._question_bank = self.question_bank
        # Tag the exam so _finish_test knows to call grade_diagnostic at the
        # end and route to the diagnostic-results screen.
        self.exam._is_diagnostic = True
        self.exam._diagnostic_qids = list(verbal_ids) + list(quant_ids)

        db.connect(reuse_if_open=True)
        self.db_session = DBSession.create(
            test_type="custom",
            mode="learning",
            state="in_progress",
            started_at=datetime.now(),
            section_order=json.dumps([s.value for s in self.exam.section_order]),
        )
        self._show_section_instructions()

    def _on_start_vocab(self):
        """Launch the vocabulary flashcard session."""
        try:
            self.screens["vocab"].start_session(new_count=20)
            self._show_screen("vocab")
        except Exception as e:
            wx.MessageBox(f"Vocab session failed: {e}", "Error",
                          wx.OK | wx.ICON_ERROR)

    def _on_practice_topic(self, subtopic):
        """Start a 10-question drill on a specific subtopic."""
        from models.exam_session import ExamSession, SectionType, SECTION_META, SectionState
        from models.taxonomy import VERBAL_TAXONOMY

        # Determine measure from taxonomy (NOT from string heuristic)
        verbal_subs = set()
        for t, td in VERBAL_TAXONOMY.items():
            verbal_subs.update(td["subtopics"].keys())
        if subtopic in verbal_subs:
            measure = "verbal"
        else:
            measure = "quant"

        # Use smart drill selector — avoid recent repeats, prioritize unseen + wrong-before
        ids = self.question_bank.select_drill_smart(subtopic, count=10)

        if not ids:
            wx.MessageBox(
                f"No questions available for '{subtopic}' yet.\n"
                "Try a different subtopic or wait for the AI generation to fill this gap.",
                "No Questions",
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        # Build a topic drill exam
        self.exam = ExamSession(test_type="drill", mode="learning")
        sec_type = SectionType.VERBAL_S1 if measure == "verbal" else SectionType.QUANT_S1
        self.exam.section_order = [sec_type]
        self.exam.sections[sec_type] = SectionState(
            section_type=sec_type,
            question_ids=ids,
            time_limit=len(ids) * 90,
        )
        self.exam._question_bank = self.question_bank

        db.connect(reuse_if_open=True)
        self.db_session = DBSession.create(
            test_type="drill",
            mode="learning",
            state="in_progress",
            started_at=datetime.now(),
            section_order=json.dumps([sec_type.value]),
        )

        self._show_section_instructions()

    def _on_start_topic_drill(self, subtopic):
        self._on_practice_topic(subtopic)

    # ── Today-screen helpers ─────────────────────────────────────────

    def _on_start_quick_drill(self):
        """Build a 10-question mixed drill targeting the user's weak topics.

        Composition: 5 verbal + 5 quant. Each side draws from the user's
        weakest subtopics on that side; if a measure has no mastery
        signal, that side falls back to a random medium-difficulty pull
        from the live bank.
        """
        from models.exam_session import (
            ExamSession, SectionType, SectionState, SECTION_META,
        )
        from models.taxonomy import VERBAL_TAXONOMY, QUANT_TAXONOMY
        from services.mastery import weakness_ranking

        verbal_subs = {
            sub for td in VERBAL_TAXONOMY.values()
            for sub in td["subtopics"].keys()
        }
        quant_subs = {
            sub for td in QUANT_TAXONOMY.values()
            for sub in td["subtopics"].keys()
        }

        try:
            weak = weakness_ranking(limit=20)
        except Exception:
            weak = []

        weak_verbal = [s for (s, _, _) in weak if s in verbal_subs]
        weak_quant = [s for (s, _, _) in weak if s in quant_subs]

        per_measure = 5  # 50/50 split of the 10-question drill
        ids = []
        ids += self._draw_drill_ids("verbal", weak_verbal, per_measure)
        ids += self._draw_drill_ids("quant", weak_quant, per_measure)

        if not ids:
            wx.MessageBox("Not enough questions to start a drill.",
                          "Quick Drill", wx.OK | wx.ICON_INFORMATION)
            return

        # Shuffle so the section doesn't show 5 verbal then 5 quant in
        # blocks — interleaving keeps the drill feeling varied.
        import random
        random.shuffle(ids)

        # Pick the section type from the majority measure so per-section
        # scoring + the on-screen header label match what the user sees.
        from models.database import Question as _Q
        rows = list(_Q.select(_Q.id, _Q.measure).where(_Q.id.in_(ids)))
        n_quant = sum(1 for r in rows if r.measure == "quant")
        majority_quant = n_quant > (len(ids) - n_quant)
        sec_type = SectionType.QUANT_S1 if majority_quant else SectionType.VERBAL_S1

        self.exam = ExamSession(test_type="drill", mode="learning")
        self.exam.section_order = [sec_type]
        section_state = SectionState(
            section_type=sec_type, question_ids=ids,
            time_limit=len(ids) * 90)
        # Mixed verbal+quant — override the section header so the screen
        # shows "Quick Drill — Mixed" instead of "Verbal Reasoning" or
        # "Quantitative Reasoning". The per-question header in
        # question_screen.py also prepends the current question's
        # measure so the user always knows which side they're on.
        n_verbal = len(ids) - n_quant
        section_state.display_label = (
            f"Quick Drill — Mixed ({n_verbal} verbal, {n_quant} quant)"
        )
        self.exam.sections[sec_type] = section_state
        self.exam._question_bank = self.question_bank
        db.connect(reuse_if_open=True)
        self.db_session = DBSession.create(
            test_type="drill", mode="learning",
            state="in_progress", started_at=datetime.now(),
            section_order=json.dumps([sec_type.value]),
        )
        self._show_section_instructions()

    def _draw_drill_ids(self, measure: str, weak_subtopics: list, count: int):
        """Pull `count` question IDs for a measure, biasing toward weak
        subtopics when mastery data is available.

        Walks the weak list in order, calling select_drill_smart for
        each subtopic, until we have `count` ids or the list runs out;
        then tops up with a random medium-difficulty pull so a thin
        weak-subtopic catalog can't shrink the drill below `count`.
        """
        ids = []
        seen = set()
        for sub in weak_subtopics:
            if len(ids) >= count:
                break
            need = count - len(ids)
            picked = self.question_bank.select_drill_smart(sub, count=need)
            for qid in picked:
                if qid not in seen:
                    ids.append(qid)
                    seen.add(qid)
                    if len(ids) >= count:
                        break

        if len(ids) < count:
            top_up = self.question_bank.select_questions(
                measure=measure,
                count=count - len(ids),
                difficulty_band="medium",
                exclude_ids=list(seen),
            )
            ids.extend(top_up)
        return ids[:count]

    def _open_plan_dialog(self):
        """Open the StudyPlanDialog (the form survives in dashboard_screen.py)."""
        from services.diagnostic import get_latest_diagnostic
        from services.study_plan import generate_plan
        from datetime import timedelta as _td
        dlg = StudyPlanDialog(self)
        if dlg.ShowModal() == wx.ID_OK:
            params = dlg.get_params()
            try:
                test_date = datetime.now() + _td(weeks=params["weeks"])
                generate_plan(
                    target_score=params["target_score"],
                    test_date=test_date,
                    hours_per_week=params["hours_per_week"],
                    diagnostic=get_latest_diagnostic(),
                )
                wx.MessageBox("Study plan created!", "Success",
                              wx.OK | wx.ICON_INFORMATION)
                if "today" in self.screens:
                    self.screens["today"].refresh()
                if "insights" in self.screens:
                    self.screens["insights"].refresh()
            except Exception as e:
                wx.MessageBox(f"Failed to create plan: {e}", "Error",
                              wx.OK | wx.ICON_ERROR)
        dlg.Destroy()

    def _run_coach_now(self):
        """Manual mistake-coach trigger from the Insights screen."""
        from services.mistake_coach import analyze_mistakes
        import threading

        def _worker():
            try:
                report = analyze_mistakes()
            except Exception as e:
                wx.CallAfter(
                    wx.MessageBox,
                    f"Coach failed: {e}", "Mistake Coach",
                    wx.OK | wx.ICON_ERROR,
                )
                return
            wx.CallAfter(self._show_mistake_coach_report, report)

        threading.Thread(target=_worker, daemon=True).start()

    def _start_section_test(self, measure: str):
        """Start a 2-section adaptive test for a single measure."""
        if measure not in ("verbal", "quant"):
            return
        # Validate availability
        n = self.question_bank.get_question_count(measure)
        if n < 12:
            wx.MessageBox(
                f"Not enough {measure} questions ({n}; need at least 12).",
                "Insufficient questions", wx.OK | wx.ICON_WARNING)
            return
        self._start_test(measure, "simulation")

    def _open_tutor(self):
        """Open the AnswerChat dialog scoped to the user's last attempted Q.

        If no question has been attempted yet, scope it to a generic prompt.
        """
        from screens.answer_chat_screen import AnswerChatDialog
        from models.database import Response
        last_resp = (Response
                     .select()
                     .order_by(Response.created_at.desc())
                     .first())
        q_data = None
        if last_resp:
            try:
                q_data = self.question_bank.get_question(last_resp.question_id)
            except Exception:
                q_data = None
        if q_data is None:
            q_data = {
                "id": 0,
                "subtype": "mcq_single",
                "prompt": "(No recent question — ask anything about GRE prep.)",
                "options": [],
                "explanation": "",
                "stimulus": None,
            }
        dlg = AnswerChatDialog(self, q_data)
        dlg.ShowModal()
        dlg.Destroy()

    def _abort_test(self):
        """Abort the current test session and return to dashboard."""
        if self.exam:
            dlg = wx.MessageDialog(
                self,
                "Are you sure you want to abandon this test? Your progress will be lost.",
                "Abandon Test?",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            )
            if dlg.ShowModal() != wx.ID_YES:
                dlg.Destroy()
                return
            dlg.Destroy()
        self._mark_session_abandoned()
        self.exam = None
        self.db_session = None
        self._on_sidebar_select("today")

    def _mark_session_abandoned(self):
        """Stamp state='abandoned' on the active DB session and clear journal."""
        try:
            if self.db_session and self.db_session.state == "in_progress":
                self.db_session.state = "abandoned"
                self.db_session.ended_at = datetime.now()
                self.db_session.save()
        except Exception:
            pass
        try:
            if self.exam is not None:
                self.exam.log_event("session_abandoned", {})
                self.exam.clear_journal()
        except Exception:
            pass

    def _on_close(self, event):
        """Confirm close during active exam."""
        if self.exam and not self.exam.is_finished():
            dlg = wx.MessageDialog(
                self,
                "You have an active test in progress. Are you sure you want to quit?",
                "Confirm Exit",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            )
            if dlg.ShowModal() != wx.ID_YES:
                dlg.Destroy()
                return
            dlg.Destroy()
            self._mark_session_abandoned()

        db.close()
        self.Destroy()

    def _log_audit_summary_at_launch(self):
        """Run the data-corruption audit and log the summary to disk.

        Non-fatal — any exception is swallowed and logged. Result lives in
        the rotating logger at services/log.py so future bad imports show
        up in the same file the user already shares for debugging.
        """
        try:
            from services.log import get_logger
            from scripts.audit_data_corruption import audit_database
            corruption_found, report = audit_database(include_retired=False)
            log = get_logger("audit")
            log.info(
                "launch audit — total=%d verbal=%d quant=%d "
                "verbal_classifications=%s quant_issues=%s artifacts=%d",
                report.get("total_questions", 0),
                report.get("verbal_count", 0),
                report.get("quant_count", 0),
                report.get("verbal_classifications", {}),
                report.get("quant_issues", {}),
                len(report.get("llm_artifacts", [])),
            )
            if corruption_found:
                log.warning(
                    "launch audit found %d critical-corruption rows still live "
                    "(see scripts/audit_data_corruption.py for detail)",
                    len(report.get("worst_questions", [])),
                )
        except Exception as exc:  # pragma: no cover — diagnostics only
            try:
                from services.log import get_logger
                get_logger("audit").exception("launch audit failed: %s", exc)
            except Exception:
                pass

    def _recover_orphaned_state(self):
        """Handle leftover state from a previously-killed app run.

        Two pieces of state can outlive a crash:
        1. `Session` rows still in `state='in_progress'` because the user
           force-quit; mark them abandoned so progress queries are correct.
        2. `data/autosave_journal.jsonl` events from the killed test; archive
           with a timestamp so the user can inspect later, then clear so the
           next test starts with a fresh log.

        We don't attempt automatic in-app replay — full session reconstruction
        from event logs is out of scope. The journal acts as a forensic record.
        """
        from config import DATA_DIR
        # Mark stale sessions as abandoned.
        try:
            stale_q = (DBSession
                       .update(state="abandoned", ended_at=datetime.now())
                       .where(DBSession.state == "in_progress"))
            n_marked = stale_q.execute()
            if n_marked:
                from services.log import get_logger
                get_logger("main_frame").info(
                    "marked %d stale in-progress session(s) as abandoned", n_marked)
        except Exception:
            pass

        journal = DATA_DIR / "autosave_journal.jsonl"
        if not journal.exists():
            return
        try:
            size = journal.stat().st_size
        except OSError:
            return
        if size == 0:
            try:
                journal.unlink()
            except OSError:
                pass
            return

        # Archive with timestamp so the user can recover answers manually
        # if needed; clear the live journal so the next test starts clean.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = DATA_DIR / f"autosave_journal.{ts}.jsonl.bak"
        try:
            journal.rename(backup)
            wx.CallAfter(
                wx.MessageBox,
                f"Recovered autosave data from a previous test session was saved to:\n\n"
                f"  {backup.name}\n\n"
                f"Your in-progress responses are preserved in this file. The active "
                f"journal has been cleared so this session starts fresh.",
                "Previous Session Recovered",
                wx.OK | wx.ICON_INFORMATION,
            )
        except OSError:
            pass
