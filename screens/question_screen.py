"""
Question screen — unified screen for Verbal and Quantitative sections.
Handles all GRE question types with appropriate UI controls.
"""
import wx
import wx.html2

from widgets.timer import TimerWidget
from widgets.question_nav import QuestionNav
from widgets.numeric_entry import NumericEntry
from widgets.calculator import CalculatorWidget
from widgets.math_view import MathView


class QuestionScreen(wx.Panel):
    """
    Main question-answering screen for Verbal and Quant sections.
    Displays one question at a time with passage (if any), answer controls,
    timer, and navigation.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self._section_state = None
        self._question_bank = None
        self._current_q = None
        self._measure = None
        self._mode = "simulation"

        # Callbacks
        self._on_end_section = None
        self._on_time_expire = None
        self._on_exit_to_dashboard = None

        # Answer controls we create dynamically
        self._answer_controls = []
        self._numeric_entry = None
        self._calc_panel = None

        self._build_ui()

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Top bar ──────────────────────────────────────────────────
        top_bar = wx.BoxSizer(wx.HORIZONTAL)

        self.section_label = wx.StaticText(self, label="Section")
        self.section_label.SetFont(wx.Font(13, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                            wx.FONTWEIGHT_BOLD))
        top_bar.Add(self.section_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)

        self.question_label = wx.StaticText(self, label="Question 1 of 12")
        self.question_label.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                             wx.FONTWEIGHT_NORMAL))
        top_bar.Add(self.question_label, 1, wx.ALIGN_CENTER_VERTICAL)

        self.timer = TimerWidget(self)
        top_bar.Add(self.timer, 0, wx.ALIGN_CENTER_VERTICAL)

        main_sizer.Add(top_bar, 0, wx.EXPAND | wx.ALL, 8)
        main_sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # ── Content area (splitter: passage left, question+answers right) ─
        self.content_splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)

        # Left panel: passage/stimulus (hidden if no passage)
        self.passage_panel = wx.Panel(self.content_splitter)
        passage_sizer = wx.BoxSizer(wx.VERTICAL)
        passage_header = wx.StaticText(self.passage_panel, label="Passage")
        passage_header.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                        wx.FONTWEIGHT_BOLD))
        passage_sizer.Add(passage_header, 0, wx.ALL, 6)
        self.passage_view = MathView(self.passage_panel)
        passage_sizer.Add(self.passage_view, 1, wx.EXPAND | wx.ALL, 4)
        self.passage_panel.SetSizer(passage_sizer)

        # Right panel: question prompt + answers
        self.question_panel = wx.Panel(self.content_splitter)
        self.question_sizer = wx.BoxSizer(wx.VERTICAL)

        self.prompt_view = MathView(self.question_panel, size=(-1, 150))
        self.question_sizer.Add(self.prompt_view, 0, wx.EXPAND | wx.ALL, 4)

        # Subtype label
        self.subtype_label = wx.StaticText(self.question_panel, label="")
        self.subtype_label.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_ITALIC,
                                            wx.FONTWEIGHT_NORMAL))
        self.subtype_label.SetForegroundColour(wx.Colour(120, 120, 120))
        self.question_sizer.Add(self.subtype_label, 0, wx.LEFT | wx.BOTTOM, 8)

        # Answer area (dynamically populated)
        self.answer_panel = wx.ScrolledWindow(self.question_panel)
        self.answer_panel.SetScrollRate(0, 10)
        self.answer_sizer = wx.BoxSizer(wx.VERTICAL)
        self.answer_panel.SetSizer(self.answer_sizer)
        self.question_sizer.Add(self.answer_panel, 1, wx.EXPAND | wx.ALL, 4)

        self.question_panel.SetSizer(self.question_sizer)

        # Initially unsplit (will split if passage exists)
        self.content_splitter.Initialize(self.question_panel)
        main_sizer.Add(self.content_splitter, 1, wx.EXPAND)

        # ── Calculator toggle (Quant only) ────────────────────────────
        self._calc_panel = CalculatorWidget(self)
        self._calc_panel.Hide()
        main_sizer.Add(self._calc_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # ── Inline explanation panel (Learning mode + Show Answer) ────
        self._explanation_panel = MathView(self, size=(-1, 240))
        self._explanation_panel.Hide()
        main_sizer.Add(self._explanation_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self._explanation_visible = False

        # ── Bottom bar: navigation ────────────────────────────────────
        bottom_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.mark_btn = wx.Button(self, label="☆ Mark for Review")
        self.mark_btn.Bind(wx.EVT_BUTTON, self._on_mark)
        bottom_sizer.Add(self.mark_btn, 0, wx.ALL, 4)

        self.calc_btn = wx.Button(self, label="Calculator")
        self.calc_btn.Bind(wx.EVT_BUTTON, self._on_toggle_calc)
        bottom_sizer.Add(self.calc_btn, 0, wx.ALL, 4)

        # Learning mode: show answer button
        self.show_answer_btn = wx.Button(self, label="Show Answer")
        self.show_answer_btn.Bind(wx.EVT_BUTTON, self._on_show_answer)
        self.show_answer_btn.Hide()
        bottom_sizer.Add(self.show_answer_btn, 0, wx.ALL, 4)

        # Learning mode: ask AI tutor button (only shown after answer revealed)
        self.ask_tutor_btn = wx.Button(self, label="🤖 Ask AI Tutor")
        self.ask_tutor_btn.Bind(wx.EVT_BUTTON, self._on_ask_tutor)
        self.ask_tutor_btn.Hide()
        bottom_sizer.Add(self.ask_tutor_btn, 0, wx.ALL, 4)

        bottom_sizer.AddStretchSpacer()

        self.prev_btn = wx.Button(self, label="◀ Previous")
        self.prev_btn.Bind(wx.EVT_BUTTON, self._on_prev)
        bottom_sizer.Add(self.prev_btn, 0, wx.ALL, 4)

        self.next_btn = wx.Button(self, label="Next ▶")
        self.next_btn.Bind(wx.EVT_BUTTON, self._on_next)
        bottom_sizer.Add(self.next_btn, 0, wx.ALL, 4)

        self.review_btn = wx.Button(self, label="Review All")
        self.review_btn.Bind(wx.EVT_BUTTON, self._on_review)
        bottom_sizer.Add(self.review_btn, 0, wx.ALL, 4)

        self.end_btn = wx.Button(self, label="End Section")
        self.end_btn.Bind(wx.EVT_BUTTON, self._on_end_section_click)
        bottom_sizer.Add(self.end_btn, 0, wx.ALL, 4)

        self.exit_btn = wx.Button(self, label="Exit to Dashboard")
        self.exit_btn.Bind(wx.EVT_BUTTON, self._on_exit_clicked)
        bottom_sizer.Add(self.exit_btn, 0, wx.ALL, 4)

        main_sizer.Add(bottom_sizer, 0, wx.EXPAND | wx.ALL, 4)

        # ── Question nav grid ────────────────────────────────────────
        self.question_nav = QuestionNav(self, 0)
        self.question_nav.set_on_navigate(self._on_nav_jump)
        main_sizer.Add(self.question_nav, 0, wx.EXPAND | wx.ALL, 4)

        self.SetSizer(main_sizer)

    # ── Public API ────────────────────────────────────────────────────

    def configure(self, section_state, question_bank, measure, mode="simulation"):
        """Set up the screen for a section."""
        self._section_state = section_state
        self._question_bank = question_bank
        self._measure = measure
        self._mode = mode

        # Section label — extract numeric section index from SECTION_META
        labels = {
            "verbal": "Verbal Reasoning",
            "quant": "Quantitative Reasoning",
        }
        section_lbl = labels.get(measure, measure.title())
        sec_type = section_state.section_type
        from models.exam_session import SECTION_META
        _, sec_idx, _, _ = SECTION_META[sec_type]
        self.section_label.SetLabel(f"{section_lbl} — Section {sec_idx}")

        # Timer
        self.timer.set_time(section_state.time_limit)
        self.timer.set_on_expire(self._handle_time_expire)
        self.timer.set_on_tick(lambda elapsed: section_state.tick(elapsed))

        # Calculator visibility
        is_quant = measure == "quant"
        self.calc_btn.Show(is_quant)

        # Learning mode controls
        self.show_answer_btn.Show(mode == "learning")

        # Question nav
        self.question_nav.rebuild(section_state.total_questions)

        # Load first question
        self._load_question(0)
        self.Layout()

    def start_timer(self):
        self.timer.start()

    def set_on_end_section(self, callback):
        """callback()"""
        self._on_end_section = callback

    def set_on_time_expire(self, callback):
        self._on_time_expire = callback

    def set_on_review(self, callback):
        """callback()"""
        self._on_review_callback = callback

    def set_on_exit_to_dashboard(self, callback):
        """callback() — handler for exit-to-dashboard button"""
        self._on_exit_to_dashboard = callback

    # ── Question Loading ──────────────────────────────────────────────

    def _load_question(self, index):
        """Load and display question at the given index."""
        ss = self._section_state
        if not ss.navigate_to(index):
            return

        qid = ss.current_question_id
        if qid is None:
            return

        # Hide explanation panel when changing questions
        self._hide_explanation()
        q = self._question_bank.get_question(qid)
        if q is None:
            self.prompt_view.set_content(f"<p><i>Question {qid} not found.</i></p>")
            return

        self._current_q = q

        # Update header
        self.question_label.SetLabel(
            f"Question {index + 1} of {ss.total_questions}"
        )

        # Subtype display
        subtype_names = {
            "rc_single": "Reading Comprehension — Select One",
            "rc_multi": "Reading Comprehension — Select All That Apply",
            "rc_select_passage": "Reading Comprehension — Select in Passage",
            "tc": "Text Completion",
            "se": "Sentence Equivalence — Select Exactly Two",
            "qc": "Quantitative Comparison",
            "mcq_single": "Multiple Choice — Select One",
            "mcq_multi": "Multiple Choice — Select All That Apply",
            "numeric_entry": "Numeric Entry",
            "data_interp": "Data Interpretation",
        }
        self.subtype_label.SetLabel(subtype_names.get(q["subtype"], q["subtype"]))

        # Passage / stimulus
        if q.get("stimulus"):
            self.passage_view.set_content(q["stimulus"]["content"])
            self.passage_panel.Show()
            if not self.content_splitter.IsSplit():
                self.content_splitter.SplitVertically(
                    self.passage_panel, self.question_panel, 420)
                self.content_splitter.SetMinimumPaneSize(200)
        else:
            # Always clear stale content before unsplitting (macOS WebView caches)
            self.passage_view.set_content("")
            if self.content_splitter.IsSplit():
                self.content_splitter.Unsplit(self.passage_panel)
            self.passage_panel.Hide()

        # Prompt
        self.prompt_view.set_content(f'<div class="prompt">{q["prompt"]}</div>')

        # Mark button state
        if qid in ss.marked:
            self.mark_btn.SetLabel("★ Unmark")
        else:
            self.mark_btn.SetLabel("☆ Mark for Review")

        # Build answer controls
        self._build_answer_controls(q)

        # Restore saved response
        saved = ss.get_response(qid)
        if saved:
            self._restore_response(saved)

        # Update nav
        self._update_nav()
        self.Layout()

    def _build_answer_controls(self, q):
        """Create appropriate answer controls based on question subtype."""
        # Clear old controls
        self.answer_sizer.Clear(True)
        self._answer_controls = []
        self._numeric_entry = None

        subtype = q["subtype"]
        options = q.get("options", [])

        if subtype in ("rc_single", "mcq_single", "qc", "data_interp", "rc_select_passage"):
            # Radio buttons for single-select
            for opt in options:
                radio = wx.RadioButton(
                    self.answer_panel,
                    label=f"{opt['label']}) {opt['text']}",
                    style=wx.RB_GROUP if opt == options[0] else 0,
                )
                radio.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                       wx.FONTWEIGHT_NORMAL))
                radio.Bind(wx.EVT_RADIOBUTTON, self._on_answer_change)
                self.answer_sizer.Add(radio, 0, wx.ALL, 6)
                self._answer_controls.append(("radio", opt["label"], radio))

        elif subtype in ("rc_multi", "mcq_multi", "se"):
            # Checkboxes for multi-select
            if subtype == "se":
                hint = wx.StaticText(self.answer_panel,
                                      label="Select exactly two answer choices.")
                hint.SetForegroundColour(wx.Colour(0, 100, 180))
                self.answer_sizer.Add(hint, 0, wx.LEFT | wx.BOTTOM, 6)
            elif subtype in ("rc_multi", "mcq_multi"):
                hint = wx.StaticText(self.answer_panel,
                                      label="Select all that apply.")
                hint.SetForegroundColour(wx.Colour(0, 100, 180))
                self.answer_sizer.Add(hint, 0, wx.LEFT | wx.BOTTOM, 6)

            for opt in options:
                cb = wx.CheckBox(self.answer_panel,
                                  label=f"{opt['label']}) {opt['text']}")
                cb.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                    wx.FONTWEIGHT_NORMAL))
                cb.Bind(wx.EVT_CHECKBOX, self._on_answer_change)
                self.answer_sizer.Add(cb, 0, wx.ALL, 6)
                self._answer_controls.append(("check", opt["label"], cb))

        elif subtype == "tc":
            # Text completion: group options by blank
            blanks = {}
            for opt in options:
                parts = opt["label"].split("_", 1)
                if len(parts) == 2:
                    blank = parts[0]
                    choice = parts[1]
                else:
                    blank = "blank1"
                    choice = opt["label"]
                blanks.setdefault(blank, []).append((choice, opt["text"]))

            for blank_name, choices in sorted(blanks.items()):
                label = wx.StaticText(self.answer_panel,
                                       label=f"  {blank_name.replace('blank', 'Blank ')}:")
                label.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                       wx.FONTWEIGHT_BOLD))
                self.answer_sizer.Add(label, 0, wx.LEFT | wx.TOP, 6)

                for i, (choice_label, choice_text) in enumerate(choices):
                    radio = wx.RadioButton(
                        self.answer_panel,
                        label=f"  {choice_label}) {choice_text}",
                        style=wx.RB_GROUP if i == 0 else 0,
                    )
                    radio.Bind(wx.EVT_RADIOBUTTON, self._on_answer_change)
                    self.answer_sizer.Add(radio, 0, wx.ALL, 4)
                    self._answer_controls.append(("tc_radio", blank_name, choice_label, radio))

        elif subtype == "numeric_entry":
            # Numeric entry
            is_fraction = (q.get("numeric_answer") and
                          q["numeric_answer"].get("numerator") is not None)
            self._numeric_entry = NumericEntry(self.answer_panel,
                                                fraction_mode=is_fraction)
            self._numeric_entry.set_on_change(lambda _: self._on_answer_change(None))
            self.answer_sizer.Add(self._numeric_entry, 0, wx.ALL, 8)

        self.answer_panel.FitInside()
        self.answer_panel.Layout()

    def _get_current_response(self):
        """Build response dict from current answer controls."""
        if self._current_q is None:
            return {}

        subtype = self._current_q["subtype"]

        if subtype == "rc_select_passage":
            for ctrl_type, label, ctrl in self._answer_controls:
                if ctrl.GetValue():
                    return {"selected_sentence": label}
            return {}

        elif subtype in ("rc_single", "mcq_single", "qc", "data_interp"):
            for ctrl_type, label, ctrl in self._answer_controls:
                if ctrl.GetValue():
                    return {"selected": [label]}
            return {}

        elif subtype in ("rc_multi", "mcq_multi", "se"):
            selected = [label for ct, label, ctrl in self._answer_controls
                       if ctrl.GetValue()]
            return {"selected": selected} if selected else {}

        elif subtype == "tc":
            selected = {}
            for entry in self._answer_controls:
                if len(entry) == 4:
                    _, blank, choice, ctrl = entry
                    if ctrl.GetValue():
                        selected[blank] = choice
            return {"selected": selected} if selected else {}

        elif subtype == "numeric_entry" and self._numeric_entry:
            return self._numeric_entry.get_response()

        return {}

    def _restore_response(self, saved):
        """Restore saved response to controls."""
        if not saved or self._current_q is None:
            return

        subtype = self._current_q["subtype"]

        if subtype == "rc_select_passage":
            sel = saved.get("selected_sentence")
            for ct, label, ctrl in self._answer_controls:
                ctrl.SetValue(label == sel)

        elif subtype in ("rc_single", "mcq_single", "qc", "data_interp"):
            sel = saved.get("selected", [])
            for ct, label, ctrl in self._answer_controls:
                ctrl.SetValue(label in sel)

        elif subtype in ("rc_multi", "mcq_multi", "se"):
            sel = set(saved.get("selected", []))
            for ct, label, ctrl in self._answer_controls:
                ctrl.SetValue(label in sel)

        elif subtype == "tc":
            sel = saved.get("selected", {})
            for entry in self._answer_controls:
                if len(entry) == 4:
                    _, blank, choice, ctrl = entry
                    ctrl.SetValue(sel.get(blank) == choice)

        elif subtype == "numeric_entry" and self._numeric_entry:
            self._numeric_entry.set_response(saved)

    # ── Event handlers ────────────────────────────────────────────────

    def _on_answer_change(self, event):
        """Save current answer to section state."""
        ss = self._section_state
        if ss is None:
            return
        qid = ss.current_question_id
        response = self._get_current_response()
        ss.set_response(qid, response)
        self._update_nav()

    def _on_mark(self, event):
        ss = self._section_state
        if ss:
            ss.toggle_mark()
            qid = ss.current_question_id
            if qid in ss.marked:
                self.mark_btn.SetLabel("★ Unmark")
            else:
                self.mark_btn.SetLabel("☆ Mark for Review")
            self._update_nav()

    def _on_toggle_calc(self, event):
        self._calc_panel.Show(not self._calc_panel.IsShown())
        self.Layout()

    def _on_show_answer(self, event):
        """Learning mode: toggle inline explanation panel."""
        if self._current_q is None:
            return

        # Toggle off if already showing
        if self._explanation_visible:
            self._hide_explanation()
            return

        # Build correct answer text
        options = self._current_q.get("options", [])
        correct_parts = []
        for o in options:
            if o.get("is_correct"):
                # Strip blank prefix for display (blank1_A → A)
                label = o["label"].split("_")[-1] if "_" in o["label"] else o["label"]
                text = o.get("text", "")
                if text:
                    correct_parts.append(f"{label}) {text}")
                else:
                    correct_parts.append(label)
        na = self._current_q.get("numeric_answer")
        if na:
            if na.get("exact_value") is not None:
                correct_parts.append(str(na["exact_value"]))
            elif na.get("numerator") is not None:
                correct_parts.append(f"{na['numerator']}/{na['denominator']}")

        correct_html = " &nbsp; • &nbsp; ".join(self._escape_html(p) for p in correct_parts)

        # Build explanation HTML
        explanation = self._current_q.get("explanation", "")
        explanation_html = self._format_explanation_html(explanation)

        html = f"""
            <div class="answer-correct">
                <strong>Correct Answer:</strong> {correct_html}
            </div>
            {explanation_html}
        """

        self._explanation_panel.set_content(html)
        self._explanation_panel.Show()
        self._explanation_visible = True
        self.show_answer_btn.SetLabel("Hide Answer")
        # Show the AI Tutor button now that the answer is revealed
        if self._mode == "learning":
            self.ask_tutor_btn.Show()
        self.Layout()

    def _hide_explanation(self):
        """Hide the inline explanation panel."""
        if self._explanation_visible:
            self._explanation_panel.Hide()
            self._explanation_visible = False
            self.show_answer_btn.SetLabel("Show Answer")
            self.ask_tutor_btn.Hide()
            self.Layout()

    def _on_ask_tutor(self, _):
        """Open the AI Tutor chat dialog scoped to this question."""
        if not self._current_q:
            return
        # Get current user response if any
        from screens.answer_chat_screen import AnswerChatDialog
        user_resp = self._get_current_response()
        dlg = AnswerChatDialog(self, self._current_q, user_response=user_resp)
        dlg.ShowModal()
        dlg.Destroy()

    @staticmethod
    def _escape_html(text):
        return (str(text)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))

    def _format_explanation_html(self, explanation):
        """Convert plain-text explanation into clean HTML paragraphs."""
        if not explanation or not explanation.strip():
            return ""
        # Split on blank lines for paragraphs; preserve LaTeX intact
        paragraphs = [p.strip() for p in explanation.split("\n\n") if p.strip()]
        if not paragraphs:
            paragraphs = [explanation.strip()]
        # Each paragraph: replace single newlines with spaces, escape HTML
        body = "".join(
            f"<p>{self._escape_html(p).replace(chr(10), ' ')}</p>"
            for p in paragraphs
        )
        return f'<div class="explanation"><h3>Explanation</h3>{body}</div>'

    def _on_prev(self, event):
        ss = self._section_state
        if ss and ss.current_index > 0:
            self._load_question(ss.current_index - 1)

    def _on_next(self, event):
        ss = self._section_state
        if ss and ss.current_index < ss.total_questions - 1:
            self._load_question(ss.current_index + 1)

    def _on_nav_jump(self, index):
        self._load_question(index)

    def _on_review(self, event):
        if hasattr(self, '_on_review_callback') and self._on_review_callback:
            self._on_review_callback()

    def _on_end_section_click(self, event):
        ss = self._section_state
        if ss is None:
            return
        unanswered = ss.total_questions - ss.count_answered()
        if unanswered > 0:
            dlg = wx.MessageDialog(
                self,
                f"You have {unanswered} unanswered question(s). End section anyway?",
                "Confirm End Section",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            )
            if dlg.ShowModal() != wx.ID_YES:
                dlg.Destroy()
                return
            dlg.Destroy()
        self.timer.stop()
        if self._on_end_section:
            self._on_end_section()

    def _on_exit_clicked(self, event):
        """Exit to dashboard, abandoning the test session."""
        dlg = wx.MessageDialog(
            self,
            "Exit to dashboard? Your test progress will be lost.",
            "Exit Test?",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        if dlg.ShowModal() == wx.ID_YES:
            dlg.Destroy()
            self.timer.stop()
            if self._on_exit_to_dashboard:
                self._on_exit_to_dashboard()
        else:
            dlg.Destroy()

    def _handle_time_expire(self):
        self.timer.stop()
        wx.MessageBox("Time is up! Moving to the next section.",
                       "Time Expired", wx.OK | wx.ICON_INFORMATION, self)
        if self._on_time_expire:
            self._on_time_expire()
        elif self._on_end_section:
            self._on_end_section()

    def _update_nav(self):
        """Update the question navigation grid."""
        ss = self._section_state
        if ss is None:
            return
        answered_indices = set()
        marked_indices = set()
        for i, qid in enumerate(ss.question_ids):
            resp = ss.get_response(qid)
            if resp and resp != {}:
                answered_indices.add(i)
            if qid in ss.marked:
                marked_indices.add(i)
        self.question_nav.set_state(ss.current_index, answered_indices, marked_indices)

        # Update nav button states
        self.prev_btn.Enable(ss.current_index > 0)
        self.next_btn.Enable(ss.current_index < ss.total_questions - 1)
