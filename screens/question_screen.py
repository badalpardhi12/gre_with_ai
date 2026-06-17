"""
Question screen — ETS GRE exam-mode interface for Verbal and Quant sections.

Replicates the official ETS GRE test-taking UI (docs/gre_ui_spec_2026_06.md):
a navy header (ETS·GRE logo + Submit Section) over a white, serif content area,
a grey directions band with the exact ETS per-type directions, centered
Mark · Back · Next, and a navy footer (question navigator + Calc + Help +
countdown timer + Hide Time). Study affordances (Show Answer / Ask AI Tutor /
inline explanations) are intentionally removed from the in-test flow and live
in the post-session review instead. Reporting a broken question remains
reachable via the Help (?) button.

Handles every GRE subtype: QC (two-column Quantity A/B + 4 fixed choices),
MC single (radio), MC multi & SE (checkbox), Numeric Entry (single / stacked
fraction), Text Completion (per-blank highlight columns), Reading Comprehension
(split passage), and Select-in-Passage (clickable highlightable sentences in
the passage pane).
"""
import re

import wx
import wx.html2

from widgets.timer import TimerWidget
from widgets.question_nav import QuestionNav
from widgets.numeric_entry import NumericEntry
from widgets.calculator import CalculatorWidget
from widgets.math_view import MathView
from widgets.exam_button import ExamButton
from widgets.theme import ExamColor
from widgets import ui_scale


# Exact ETS directions strings per subtype (spec §3.4).
_DIRECTIONS = {
    "rc_single": "Select one answer choice.",
    "mcq_single": "Select one answer choice.",
    "data_interp": "Select one answer choice.",
    "qc": "Compare Quantity A and Quantity B, then select one answer choice.",
    "rc_multi": "Consider each answer choice separately and select all that apply.",
    "mcq_multi": "Select one or more answer choices.",
    "se": ("Select the two answer choices that, when used to complete the "
           "sentence, produce completed sentences that are alike in meaning."),
    "tc": ("Select one entry for each blank from the corresponding column of "
           "choices. Fill all blanks in the way that best completes the text."),
    "rc_select_passage": "Click on the sentence in the passage that best answers the question.",
    "numeric_entry": "Enter your answer as an integer or a decimal in the answer box.",
    "numeric_entry_fraction": ("Enter your answer as a fraction. There is one box "
                               "for the numerator and one box for the denominator."),
}


