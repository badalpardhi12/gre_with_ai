"""
Main application frame — orchestrates screen switching, exam flow, and scoring.
"""
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

from screens.welcome_screen import WelcomeScreen
from screens.instructions_screen import InstructionsScreen
from screens.awa_screen import AWAScreen
from screens.question_screen import QuestionScreen
from screens.review_screen import ReviewScreen
from screens.results_screen import ResultsScreen
from screens.progress_screen import ProgressScreen
from screens.llm_settings import LLMSettingsDialog


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

        # Exam state
        self.exam = None
        self.db_session = None

        # ── Create all screen panels ──────────────────────────────────
        self.panel_container = wx.Panel(self)
        self.main_sizer = wx.BoxSizer(wx.VERTICAL)

        self.screens = {}
        self._create_screens()

        self.panel_container.SetSizer(self.main_sizer)

        # Start on welcome screen
        self._show_screen("welcome")

        # Close confirmation
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _create_screens(self):
        """Instantiate all screen panels (hidden by default)."""
        screen_classes = {
            "welcome": WelcomeScreen,
            "instructions": InstructionsScreen,
            "awa": AWAScreen,
            "question": QuestionScreen,
            "review": ReviewScreen,
            "results": ResultsScreen,
            "progress": ProgressScreen,
        }

        for name, cls in screen_classes.items():
            panel = cls(self.panel_container)
            panel.Hide()
            self.main_sizer.Add(panel, 1, wx.EXPAND)
            self.screens[name] = panel

        # Wire callbacks
        self.screens["welcome"].set_on_start(self._start_test)
        self.screens["welcome"].set_on_settings(self._show_settings)
        self.screens["welcome"].set_on_progress(self._show_progress)
        self.screens["instructions"].set_on_begin(self._begin_section)
        self.screens["awa"].set_on_submit(self._submit_awa)
        self.screens["awa"].set_on_time_expire(self._awa_time_expired)
        self.screens["question"].set_on_end_section(self._end_current_section)
        self.screens["question"].set_on_time_expire(self._end_current_section)
        self.screens["question"].set_on_review(self._show_review)
        self.screens["review"].set_on_goto(self._goto_question)
        self.screens["review"].set_on_return(self._return_to_questions)
        self.screens["review"].set_on_end_section(self._end_current_section)
        self.screens["results"].set_on_home(self._go_home)
        self.screens["progress"].set_on_home(self._go_home)

        # Update welcome screen info
        v_count = self.question_bank.get_question_count("verbal")
        q_count = self.question_bank.get_question_count("quant")
        self.screens["welcome"].set_info(
            f"Question bank: {v_count} verbal, {q_count} quant questions loaded"
        )

    def _show_screen(self, name):
        """Show one screen, hide all others."""
        for sname, panel in self.screens.items():
            panel.Show(sname == name)
        self.panel_container.Layout()

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

        # Create DB session
        db.connect(reuse_if_open=True)
        self.db_session = DBSession.create(
            test_type=test_type,
            mode=mode,
            section_order=str([s.value for s in self.exam.section_order]),
        )

        # Show instructions for first section
        self._show_section_instructions()

    def _show_section_instructions(self):
        """Show instructions for the current section."""
        sec_type = self.exam.current_section_type
        if sec_type is None:
            self._finish_test()
            return

        self.screens["instructions"].set_section(sec_type)
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

        self.screens["awa"].load_prompt(prompt_data)
        self.screens["awa"].start_timer()
        self._show_screen("awa")

    def _start_question_section(self, sec_type, measure):
        """Start a Verbal or Quant section."""
        section = self.exam.current_section
        self.screens["question"].configure(
            section, self.question_bank, measure, self.exam.mode)
        self.screens["question"].start_timer()
        self._show_screen("question")

    def _submit_awa(self, essay_text, word_count):
        """Handle AWA essay submission."""
        section = self.exam.current_section
        section.finish()

        # Save to DB
        if self.db_session and section.question_ids:
            prompt_id = section.question_ids[0]
            AWASubmission.create(
                session=self.db_session,
                prompt=prompt_id,
                essay_text=essay_text,
                word_count=word_count,
            )

        # Score async (non-blocking)
        prompt_data = self.question_bank.get_awa_prompt(section.question_ids[0]) if section.question_ids else {}
        prompt_text = prompt_data.get("prompt_text", "") if prompt_data else ""

        self._awa_essay = essay_text
        self._awa_prompt_text = prompt_text
        self._awa_score = None

        scorer = AWAScoringService(llm_service)
        scorer.score_essay_async(
            essay_text, prompt_text,
            lambda result, err: wx.CallAfter(self._on_awa_scored, result, err)
        )

        # Move to next section immediately (scoring happens in background)
        self._advance_to_next()

    def _on_awa_scored(self, result, error):
        """Callback when AWA scoring completes."""
        if error:
            self._awa_score = {"score_estimate": None, "error": str(error)}
        else:
            self._awa_score = result

        # Save to DB
        if self.db_session and result and result.get("score_estimate") is not None:
            sub = AWASubmission.select().where(
                AWASubmission.session == self.db_session
            ).first()
            if sub:
                import json
                AWAResult.create(
                    submission=sub,
                    score_estimate=result["score_estimate"],
                    score_confidence_low=result.get("score_confidence_low", 0),
                    score_confidence_high=result.get("score_confidence_high", 0),
                    rubric_json=json.dumps(result.get("dimensions", {})),
                    feedback_text=result.get("summary", ""),
                    model_used="openrouter",
                )

    def _awa_time_expired(self):
        """Auto-submit AWA when time runs out."""
        essay = self.screens["awa"].get_essay()
        wc = self.screens["awa"].get_word_count()
        self._submit_awa(essay, wc)

    def _end_current_section(self):
        """End the current V/Q section, score it, persist results, and advance."""
        section = self.exam.current_section
        if section is None:
            return

        section.finish()

        # Score questions
        for qid in section.question_ids:
            resp = section.get_response(qid)
            if resp and resp != {}:
                q_data = self.question_bank.get_question(qid)
                if q_data:
                    is_correct = self.scoring_engine.check_answer(q_data, resp)
                    if not hasattr(section, '_correctness'):
                        section._correctness = {}
                    section._correctness[qid] = is_correct

        # Persist SectionResult and Response rows
        sec_type = self.exam.current_section_type
        if self.db_session and sec_type:
            import json as _json
            measure, sec_idx, time_limit, _ = SECTION_META[sec_type]
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
                question_ids=_json.dumps(section.question_ids),
            )
            correctness = getattr(section, '_correctness', {})
            per_q_time = getattr(section, '_per_question_time', {})
            for qid in section.question_ids:
                resp = section.get_response(qid)
                Response.create(
                    session=self.db_session,
                    section_result=sr,
                    question=qid,
                    response_payload=_json.dumps(resp) if resp else "{}",
                    is_marked=qid in section.marked,
                    is_correct=correctness.get(qid),
                    time_spent_seconds=per_q_time.get(qid, 0),
                )

        # Section-level adaptation
        self.exam.end_current_section()

        self._advance_to_next()

    def _advance_to_next(self):
        """Move to the next section or finish."""
        has_more = self.exam.advance_section()
        if has_more:
            self._show_section_instructions()
        else:
            self._finish_test()

    def _finish_test(self):
        """Compute final scores and show results."""
        scores = self._compute_final_scores()
        section_summaries = self._build_section_summaries()
        question_details = self._build_question_details()

        self.screens["results"].load_results(scores, section_summaries, question_details)
        self._show_screen("results")

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
        """Return to welcome screen."""
        self.exam = None
        self.db_session = None
        # Refresh question counts
        v_count = self.question_bank.get_question_count("verbal")
        q_count = self.question_bank.get_question_count("quant")
        self.screens["welcome"].set_info(
            f"Question bank: {v_count} verbal, {q_count} quant questions loaded"
        )
        self._show_screen("welcome")

    def _show_settings(self):
        """Show LLM settings dialog."""
        dlg = LLMSettingsDialog(self)
        dlg.ShowModal()
        dlg.Destroy()

    def _show_progress(self):
        """Show the progress dashboard."""
        self.screens["progress"].load_data()
        self._show_screen("progress")

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

        db.close()
        self.Destroy()
