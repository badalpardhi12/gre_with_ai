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
from widgets.theme import Color


class QuestionScreen(wx.Panel):
    """
    Main question-answering screen for Verbal and Quant sections.
    Displays one question at a time with passage (if any), answer controls,
    timer, and navigation.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)
        self._section_state = None
        self._question_bank = None
        self._exam = None
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
        # Wrap-on-resize: long option text overflows the panel on narrow
        # widths (e.g. when the user drags the passage/question splitter
        # left). Re-wrap every option's StaticText whenever the answer
        # panel's width changes.
        self._option_texts = []
        self.answer_panel.Bind(wx.EVT_SIZE, self._on_answer_panel_resize)

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

        # User report button — always available so a learner can flag a
        # broken question whether they spot it before or after answering.
        self.report_btn = wx.Button(self, label="🚩 Report")
        self.report_btn.SetToolTip(
            "Report a wrong answer, mismatched explanation, or unanswerable question"
        )
        self.report_btn.Bind(wx.EVT_BUTTON, self._on_report_question)
        bottom_sizer.Add(self.report_btn, 0, wx.ALL, 4)

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

    def configure(self, section_state, question_bank, measure, mode="simulation",
                  exam=None):
        """Set up the screen for a section.

        `exam` is the parent ExamSession; passed in so per-question events
        can be logged to the autosave journal for crash-recovery.
        """
        self._section_state = section_state
        self._question_bank = question_bank
        self._exam = exam
        self._measure = measure
        self._mode = mode

        # Section label — extract numeric section index from SECTION_META.
        # SectionState may carry a `display_label` override (set by mixed
        # drills like Quick Drill where one section spans both measures);
        # honour it so the header doesn't lie about the section's type.
        labels = {
            "verbal": "Verbal Reasoning",
            "quant": "Quantitative Reasoning",
        }
        sec_type = section_state.section_type
        from models.exam_session import SECTION_META
        _, sec_idx, _, _ = SECTION_META[sec_type]
        if getattr(section_state, "display_label", None):
            self.section_label.SetLabel(section_state.display_label)
        else:
            section_lbl = labels.get(measure, measure.title())
            self.section_label.SetLabel(f"{section_lbl} — Section {sec_idx}")

        # Timer
        self.timer.set_time(section_state.time_limit)
        self.timer.set_on_expire(self._handle_time_expire)
        self.timer.set_on_tick(lambda elapsed: section_state.tick(elapsed))

        # Calculator visibility — defaults to the section's measure;
        # mixed drills override per question in _load_question.
        is_quant = measure == "quant"
        self.calc_btn.Show(is_quant)
        # Track whether this section mixes measures; cheaper to compute
        # once than to re-detect on every navigation.
        self._mixed_section = bool(getattr(section_state, "display_label", None))

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

        # In a mixed-measure section (Quick Drill) toggle calc-button
        # visibility per question and prepend the measure to the
        # question label so the user always knows which side they're on.
        if getattr(self, "_mixed_section", False):
            q_measure = (q.get("measure") or "").lower()
            self.calc_btn.Show(q_measure == "quant")
            measure_tag = "Verbal" if q_measure == "verbal" else (
                "Quant" if q_measure == "quant" else q_measure.title()
            )
            self.question_label.SetLabel(
                f"{measure_tag} • Question {index + 1} of {ss.total_questions}"
            )
            self.Layout()
        else:
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
                # Sash at 50% of current width — adapts to whatever
                # window size the user is on. Gravity 0.5 keeps the
                # split balanced when the window resizes (without it
                # the sash sticks to the left, starving the question).
                w = max(800, self.content_splitter.GetClientSize().width)
                self.content_splitter.SplitVertically(
                    self.passage_panel, self.question_panel, w // 2)
                self.content_splitter.SetMinimumPaneSize(220)
                self.content_splitter.SetSashGravity(0.5)
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
        # Reset the per-option StaticText list so the resize handler
        # only re-wraps the live question's options.
        self._option_texts = []

        subtype = q["subtype"]
        options = q.get("options", [])

        if subtype in ("rc_single", "mcq_single", "qc", "data_interp", "rc_select_passage"):
            # Radio buttons for single-select — split control + wrappable
            # text so long option labels don't get clipped at the panel
            # boundary on narrow layouts.
            for opt in options:
                radio = self._add_wrapping_option(
                    label_text=f"{opt['label']}) {opt['text']}",
                    control_type="radio",
                    is_first=(opt is options[0]),
                    on_change=self._on_answer_change,
                )
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
                cb = self._add_wrapping_option(
                    label_text=f"{opt['label']}) {opt['text']}",
                    control_type="check",
                    on_change=self._on_answer_change,
                )
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
                    radio = self._add_wrapping_option(
                        label_text=f"  {choice_label}) {choice_text}",
                        control_type="radio",
                        is_first=(i == 0),
                        on_change=self._on_answer_change,
                    )
                    self._answer_controls.append(
                        ("tc_radio", blank_name, choice_label, radio)
                    )

        elif subtype == "numeric_entry":
            # Numeric entry: prefer the explicit `mode` field added in PR 1
            # (NumericAnswer.mode = 'decimal' | 'fraction' | 'auto'). For
            # legacy 'auto' rows, fall back to the old "has numerator?" heuristic.
            na = q.get("numeric_answer") or {}
            mode = na.get("mode") or "auto"
            if mode == "fraction":
                is_fraction = True
            elif mode == "decimal":
                is_fraction = False
            else:
                is_fraction = na.get("numerator") is not None
            self._numeric_entry = NumericEntry(self.answer_panel,
                                                fraction_mode=is_fraction)
            self._numeric_entry.set_on_change(lambda _: self._on_answer_change(None))
            self.answer_sizer.Add(self._numeric_entry, 0, wx.ALL, 8)

        # Initial wrap pass — answer_panel may already have a stable
        # width by now (subsequent EVT_SIZE events will re-wrap on
        # splitter drags).
        self._rewrap_options()
        self.answer_panel.FitInside()
        self.answer_panel.Layout()

    def _add_wrapping_option(self, label_text: str, control_type: str,
                              on_change, is_first: bool = False):
        """Build a row with a small radio/checkbox + a wrappable text
        label so long option strings flow over multiple lines instead
        of being clipped by the panel boundary.

        Clicking anywhere on the text also activates the control —
        matches the official ETS interface where the option's text is a
        click target.

        Returns the inner control (RadioButton / CheckBox) so callers
        can `GetValue()` and bind events as before.
        """
        row = wx.BoxSizer(wx.HORIZONTAL)

        if control_type == "radio":
            style = wx.RB_GROUP if is_first else 0
            ctrl = wx.RadioButton(self.answer_panel, label="", style=style)
            ctrl.Bind(wx.EVT_RADIOBUTTON, on_change)
        else:
            ctrl = wx.CheckBox(self.answer_panel, label="")
            ctrl.Bind(wx.EVT_CHECKBOX, on_change)

        row.Add(ctrl, 0, wx.RIGHT | wx.ALIGN_TOP, 6)

        text = wx.StaticText(self.answer_panel, label=label_text)
        text.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                              wx.FONTWEIGHT_NORMAL))
        text.Bind(wx.EVT_LEFT_DOWN,
                  lambda evt, c=ctrl: self._toggle_from_text(c, evt))
        row.Add(text, 1, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)

        self.answer_sizer.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        self._option_texts.append(text)
        return ctrl

    def _toggle_from_text(self, ctrl, _evt):
        """Click on option text → activate the control + fire its event."""
        if isinstance(ctrl, wx.RadioButton):
            ctrl.SetValue(True)
            new_evt = wx.PyCommandEvent(wx.EVT_RADIOBUTTON.typeId, ctrl.GetId())
            new_evt.SetEventObject(ctrl)
            wx.PostEvent(ctrl, new_evt)
        elif isinstance(ctrl, wx.CheckBox):
            ctrl.SetValue(not ctrl.GetValue())
            new_evt = wx.PyCommandEvent(wx.EVT_CHECKBOX.typeId, ctrl.GetId())
            new_evt.SetEventObject(ctrl)
            wx.PostEvent(ctrl, new_evt)

    def _on_answer_panel_resize(self, event):
        """Re-wrap option text whenever the answer panel resizes
        (window resize, splitter drag, sidebar toggle)."""
        self._rewrap_options()
        event.Skip()

    def _rewrap_options(self):
        """Wrap every option's StaticText to fit the current panel width."""
        if not self._option_texts:
            return
        # Subtract padding (radio width + horizontal margins) so wrap
        # doesn't push past the panel edge.
        avail = self.answer_panel.GetClientSize().width - 56
        if avail < 80:
            return  # too narrow to lay out anything sensibly
        for t in self._option_texts:
            if t:
                t.Wrap(avail)
        self.answer_sizer.Layout()
        self.answer_panel.FitInside()

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
        # Crash-durable autosave so a force-quit mid-test can be replayed.
        if self._exam is not None:
            self._exam.log_event("answer_changed",
                                 {"qid": qid, "response": response})
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

        # Stored explanation (preferred). If missing, fire a one-shot LLM call
        # to generate one and cache it back to the DB.
        explanation = self._current_q.get("explanation", "")
        if not explanation or not explanation.strip():
            explanation = "Generating explanation…"
            wx.CallAfter(self._fetch_explanation_async)
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

    def _fetch_explanation_async(self):
        """Background-generate an explanation if the question has none."""
        if self._current_q is None:
            return
        from services.explanation import ExplanationService
        ExplanationService().get_explanation_async(
            self._current_q,
            user_response=self._get_current_response() if self._section_state else None,
            callback=self._on_explanation_ready,
        )

    def _on_explanation_ready(self, text, error):
        """Render the just-generated explanation and persist it back."""
        if not self._explanation_visible or self._current_q is None:
            return
        if error or not text:
            text = ("(Explanation could not be generated — "
                    f"{error if error else 'empty response'}.)")
        else:
            # Cache for future opens.
            try:
                from services.explanation import ExplanationService
                ExplanationService().save_explanation(self._current_q["id"], text)
                self._current_q["explanation"] = text
            except Exception:
                pass

        options = self._current_q.get("options", [])
        correct_parts = []
        for o in options:
            if o.get("is_correct"):
                label = o["label"].split("_")[-1] if "_" in o["label"] else o["label"]
                t = o.get("text", "")
                correct_parts.append(f"{label}) {t}" if t else label)
        correct_html = " &nbsp; • &nbsp; ".join(self._escape_html(p) for p in correct_parts)
        explanation_html = self._format_explanation_html(text)
        html = f"""
            <div class="answer-correct">
                <strong>Correct Answer:</strong> {correct_html}
            </div>
            {explanation_html}
        """
        self._explanation_panel.set_content(html)
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

    def _on_report_question(self, _):
        """Open a small dialog to report a problem with this question."""
        if not self._current_q:
            return
        qid = self._current_q.get("id")
        if qid is None:
            return
        from widgets.flag_dialog import FlagQuestionDialog
        from services.question_bank import (
            flag_question, auto_retire_flagged_questions,
        )
        dlg = FlagQuestionDialog(self, qid)
        if dlg.ShowModal() == wx.ID_OK:
            reason = dlg.get_reason()
            note = dlg.get_note()
            if reason:
                ok = flag_question(qid, reason, note=note, user_id="local")
                if ok:
                    # Auto-retire after enough flags accumulate; this is a
                    # cheap query so running it inline is fine.
                    auto_retire_flagged_questions()
                    wx.MessageBox(
                        "Thanks — your report was recorded. We'll review it.",
                        "Reported", wx.OK | wx.ICON_INFORMATION, parent=self,
                    )
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