class QuestionScreen(wx.Panel):
    """ETS exam-mode question-answering screen for Verbal and Quant sections."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(ExamColor.CONTENT_BG)
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
        self._on_review_callback = None

        # Answer controls we create dynamically
        self._answer_controls = []
        self._numeric_entry = None
        self._calc_panel = None
        self._option_texts = []
        self._mixed_section = False

        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────

    def _build_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Navy header: ETS·GRE logo (left) + Submit Section (right) ──
        self.header = wx.Panel(self)
        self.header.SetBackgroundColour(ExamColor.HEADER_NAVY)
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)

        logo = wx.StaticText(self.header, label="ETS")
        logo.SetForegroundColour(ExamColor.HEADER_NAVY)
        logo.SetBackgroundColour(ExamColor.TEXT_ON_NAVY)
        logo.SetFont(ui_scale.exam_sans(ui_scale.EXAM_COUNTER_PT, wx.FONTWEIGHT_BOLD))
        header_sizer.Add(logo, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(3))
        gre = wx.StaticText(self.header, label="GRE")
        gre.SetForegroundColour(ExamColor.TEXT_ON_NAVY)
        gre.SetFont(ui_scale.exam_sans(ui_scale.EXAM_STEM_PT, wx.FONTWEIGHT_BOLD,
                                       wx.FONTSTYLE_ITALIC))
        header_sizer.Add(gre, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, ui_scale.space(3))

        header_sizer.AddStretchSpacer()

        self.submit_btn = ExamButton(self.header, "Submit Section", kind="mauve",
                                     icon="⬆", icon_after=True)
        self.submit_btn.Bind(wx.EVT_BUTTON, self._on_submit_section)
        header_sizer.Add(self.submit_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL,
                         ui_scale.space(2))
        self.header.SetSizer(header_sizer)
        main_sizer.Add(self.header, 0, wx.EXPAND)

        # ── Section / question counter row (white) ────────────────────
        counter_row = wx.BoxSizer(wx.HORIZONTAL)
        self.section_label = wx.StaticText(self, label="Section")
        self.section_label.SetForegroundColour(ExamColor.TEXT)
        self.section_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_COUNTER_PT,
                                                      wx.FONTWEIGHT_BOLD))
        counter_row.Add(self.section_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                        ui_scale.space(4))
        sep = wx.StaticText(self, label="|")
        sep.SetForegroundColour(ExamColor.TEXT_MUTED)
        counter_row.Add(sep, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, ui_scale.space(4))
        self.question_label = wx.StaticText(self, label="Question 1 of 12")
        self.question_label.SetForegroundColour(ExamColor.TEXT)
        self.question_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_COUNTER_PT,
                                                       wx.FONTWEIGHT_BOLD))
        counter_row.Add(self.question_label, 0, wx.ALIGN_CENTER_VERTICAL)
        main_sizer.Add(counter_row, 0, wx.EXPAND | wx.LEFT | wx.TOP | wx.BOTTOM,
                       ui_scale.space(3))
        main_sizer.Add(wx.StaticLine(self), 0, wx.EXPAND)

        # ── Content area (splitter: passage left, question+answers right) ─
        self.content_splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        self.content_splitter.SetSashGravity(0.5)
        self.content_splitter.SetMinimumPaneSize(280)

        # Left panel: passage/stimulus
        self.passage_panel = wx.Panel(self.content_splitter)
        self.passage_panel.SetBackgroundColour(ExamColor.CONTENT_BG)
        passage_sizer = wx.BoxSizer(wx.VERTICAL)
        # Two ways to show a passage: the WebView (RC/DI prose + figures) or a
        # native clickable sentence list (select-in-passage). Both live here;
        # exactly one is shown per question.
        self.passage_view = MathView(self.passage_panel, exam=True)
        passage_sizer.Add(self.passage_view, 1, wx.EXPAND | wx.ALL, 4)
        self.sip_panel = wx.ScrolledWindow(self.passage_panel)
        self.sip_panel.SetScrollRate(0, 12)
        self.sip_panel.SetBackgroundColour(ExamColor.CONTENT_BG)
        self.sip_sizer = wx.BoxSizer(wx.VERTICAL)
        self.sip_panel.SetSizer(self.sip_sizer)
        self.sip_panel.Hide()
        passage_sizer.Add(self.sip_panel, 1, wx.EXPAND | wx.ALL, 4)
        self.passage_panel.SetSizer(passage_sizer)

        # Right panel: question prompt + answers
        self.question_panel = wx.Panel(self.content_splitter)
        self.question_panel.SetBackgroundColour(ExamColor.CONTENT_BG)
        self.question_sizer = wx.BoxSizer(wx.VERTICAL)

        self.prompt_view = MathView(self.question_panel, size=(-1, 120), exam=True)
        self.question_sizer.Add(self.prompt_view, 0, wx.EXPAND | wx.ALL, 4)

        # Answer area (dynamically populated)
        self.answer_panel = wx.ScrolledWindow(self.question_panel)
        self.answer_panel.SetScrollRate(0, 10)
        self.answer_panel.SetBackgroundColour(ExamColor.CONTENT_BG)
        self.answer_sizer = wx.BoxSizer(wx.VERTICAL)
        self.answer_panel.SetSizer(self.answer_sizer)
        self.question_sizer.Add(self.answer_panel, 1, wx.EXPAND | wx.ALL, 4)
        self._option_texts = []
        self.answer_panel.Bind(wx.EVT_SIZE, self._on_answer_panel_resize)

        self.question_panel.SetSizer(self.question_sizer)
        self.content_splitter.Initialize(self.question_panel)
        main_sizer.Add(self.content_splitter, 1, wx.EXPAND)

        # ── Directions band (full-width grey) ─────────────────────────
        self.directions_band = wx.Panel(self)
        self.directions_band.SetBackgroundColour(ExamColor.DIRECTIONS_BAND)
        db_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.directions_label = wx.StaticText(self.directions_band, label="",
                                              style=wx.ALIGN_CENTER)
        self.directions_label.SetForegroundColour(ExamColor.DIRECTIONS_TEXT)
        self.directions_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT))
        db_sizer.AddStretchSpacer()
        db_sizer.Add(self.directions_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL,
                     ui_scale.space(2))
        db_sizer.AddStretchSpacer()
        self.directions_band.SetSizer(db_sizer)
        main_sizer.Add(self.directions_band, 0, wx.EXPAND)

        # ── Centered Mark · Back · Next ───────────────────────────────
        nav_row = wx.BoxSizer(wx.HORIZONTAL)
        nav_row.AddStretchSpacer()
        self.mark_btn = ExamButton(self, "Mark", kind="grey", icon="☐")
        self.mark_btn.Bind(wx.EVT_BUTTON, self._on_mark)
        nav_row.Add(self.mark_btn, 0, wx.ALL, ui_scale.space(2))
        self.prev_btn = ExamButton(self, "Back", kind="grey", icon="◀")
        self.prev_btn.Bind(wx.EVT_BUTTON, self._on_prev)
        nav_row.Add(self.prev_btn, 0, wx.ALL, ui_scale.space(2))
        self.next_btn = ExamButton(self, "Next", kind="next", icon="▶", icon_after=True)
        self.next_btn.Bind(wx.EVT_BUTTON, self._on_next)
        nav_row.Add(self.next_btn, 0, wx.ALL, ui_scale.space(2))
        nav_row.AddStretchSpacer()
        main_sizer.Add(nav_row, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, ui_scale.space(1))

        # ── Navy footer: navigator + Calc + Help + timer + Hide Time ──
        self.footer = wx.Panel(self)
        self.footer.SetBackgroundColour(ExamColor.HEADER_NAVY)
        footer_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.question_nav = QuestionNav(self.footer, 0)
        self.question_nav.set_on_navigate(self._on_nav_jump)
        footer_sizer.Add(self.question_nav, 1, wx.EXPAND)

        self.calc_btn = ExamButton(self.footer, "Calc", kind="grey")
        self.calc_btn.Bind(wx.EVT_BUTTON, self._on_toggle_calc)
        footer_sizer.Add(self.calc_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL,
                         ui_scale.space(1))

        self.help_btn = ExamButton(self.footer, "Help", kind="grey", icon="?")
        self.help_btn.Bind(wx.EVT_BUTTON, self._on_help)
        footer_sizer.Add(self.help_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL,
                         ui_scale.space(1))

        self.timer = TimerWidget(self.footer)
        footer_sizer.Add(self.timer, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL,
                         ui_scale.space(1))

        self.footer.SetSizer(footer_sizer)
        main_sizer.Add(self.footer, 0, wx.EXPAND)

        # Floating calculator (created hidden; toggled by Calc).
        self._calc_panel = CalculatorWidget(self)
        self._calc_panel.Hide()
        if hasattr(self._calc_panel, "set_on_transfer"):
            self._calc_panel.set_on_transfer(self._on_calc_transfer)

        self.SetSizer(main_sizer)

    # ── Public API ────────────────────────────────────────────────────

    def configure(self, section_state, question_bank, measure, mode="simulation",
                  exam=None):
        """Set up the screen for a section. ``exam`` is the parent ExamSession."""
        self._section_state = section_state
        self._question_bank = question_bank
        self._exam = exam
        self._measure = measure
        self._mode = mode

        from models.exam_session import SECTION_META
        sec_type = section_state.section_type
        _, sec_idx, _, _ = SECTION_META[sec_type]
        # ETS shows "Section X of Y" with Y = total scored sections (5).
        total_sections = getattr(section_state, "total_sections", 5) or 5
        if getattr(section_state, "display_label", None):
            self.section_label.SetLabel(section_state.display_label)
        else:
            self.section_label.SetLabel(f"Section {sec_idx} of {total_sections}")

        # Timer
        self.timer.set_time(section_state.time_limit)
        self.timer.set_on_expire(self._handle_time_expire)
        self.timer.set_on_tick(lambda elapsed: section_state.tick(elapsed))

        is_quant = measure == "quant"
        self.calc_btn.Show(is_quant)
        self._mixed_section = bool(getattr(section_state, "display_label", None))

        self.question_nav.rebuild(section_state.total_questions)

        self._load_question(0)
        self.Layout()

    def start_timer(self):
        self.timer.start()

    def set_on_end_section(self, callback):
        self._on_end_section = callback

    def set_on_time_expire(self, callback):
        self._on_time_expire = callback

    def set_on_review(self, callback):
        self._on_review_callback = callback

    def set_on_exit_to_dashboard(self, callback):
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

        q = self._question_bank.get_question(qid)
        if q is None:
            self.prompt_view.set_content(f"<p><i>Question {qid} not found.</i></p>")
            return

        self._current_q = q
        subtype = q["subtype"]

        # Mixed-measure section (Quick Drill): toggle calc per question and
        # prepend the measure tag so the user always knows the side.
        if getattr(self, "_mixed_section", False):
            q_measure = (q.get("measure") or "").lower()
            self.calc_btn.Show(q_measure == "quant")
            measure_tag = "Verbal" if q_measure == "verbal" else (
                "Quant" if q_measure == "quant" else q_measure.title())
            self.question_label.SetLabel(
                f"{measure_tag} • Question {index + 1} of {ss.total_questions}")
        else:
            self.question_label.SetLabel(
                f"Question {index + 1} of {ss.total_questions}")

        # Directions band (per-type ETS string).
        na = q.get("numeric_answer") or {}
        if subtype == "numeric_entry" and self._is_fraction_mode(na):
            directions = _DIRECTIONS["numeric_entry_fraction"]
        else:
            directions = _DIRECTIONS.get(subtype, "")
        self.directions_label.SetLabel(directions)
        self.directions_band.Layout()

        # Passage / stimulus. Select-in-passage uses the native clickable
        # sentence pane; everything else uses the WebView.
        self._show_passage(q)

        # Prompt — QC renders a two-column Quantity A/B table; others render
        # the stem HTML directly (serif via exam MathView).
        if subtype == "qc":
            prompt_html = self._qc_prompt_html(q["prompt"])
        else:
            prompt_html = f'<div class="prompt">{q["prompt"]}</div>'
        self.prompt_view.set_content_auto_height(prompt_html, min_h=80, max_h=340)

        # Mark button reflects state.
        self._sync_mark_button()

        # Build answer controls + restore saved response.
        self._build_answer_controls(q)
        saved = ss.get_response(qid)
        if saved:
            self._restore_response(saved)

        self._update_nav()
        self.Layout()

    def _show_passage(self, q):
        """Show the left pane appropriately: clickable sentences for
        select-in-passage, the WebView for RC/DI, or nothing."""
        subtype = q["subtype"]
        stim = q.get("stimulus") or {}
        content = stim.get("content") or ""

        if subtype == "rc_select_passage":
            # Native clickable sentence list in the left pane.
            self.passage_view.Hide()
            self.sip_panel.Show()
            self._split_if_needed()
            return  # sentences are built in _build_answer_controls (needs options)

        self.sip_panel.Hide()
        if content:
            self.passage_view.Show()
            self.passage_view.set_content(content)
            self._split_if_needed()
        else:
            self.passage_view.set_content("")
            if self.content_splitter.IsSplit():
                self.content_splitter.Unsplit(self.passage_panel)
            self.passage_panel.Hide()

    def _split_if_needed(self):
        self.passage_panel.Show()
        if not self.content_splitter.IsSplit():
            self.content_splitter.SplitVertically(
                self.passage_panel, self.question_panel, 0)
            wx.CallAfter(self._center_passage_sash)

    @staticmethod
    def _is_fraction_mode(na):
        mode = na.get("mode") or "auto"
        if mode == "fraction":
            return True
        if mode == "decimal":
            return False
        return na.get("numerator") is not None

    def _qc_prompt_html(self, prompt):
        """Transform a QC stem (which stores '<p>Quantity A: …</p>
        <p>Quantity B: …</p>' plus optional common info) into a two-column
        table with underlined headers, preserving KaTeX math."""
        a = re.search(r"Quantity\s*A\s*[:\-]\s*(.*?)(?=<p>\s*Quantity\s*B|$)",
                      prompt, re.IGNORECASE | re.DOTALL)
        b = re.search(r"Quantity\s*B\s*[:\-]\s*(.*?)(?=</p>|$)",
                      prompt, re.IGNORECASE | re.DOTALL)
        if not (a and b):
            return f'<div class="prompt">{prompt}</div>'

        def _clean(s):
            return re.sub(r"</?p>", "", s).strip()

        # Common information = anything before the first "Quantity A".
        common = prompt[:a.start()]
        common = re.sub(r"<p>\s*</p>", "", common).strip()
        qa, qb = _clean(a.group(1)), _clean(b.group(1))
        common_html = f'<div class="prompt">{common}</div>' if common else ""
        return (
            f'{common_html}'
            f'<table style="width:100%; border-collapse:collapse; border:none;">'
            f'<tr>'
            f'<td style="border:none; text-align:center; width:50%;">'
            f'<u>Quantity A</u></td>'
            f'<td style="border:none; text-align:center; width:50%;">'
            f'<u>Quantity B</u></td></tr>'
            f'<tr>'
            f'<td style="border:none; text-align:center;">{qa}</td>'
            f'<td style="border:none; text-align:center;">{qb}</td></tr>'
            f'</table>'
        )

    def _build_answer_controls(self, q):
        """Create answer controls based on subtype (re-skinned to ETS)."""
        self.answer_sizer.Clear(True)
        self._answer_controls = []
        self._numeric_entry = None
        self._option_texts = []

        subtype = q["subtype"]
        options = q.get("options", [])

        if subtype == "rc_select_passage":
            self._build_select_in_passage(q, options)

        elif subtype in ("rc_single", "mcq_single", "qc", "data_interp"):
            for opt in options:
                label = (opt["text"] if subtype == "qc"
                         else f"{opt['label']}) {opt['text']}")
                radio = self._add_wrapping_option(
                    label_text=label, control_type="radio",
                    is_first=(opt is options[0]), on_change=self._on_answer_change)
                self._answer_controls.append(("radio", opt["label"], radio))

        elif subtype in ("rc_multi", "mcq_multi", "se"):
            for opt in options:
                cb = self._add_wrapping_option(
                    label_text=f"{opt['label']}) {opt['text']}",
                    control_type="check", on_change=self._on_answer_change)
                self._answer_controls.append(("check", opt["label"], cb))

        elif subtype == "tc":
            self._build_tc_columns(options)

        elif subtype == "numeric_entry":
            na = q.get("numeric_answer") or {}
            is_fraction = self._is_fraction_mode(na)
            prefix = na.get("prefix") or None
            suffix = na.get("suffix") or na.get("unit") or None
            self._numeric_entry = NumericEntry(
                self.answer_panel, fraction_mode=is_fraction,
                prefix=prefix, suffix=suffix)
            self._numeric_entry.set_on_change(lambda _: self._on_answer_change(None))
            self.answer_sizer.Add(self._numeric_entry, 0, wx.ALL, 8)
            # Transfer Display only enabled for single-box numeric entry.
            if hasattr(self._calc_panel, "set_transfer_enabled"):
                self._calc_panel.set_transfer_enabled(not is_fraction)

        if subtype != "numeric_entry" and hasattr(self._calc_panel, "set_transfer_enabled"):
            self._calc_panel.set_transfer_enabled(False)

        self._rewrap_options()
        self.answer_panel.FitInside()
        self.answer_panel.Layout()

    # ── Text Completion: per-blank highlight columns ──────────────────

    def _build_tc_columns(self, options):
        """Render TC as one column of clickable highlightable choices per
        blank, labeled Blank (i)/(ii)/(iii) (spec §5.6)."""
        from services.scoring import normalize_tc_options
        blanks = {}
        for blank, choice, opt in normalize_tc_options(options):
            blanks.setdefault(blank, []).append((choice, opt["text"]))

        roman = {"blank1": "(i)", "blank2": "(ii)", "blank3": "(iii)"}
        cols = wx.BoxSizer(wx.HORIZONTAL)
        # Per-blank current selection state for the highlight mechanic.
        self._tc_selected = {}
        for blank_name, choices in sorted(blanks.items()):
            col = wx.BoxSizer(wx.VERTICAL)
            hdr = wx.StaticText(self.answer_panel,
                                label=f"Blank {roman.get(blank_name, '')}".strip())
            hdr.SetForegroundColour(ExamColor.TEXT)
            hdr.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT,
                                           wx.FONTWEIGHT_BOLD))
            col.Add(hdr, 0, wx.BOTTOM, ui_scale.space(1))
            for choice_label, choice_text in choices:
                cell = self._make_tc_choice(blank_name, choice_label, choice_text)
                col.Add(cell, 0, wx.EXPAND | wx.BOTTOM, ui_scale.space(1))
            cols.Add(col, 1, wx.EXPAND | wx.RIGHT, ui_scale.space(4))
        self.answer_sizer.Add(cols, 0, wx.EXPAND | wx.ALL, ui_scale.space(2))

    def _make_tc_choice(self, blank_name, choice_label, choice_text):
        from widgets.latex_inline_text import latex_inline_to_text
        cell = wx.Panel(self.answer_panel)
        cell.SetBackgroundColour(ExamColor.CONTENT_BG)
        s = wx.BoxSizer(wx.VERTICAL)
        txt = wx.StaticText(cell, label=latex_inline_to_text(choice_text))
        txt.SetForegroundColour(ExamColor.TEXT)
        txt.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
        s.Add(txt, 0, wx.ALL, ui_scale.space(2))
        cell.SetSizer(s)

        def _pick(_evt, bn=blank_name, cl=choice_label, c=cell):
            self._on_tc_pick(bn, cl, c)
        for w in (cell, txt):
            w.Bind(wx.EVT_LEFT_DOWN, _pick)
            w.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        # Track for restore + selection readout.
        self._answer_controls.append(("tc_cell", blank_name, choice_label, cell))
        return cell

    def _on_tc_pick(self, blank_name, choice_label, cell):
        """Highlight the picked choice in its column; clear siblings."""
        self._tc_selected[blank_name] = choice_label
        for entry in self._answer_controls:
            if entry[0] == "tc_cell" and entry[1] == blank_name:
                c = entry[3]
                sel = entry[2] == choice_label
                c.SetBackgroundColour(
                    ExamColor.TC_HIGHLIGHT if sel else ExamColor.CONTENT_BG)
                c.Refresh()
        self._on_answer_change(None)

    # ── Select-in-passage: clickable sentences in the left pane ───────

    def _build_select_in_passage(self, q, options):
        """Build the native clickable sentence list in the left passage pane.
        Each sentence highlights pale-yellow when selected (spec §5.7)."""
        from widgets.latex_inline_text import latex_inline_to_text
        self.sip_sizer.Clear(True)
        self._answer_controls = []
        sentences = self._extract_passage_sentences(
            (q.get("stimulus") or {}).get("content") or "")
        avail = max(360, self.sip_panel.GetClientSize().width - 40)
        for opt in options:
            label_idx = opt["label"]
            text = sentences.get(str(label_idx)) or opt["text"]
            row = wx.Panel(self.sip_panel)
            row.SetBackgroundColour(ExamColor.CONTENT_BG)
            rs = wx.BoxSizer(wx.VERTICAL)
            st = wx.StaticText(row, label=latex_inline_to_text(text))
            st.SetForegroundColour(ExamColor.TEXT)
            st.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
            st.Wrap(avail)
            rs.Add(st, 0, wx.ALL, ui_scale.space(2))
            row.SetSizer(rs)

            def _pick(_evt, lbl=label_idx, r=row):
                self._on_sip_pick(lbl, r)
            for w in (row, st):
                w.Bind(wx.EVT_LEFT_DOWN, _pick)
                w.SetCursor(wx.Cursor(wx.CURSOR_HAND))
            self.sip_sizer.Add(row, 0, wx.EXPAND | wx.BOTTOM, ui_scale.space(1))
            self._answer_controls.append(("sip", str(label_idx), row))
        self.sip_panel.FitInside()
        self.sip_panel.Layout()

    def _on_sip_pick(self, label_idx, row):
        for entry in self._answer_controls:
            if entry[0] == "sip":
                r = entry[2]
                sel = entry[1] == str(label_idx)
                r.SetBackgroundColour(
                    ExamColor.SELECT_IN_PASSAGE_HL if sel else ExamColor.CONTENT_BG)
                for ch in r.GetChildren():
                    ch.SetBackgroundColour(
                        ExamColor.SELECT_IN_PASSAGE_HL if sel else ExamColor.CONTENT_BG)
                r.Refresh()
        self._sip_selected = str(label_idx)
        self._on_answer_change(None)

    def _add_wrapping_option(self, label_text, control_type, on_change,
                             is_first=False):
        """Row with a native radio/checkbox (renders as ETS oval/square on
        macOS) + a wrappable serif text label. Clicking the text activates
        the control (ETS click-target parity)."""
        from widgets.latex_inline_text import latex_inline_to_text
        if control_type == "radio":
            style = wx.RB_GROUP if is_first else 0
            ctrl = wx.RadioButton(self.answer_panel, label="", style=style)
            ctrl.Bind(wx.EVT_RADIOBUTTON, on_change)
        else:
            ctrl = wx.CheckBox(self.answer_panel, label="")
            ctrl.Bind(wx.EVT_CHECKBOX, on_change)
        ctrl.SetBackgroundColour(ExamColor.CONTENT_BG)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(ctrl, 0, wx.RIGHT | wx.ALIGN_TOP, ui_scale.space(2))
        text = wx.StaticText(self.answer_panel, label=latex_inline_to_text(label_text))
        text.SetForegroundColour(ExamColor.TEXT)
        text.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
        text.Bind(wx.EVT_LEFT_DOWN, lambda evt, c=ctrl: self._toggle_from_text(c, evt))
        row.Add(text, 1, wx.EXPAND)
        self.answer_sizer.Add(row, 0, wx.EXPAND | wx.ALL, ui_scale.space(2))
        self._option_texts.append(text)
        return ctrl

    def _toggle_from_text(self, ctrl, _evt):
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
        self._rewrap_options()
        event.Skip()

    def _center_passage_sash(self):
        if not self.content_splitter.IsSplit():
            return
        w = self.content_splitter.GetClientSize().width
        if w < 560:
            wx.CallLater(50, self._center_passage_sash)
            return
        self.content_splitter.SetSashPosition(w // 2)
        self._rewrap_options()

    def _rewrap_options(self):
        if not self._option_texts:
            return
        avail = self.answer_panel.GetClientSize().width - 56
        if avail < 80:
            return
        for t in self._option_texts:
            if t:
                t.Wrap(avail)
        self.answer_sizer.Layout()
        self.answer_panel.FitInside()

    # ── Response read / restore ───────────────────────────────────────

    def _get_current_response(self):
        if self._current_q is None:
            return {}
        subtype = self._current_q["subtype"]

        if subtype == "rc_select_passage":
            sel = getattr(self, "_sip_selected", None)
            return {"selected_sentence": sel} if sel else {}

        elif subtype in ("rc_single", "mcq_single", "qc", "data_interp"):
            for ctrl_type, label, ctrl in self._answer_controls:
                if ctrl.GetValue():
                    return {"selected": [label]}
            return {}

        elif subtype in ("rc_multi", "mcq_multi", "se"):
            selected = [label for ct, label, ctrl in self._answer_controls
                        if ct == "check" and ctrl.GetValue()]
            return {"selected": selected} if selected else {}

        elif subtype == "tc":
            sel = dict(getattr(self, "_tc_selected", {}))
            return {"selected": sel} if sel else {}

        elif subtype == "numeric_entry" and self._numeric_entry:
            return self._numeric_entry.get_response()

        return {}

    def _restore_response(self, saved):
        if not saved or self._current_q is None:
            return
        subtype = self._current_q["subtype"]

        if subtype == "rc_select_passage":
            sel = saved.get("selected_sentence")
            if sel is not None:
                self._on_sip_pick(sel, None)

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
            for blank, choice in sel.items():
                self._tc_selected[blank] = choice
            # Re-apply highlights.
            for entry in self._answer_controls:
                if entry[0] == "tc_cell":
                    _, bn, cl, cell = entry
                    on = sel.get(bn) == cl
                    cell.SetBackgroundColour(
                        ExamColor.TC_HIGHLIGHT if on else ExamColor.CONTENT_BG)
                    cell.Refresh()

        elif subtype == "numeric_entry" and self._numeric_entry:
            self._numeric_entry.set_response(saved)

    # ── Event handlers ────────────────────────────────────────────────

    def _on_answer_change(self, event):
        ss = self._section_state
        if ss is None:
            return
        qid = ss.current_question_id
        response = self._get_current_response()
        ss.set_response(qid, response)
        if self._exam is not None:
            self._exam.log_event("answer_changed", {"qid": qid, "response": response})
        self._update_nav()

    def _sync_mark_button(self):
        ss = self._section_state
        qid = ss.current_question_id if ss else None
        marked = bool(ss and qid in ss.marked)
        self.mark_btn.set_label("Marked" if marked else "Mark",
                                icon="☑" if marked else "☐")

    def _on_mark(self, event):
        ss = self._section_state
        if ss:
            ss.toggle_mark()
            self._sync_mark_button()
            self._update_nav()

    def _on_toggle_calc(self, event):
        self._calc_panel.Show(not self._calc_panel.IsShown())

    def _on_calc_transfer(self, value):
        """Transfer Display → numeric-entry single box."""
        if self._numeric_entry and not getattr(self._numeric_entry, "fraction_mode", False):
            try:
                self._numeric_entry.set_response({"value": value})
            except Exception:
                pass
            self._on_answer_change(None)

    def _on_help(self, event):
        """Help (?) → menu with directions and 'Report a problem'."""
        menu = wx.Menu()
        directions_item = menu.Append(wx.ID_ANY, "Question directions")
        report_item = menu.Append(wx.ID_ANY, "Report a problem with this question…")
        self.Bind(wx.EVT_MENU, lambda e: self._show_directions_help(), directions_item)
        self.Bind(wx.EVT_MENU, lambda e: self._on_report_question(None), report_item)
        self.PopupMenu(menu)
        menu.Destroy()

    def _show_directions_help(self):
        subtype = (self._current_q or {}).get("subtype", "")
        msg = _DIRECTIONS.get(subtype, "Answer the question, then click Next.")
        wx.MessageBox(msg, "Directions", wx.OK | wx.ICON_INFORMATION, self)

    def _on_prev(self, event):
        ss = self._section_state
        if ss and ss.current_index > 0:
            self._load_question(ss.current_index - 1)

    def _on_next(self, event):
        ss = self._section_state
        if ss is None:
            return
        if ss.current_index < ss.total_questions - 1:
            self._load_question(ss.current_index + 1)
        else:
            # Next on the last question → section review (ETS flow).
            if self._on_review_callback:
                self._on_review_callback()

    def _on_nav_jump(self, index):
        self._load_question(index)

    def _on_submit_section(self, event):
        """Submit Section → review screen (final submit happens there)."""
        if self._on_review_callback:
            self._on_review_callback()
        else:
            self._finalize_section()

    def _finalize_section(self):
        ss = self._section_state
        if ss is None:
            return
        unanswered = ss.total_questions - ss.count_answered()
        if unanswered > 0:
            dlg = wx.MessageDialog(
                self,
                f"You have {unanswered} unanswered question(s). End section anyway?",
                "Confirm End Section",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
            if dlg.ShowModal() != wx.ID_YES:
                dlg.Destroy()
                return
            dlg.Destroy()
        self.timer.stop()
        if self._on_end_section:
            self._on_end_section()

    def _handle_time_expire(self):
        self.timer.stop()
        wx.MessageBox("Time is up! Moving to the next section.",
                      "Time Expired", wx.OK | wx.ICON_INFORMATION, self)
        if self._on_time_expire:
            self._on_time_expire()
        elif self._on_end_section:
            self._on_end_section()

    def _update_nav(self):
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
        self.prev_btn.Enable(ss.current_index > 0)

    # ── Select-in-passage helpers (preserved) ─────────────────────────

    _SENT_TAG_RE = re.compile(
        r"<sent\s+id=['\"](\d+)['\"]\s*>(.*?)</sent>",
        re.IGNORECASE | re.DOTALL)

    @classmethod
    def _extract_passage_sentences(cls, passage_html):
        if not passage_html:
            return {}
        out = {}
        for m in cls._SENT_TAG_RE.finditer(passage_html):
            idx = m.group(1)
            raw = m.group(2)
            text = re.sub(r"<[^>]+>", "", raw).strip()
            if text:
                out[idx] = text
        return out

    @staticmethod
    def _escape_html(text):
        return (str(text).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    # ── Report flow (preserved) ───────────────────────────────────────

    def _on_report_question(self, _):
        """Open a dialog to report a problem with this question, persist a
        QuestionFlag, and offer to file a pre-filled GitHub issue."""
        if not self._current_q:
            return
        qid = self._current_q.get("id")
        if qid is None:
            return
        pending_screenshot = self._prepare_screenshot(qid)

        from widgets.flag_dialog import FlagQuestionDialog
        from services.question_bank import (
            flag_question, auto_retire_flagged_questions)
        dlg = FlagQuestionDialog(self, qid)
        if dlg.ShowModal() == wx.ID_OK:
            reason = dlg.get_reason()
            note = dlg.get_note()
            want_shot = dlg.wants_screenshot()
            if reason:
                ok = flag_question(qid, reason, note=note, user_id="local")
                if ok:
                    auto_retire_flagged_questions()
                    attachment = self._finalize_screenshot(
                        pending_screenshot, enabled=want_shot)
                    self._open_github_report(qid, reason, note, attachment)
        dlg.Destroy()

    def _prepare_screenshot(self, qid):
        try:
            from services.report_screenshot import capture_main_window_png
            import wx as _wx
            try:
                from main_frame import MainFrame as _MainFrame
            except Exception:
                _MainFrame = None
            png, _main = capture_main_window_png(
                _wx.GetTopLevelWindows(), main_frame_cls=_MainFrame)
            return png
        except Exception as exc:  # pragma: no cover — defensive
            import logging
            logging.getLogger(__name__).warning(
                "main-window screenshot capture failed for q%s: %s", qid, exc)
            return None

    def _finalize_screenshot(self, png_bytes, enabled):
        if not enabled or not png_bytes:
            return None
        qid = self._current_q.get("id") if self._current_q else None
        try:
            from services.report_screenshot import (
                copy_png_to_clipboard, save_png_to_file, screenshot_path_for)
            dest = screenshot_path_for(qid)
            saved_path = save_png_to_file(png_bytes, dest)
            on_clipboard = copy_png_to_clipboard(png_bytes)
            return {"captured": True, "clipboard": on_clipboard,
                    "file_path": saved_path,
                    "error": None if on_clipboard else "clipboard-locked"}
        except Exception as exc:  # pragma: no cover — defensive
            import logging
            logging.getLogger(__name__).warning("screenshot finalize failed: %s", exc)
            return None

    def _open_github_report(self, qid, reason, note, attachment=None):
        import webbrowser
        try:
            from services.issue_reporter import build_issue_url
            from models.database import Question
            payload = dict(self._current_q)
            q_row = Question.get_or_none(Question.id == qid)
            if q_row is not None:
                payload["source"] = q_row.source
                payload["status"] = q_row.status
            combined_comment = note or ""
            if reason:
                prefix = f"[reason: {reason}]"
                combined_comment = (f"{prefix}\n\n{combined_comment}".strip()
                                    if combined_comment else prefix)
            url = build_issue_url(payload, combined_comment)
        except Exception as exc:  # pragma: no cover — defensive
            import logging
            logging.getLogger(__name__).warning(
                "Failed to build issue URL for q%s: %s", qid, exc)
            wx.MessageBox("Thanks — your report was recorded locally.",
                          "Reported", wx.OK | wx.ICON_INFORMATION, parent=self)
            return

        extra = ""
        if attachment:
            if attachment.get("clipboard"):
                extra = ("\n\nA screenshot of the main window has been copied "
                         "to your clipboard — paste it into the GitHub issue "
                         "body with Cmd+V after the page opens.")
            elif attachment.get("file_path"):
                extra = ("\n\nScreenshot couldn't be copied to the clipboard, "
                         f"but a copy was saved to:\n  {attachment['file_path']}\n"
                         "Drag the file into the GitHub issue body to attach it.")

        resp = wx.MessageBox(
            "Thanks — your report was recorded locally.\n\n"
            "Your browser will open a pre-filled GitHub issue. Please "
            "click “Submit new issue” on that page to send it to the "
            "developer (you'll be asked to sign in to GitHub once)."
            + extra + "\n\nOpen the GitHub issue now?",
            "Send report to the developer?",
            wx.YES_NO | wx.ICON_QUESTION, parent=self)
        if resp == wx.YES:
            try:
                webbrowser.open(url, new=2)
            except Exception as exc:  # pragma: no cover — platform-dependent
                import logging
                logging.getLogger(__name__).warning("webbrowser.open failed: %s", exc)
