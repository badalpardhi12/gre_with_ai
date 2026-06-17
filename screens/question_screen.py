"""
Question screen — ETS "Test Preview Tool" interface for Verbal and Quant.

Replicates the official GRE test UI: the shared ExamChrome (charcoal header +
maroon rule + top-right tool ribbon [Exit·Calc·Mark·Review·Help·Back·Next] +
pink section bar with the section/question label and the countdown timer) over a
black-bordered white content box floating on a gray page. There is NO bottom
numbered navigator — navigation is Back / Next plus the Review list. Directions
appear in a top gray band (the long instruction, for TC/SE/RC-multi) and/or a
bottom-center gray pill (the short instruction, always). Reading/DI questions
split into a left stimulus pane (with a blue "Questions N..M are based on …"
title bar) and a right question pane.

Answer controls use owner-drawn ETS ovals (single) / squares (multi); TC uses
bordered per-blank choice tables; Select-in-Passage uses clickable highlightable
sentences; Numeric Entry uses a white box (with optional $/unit) or a stacked
fraction box. Study affordances are not shown in-test (explanations live in the
post-session review).
"""
import re

import wx

from widgets.numeric_entry import NumericEntry
from widgets.calculator import CalculatorWidget
from widgets.math_view import MathView
from widgets.exam_chrome import ExamChrome
from widgets.exam_choice import ExamChoice
from widgets.theme import ExamColor
from widgets import ui_scale


# Long directions for the TOP gray band (subtypes that have one). TC is
# resolved dynamically by blank count in _load_question.
_TOP_BAND = {
    "rc_multi": "Consider each of the choices separately and select all that apply.",
    "se": ("Select the two answer choices that, when used to complete the "
           "sentence, fit the meaning of the sentence as a whole and produce "
           "completed sentences that are alike in meaning."),
}

# Short directions for the BOTTOM pill (always present). TC resolved dynamically.
_BOTTOM_PILL = {
    "mcq_single": "Select one answer choice.",
    "rc_single": "Select one answer choice.",
    "qc": "Select one answer choice.",
    "data_interp": "Select one answer choice.",
    "mcq_multi": "Select one or more answer choices.",
    "rc_multi": "Select one or more answer choices.",
    "se": "Select two answer choices.",
    "rc_select_passage": "Select a sentence in the passage.",
}

# Subtypes that ALWAYS use the split passage/stimulus pane.
_SPLIT_SUBTYPES = {"rc_single", "rc_multi", "rc_select_passage", "data_interp"}


def _is_data_presentation(q):
    """True when the question's stimulus is a DATA presentation (a table or a
    data chart) — Data-Interpretation-style content that belongs in the left
    pane at full size, NOT a small geometry figure (which stays inline with the
    stem). Geometry is identified by an ``svg_geometry`` render_spec."""
    stim = q.get("stimulus") or {}
    content = (stim.get("content") or "").lower()
    if not content:
        return False
    if "svg_geometry" in (stim.get("render_spec") or ""):
        return False  # geometry figure → inline, not a data pane
    if "<table" in content:
        return True
    stype = stim.get("type")
    if stype in ("graph", "table", "chart") and "<img" in content:
        return True
    return False


def _should_split(q):
    """Whether to use the two-pane (stimulus left / question right) layout.
    RC + DI always split; other quant subtypes split only when they carry a
    data table/chart. QC never splits (it uses its own inline two-column)."""
    subtype = q["subtype"]
    if subtype == "qc":
        return False
    if subtype in _SPLIT_SUBTYPES:
        return True
    return _is_data_presentation(q)


class QuestionScreen(wx.Panel):
    """ETS Test-Preview question screen for Verbal and Quant sections."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(ExamColor.PAGE_GRAY)
        self._section_state = None
        self._question_bank = None
        self._exam = None
        self._current_q = None
        self._measure = None
        self._mode = "simulation"

        self._on_end_section = None
        self._on_time_expire = None
        self._on_exit_to_dashboard = None
        self._on_review_callback = None

        self._answer_controls = []
        self._numeric_entry = None
        self._calc_panel = None
        self._option_texts = []
        self._mixed_section = False
        self._radio_group = None
        self._tc_selected = {}
        self._sip_selected = None

        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────

    def _build_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)

        # Shared ETS chrome (header + ribbon + pink section bar with the timer).
        self.chrome = ExamChrome(self, with_timer=True)
        self.chrome.set_on("exit", self._on_exit_section)
        self.chrome.set_on("calc", self._on_toggle_calc)
        self.chrome.set_on("mark", self._on_mark)
        self.chrome.set_on("review", self._on_review)
        self.chrome.set_on("help", self._on_help)
        self.chrome.set_on("back", self._on_prev)
        self.chrome.set_on("next", self._on_next)
        main.Add(self.chrome, 0, wx.EXPAND)
        self.timer = self.chrome.timer  # back-compat alias

        # Gray page → black-bordered white content box.
        page = wx.BoxSizer(wx.VERTICAL)
        self.content_border = wx.Panel(self)
        self.content_border.SetBackgroundColour(ExamColor.CONTENT_BORDER)
        bsz = wx.BoxSizer(wx.VERTICAL)
        self.content_box = wx.Panel(self.content_border)
        self.content_box.SetBackgroundColour(ExamColor.CONTENT_BG)
        bsz.Add(self.content_box, 1, wx.EXPAND | wx.ALL, 2)  # 2px black frame
        self.content_border.SetSizer(bsz)
        page.Add(self.content_border, 1, wx.EXPAND | wx.ALL, ui_scale.space(2))
        main.Add(page, 1, wx.EXPAND)

        box = wx.BoxSizer(wx.VERTICAL)

        # Top gray directions band (long instruction; hidden when unused).
        self.top_band = wx.Panel(self.content_box)
        self.top_band.SetBackgroundColour(ExamColor.DIRECTIONS_PILL)
        tb = wx.BoxSizer(wx.HORIZONTAL)
        self.top_band_label = wx.StaticText(self.top_band, label="",
                                            style=wx.ALIGN_CENTER)
        self.top_band_label.SetForegroundColour(ExamColor.DIRECTIONS_PILL_TEXT)
        self.top_band_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT))
        tb.AddStretchSpacer()
        tb.Add(self.top_band_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL,
               ui_scale.space(2))
        tb.AddStretchSpacer()
        self.top_band.SetSizer(tb)
        box.Add(self.top_band, 0, wx.EXPAND | wx.ALL, ui_scale.space(3))

        # Content: a splitter (passage|question) — for single-column subtypes
        # only the question pane is shown (unsplit).
        self.content_splitter = wx.SplitterWindow(self.content_box,
                                                  style=wx.SP_LIVE_UPDATE)
        self.content_splitter.SetSashGravity(0.5)
        self.content_splitter.SetMinimumPaneSize(260)

        # Left: passage/stimulus with a blue title bar.
        self.passage_panel = wx.Panel(self.content_splitter)
        self.passage_panel.SetBackgroundColour(ExamColor.CONTENT_BG)
        psz = wx.BoxSizer(wx.VERTICAL)
        self.passage_title_bar = wx.Panel(self.passage_panel)
        self.passage_title_bar.SetBackgroundColour(ExamColor.PASSAGE_TITLE_BAR)
        ptb = wx.BoxSizer(wx.HORIZONTAL)
        self.passage_title = wx.StaticText(self.passage_title_bar, label="")
        self.passage_title.SetForegroundColour(ExamColor.PASSAGE_TITLE_TEXT)
        self.passage_title.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT - 1,
                                                      wx.FONTWEIGHT_BOLD))
        # Indent the title text from the bar edges so it doesn't crowd the
        # left border (the ETS "Question is based on…" bar has clear padding).
        ptb.Add(self.passage_title, 1,
                wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, ui_scale.space(2))
        self.passage_title_bar.SetSizer(ptb)
        psz.Add(self.passage_title_bar, 0, wx.EXPAND)
        self.passage_view = MathView(self.passage_panel, exam=True)
        psz.Add(self.passage_view, 1, wx.EXPAND | wx.ALL, 4)
        # Native clickable-sentence pane for select-in-passage.
        self.sip_panel = wx.ScrolledWindow(self.passage_panel)
        self.sip_panel.SetScrollRate(0, 12)
        self.sip_panel.SetBackgroundColour(ExamColor.CONTENT_BG)
        self.sip_sizer = wx.BoxSizer(wx.VERTICAL)
        self.sip_panel.SetSizer(self.sip_sizer)
        self.sip_panel.Hide()
        psz.Add(self.sip_panel, 1, wx.EXPAND | wx.ALL, 4)
        self.passage_panel.SetSizer(psz)

        # Right: question prompt + answers (also used as the single column).
        self.question_panel = wx.Panel(self.content_splitter)
        self.question_panel.SetBackgroundColour(ExamColor.CONTENT_BG)
        qsz = wx.BoxSizer(wx.VERTICAL)
        self._top_spacer = qsz.AddStretchSpacer()  # centers single-column content
        self.prompt_view = MathView(self.question_panel, size=(-1, 120), exam=True)
        qsz.Add(self.prompt_view, 0, wx.EXPAND | wx.ALL, ui_scale.space(2))
        self.answer_panel = wx.ScrolledWindow(self.question_panel)
        self.answer_panel.SetScrollRate(0, 10)
        self.answer_panel.SetBackgroundColour(ExamColor.CONTENT_BG)
        self.answer_sizer = wx.BoxSizer(wx.VERTICAL)
        self.answer_panel.SetSizer(self.answer_sizer)
        qsz.Add(self.answer_panel, 0, wx.EXPAND | wx.ALL, ui_scale.space(2))
        self._bottom_spacer = qsz.AddStretchSpacer()
        self.question_panel.SetSizer(qsz)
        self.answer_panel.Bind(wx.EVT_SIZE, self._on_answer_panel_resize)

        self.content_splitter.Initialize(self.question_panel)
        box.Add(self.content_splitter, 1, wx.EXPAND)

        # Bottom-center gray directions pill (short instruction).
        pill_row = wx.BoxSizer(wx.HORIZONTAL)
        self.bottom_pill = wx.Panel(self.content_box)
        self.bottom_pill.SetBackgroundColour(ExamColor.DIRECTIONS_PILL)
        ppz = wx.BoxSizer(wx.HORIZONTAL)
        self.bottom_pill_label = wx.StaticText(self.bottom_pill, label="")
        self.bottom_pill_label.SetForegroundColour(ExamColor.DIRECTIONS_PILL_TEXT)
        self.bottom_pill_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT))
        ppz.Add(self.bottom_pill_label, 0, wx.ALL, ui_scale.space(2))
        self.bottom_pill.SetSizer(ppz)
        pill_row.AddStretchSpacer()
        pill_row.Add(self.bottom_pill, 0, wx.ALIGN_CENTER)
        pill_row.AddStretchSpacer()
        box.Add(pill_row, 0, wx.EXPAND | wx.ALL, ui_scale.space(3))

        self.content_box.SetSizer(box)

        # Floating calculator (hidden; toggled by Calc).
        self._calc_panel = CalculatorWidget(self)
        self._calc_panel.Hide()
        if hasattr(self._calc_panel, "set_on_transfer"):
            self._calc_panel.set_on_transfer(self._on_calc_transfer)

        self.SetSizer(main)

    # ── Public API ────────────────────────────────────────────────────

    def configure(self, section_state, question_bank, measure, mode="simulation",
                  exam=None):
        self._section_state = section_state
        self._question_bank = question_bank
        self._exam = exam
        self._measure = measure
        self._mode = mode

        from models.exam_session import SECTION_META
        sec_type = section_state.section_type
        _, sec_idx, _, _ = SECTION_META[sec_type]
        total_sections = getattr(section_state, "total_sections", 5) or 5
        self._sec_idx = sec_idx
        self._total_sections = total_sections

        # Ribbon: Calc only for quant. Mark/Review/Back/Next always.
        is_quant = measure == "quant"
        ids = ["exit"]
        if is_quant:
            ids.append("calc")
        ids += ["mark", "review", "help", "back", "next"]
        self.chrome.set_buttons(ids)
        self.chrome.set_on("exit", self._on_exit_section)
        self.chrome.set_on("calc", self._on_toggle_calc)
        self.chrome.set_on("mark", self._on_mark)
        self.chrome.set_on("review", self._on_review)
        self.chrome.set_on("help", self._on_help)
        self.chrome.set_on("back", self._on_prev)
        self.chrome.set_on("next", self._on_next)

        self.chrome.timer.set_time(section_state.time_limit)
        self.chrome.timer.set_on_expire(self._handle_time_expire)
        self.chrome.timer.set_on_tick(lambda elapsed: section_state.tick(elapsed))

        self._mixed_section = bool(getattr(section_state, "display_label", None))
        self._load_question(0)
        self.Layout()

    def start_timer(self):
        self.chrome.timer.start()

    def set_on_end_section(self, callback):
        self._on_end_section = callback

    def set_on_time_expire(self, callback):
        self._on_time_expire = callback

    def set_on_review(self, callback):
        self._on_review_callback = callback

    def set_on_exit_to_dashboard(self, callback):
        self._on_exit_to_dashboard = callback

    # ── Question loading ──────────────────────────────────────────────

    def _load_question(self, index):
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

        # Section / question label on the pink bar.
        if getattr(self, "_mixed_section", False):
            q_measure = (q.get("measure") or "").lower()
            self.chrome.enable_button("calc", q_measure == "quant") if self.chrome.has_button("calc") else None
            tag = "Verbal" if q_measure == "verbal" else (
                "Quant" if q_measure == "quant" else q_measure.title())
            self.chrome.set_section_label(
                f"{tag}  |  Question {index + 1} of {ss.total_questions}")
        else:
            self.chrome.set_section_label(
                f"Section {self._sec_idx} of {self._total_sections}  |  "
                f"Question {index + 1} of {ss.total_questions}")

        # Directions (top band + bottom pill).
        self._set_directions(q)

        # Stimulus / passage layout.
        split = _should_split(q)
        self._show_passage(q, split)

        # Prompt. QC builds the figure + common-info + two-column quantities.
        if subtype == "qc":
            prompt_html = self._qc_prompt_html(q)
        else:
            stim_inline = ""
            stim = q.get("stimulus") or {}
            # Inline the stimulus ONLY when it isn't shown in the left pane
            # (i.e. small geometry figures); data tables/charts go to the pane.
            if not split and stim.get("content"):
                stim_inline = (f'<div style="text-align:center;">'
                               f'{stim["content"]}</div>')
            prompt_html = stim_inline + f'<div class="prompt">{q["prompt"]}</div>'
        self.prompt_view.set_content_auto_height(prompt_html, min_h=60, max_h=360)

        # Single-column content is vertically centered; split panes top-align.
        center = not split
        self._top_spacer.SetProportion(1 if center else 0)
        self._bottom_spacer.SetProportion(2 if center else 0)

        self._sync_mark_button()
        self._build_answer_controls(q)
        saved = ss.get_response(qid)
        if saved:
            self._restore_response(saved)
        self._update_nav()
        self.content_box.Layout()
        self.Layout()

    def _set_directions(self, q):
        subtype = q["subtype"]
        top = _TOP_BAND.get(subtype, "")
        pill = _BOTTOM_PILL.get(subtype, "")
        if subtype == "tc":
            nblanks = self._tc_blank_count(q)
            if nblanks >= 2:
                top = ("For each blank select one entry from the corresponding "
                       "column of choices. Fill all blanks in the way that best "
                       "completes the text.")
                pill = "Select one entry from each column."
            else:
                top = ("Select one entry for the blank. Fill the blank in the "
                       "way that best completes the text.")
                pill = "Select one answer choice."
        self.top_band_label.SetLabel(top)
        self.top_band.Show(bool(top))
        self.bottom_pill_label.SetLabel(pill)
        self.bottom_pill.Show(bool(pill))
        self.top_band.GetContainingSizer().Layout()

    @staticmethod
    def _tc_blank_count(q):
        try:
            from services.scoring import normalize_tc_options
            blanks = {b for b, _c, _o in normalize_tc_options(q.get("options", []))}
            return len(blanks)
        except Exception:
            return 1

    def _show_passage(self, q, split):
        subtype = q["subtype"]
        stim = q.get("stimulus") or {}
        content = stim.get("content") or ""
        if not split:
            if self.content_splitter.IsSplit():
                self.content_splitter.Unsplit(self.passage_panel)
            self.passage_panel.Hide()
            return

        # Blue passage/data title bar text.
        self.passage_title.SetLabel(self._passage_title_text(q))
        if subtype == "rc_select_passage":
            self.passage_view.Hide()
            self.sip_panel.Show()
        else:
            self.sip_panel.Hide()
            self.passage_view.Show()
            # Data tables/charts shown at full pane width (.datafig makes a
            # small-intrinsic chart image fill the pane instead of rendering
            # tiny). Centered, with the data title already in the blue bar.
            self.passage_view.set_content(f'<div class="datafig">{content}</div>')
        self.passage_panel.Show()
        if not self.content_splitter.IsSplit():
            self.content_splitter.SplitVertically(
                self.passage_panel, self.question_panel, 0)
            wx.CallAfter(self._center_sash)

    def _passage_title_text(self, q):
        subtype = q["subtype"]
        if subtype == "data_interp" or _is_data_presentation(q):
            return "Question(s) based on the following data."
        return "Question is based on this passage."

    def _center_sash(self):
        if not self.content_splitter.IsSplit():
            return
        w = self.content_splitter.GetClientSize().width
        if w < 520:
            wx.CallLater(50, self._center_sash)
            return
        self.content_splitter.SetSashPosition(w // 2)
        self._rewrap_options()

    @staticmethod
    def _is_fraction_mode(na):
        mode = na.get("mode") or "auto"
        if mode == "fraction":
            return True
        if mode == "decimal":
            return False
        return na.get("numerator") is not None

    def _qc_prompt_html(self, q):
        """Figure (if any) + common info centered + two-column underlined
        Quantity A/B, parsed from the QC prompt HTML.

        Layout is driven by CSS classes (``qc-*`` in the MathView template),
        not inline ``style`` — the sanitizer empties inline styles, which used
        to collapse the table to content width and shove it left of center."""
        prompt = q["prompt"]
        stim = q.get("stimulus") or {}
        fig = (f'<div class="qc-fig">{stim["content"]}</div>'
               if stim.get("content") else "")
        a = re.search(r"Quantity\s*A\s*[:\-]\s*(.*?)(?=<p>\s*Quantity\s*B|$)",
                      prompt, re.IGNORECASE | re.DOTALL)
        b = re.search(r"Quantity\s*B\s*[:\-]\s*(.*?)(?=</p>|$)",
                      prompt, re.IGNORECASE | re.DOTALL)
        if not (a and b):
            return fig + f'<div class="prompt">{prompt}</div>'

        def _clean(s):
            return re.sub(r"</?p>", "", s).strip()

        # Strip any stray <p>/</p> from the common-info slice so it doesn't
        # leak an unbalanced tag into the centered div.
        common = _clean(re.sub(r"<p>\s*</p>", "", prompt[:a.start()]))
        qa, qb = _clean(a.group(1)), _clean(b.group(1))
        common_html = (f'<div class="qc-common">{common}</div>'
                       if common else "")
        return (
            f'{fig}{common_html}'
            f'<table class="qc-table"><tr>'
            f'<td class="qc-head"><u>Quantity A</u></td>'
            f'<td class="qc-head"><u>Quantity B</u></td>'
            f'</tr><tr>'
            f'<td class="qc-quantity">{qa}</td>'
            f'<td class="qc-quantity">{qb}</td>'
            f'</tr></table>'
        )

    # ── Answer controls ───────────────────────────────────────────────

    def _build_answer_controls(self, q):
        self.answer_sizer.Clear(True)
        self._answer_controls = []
        self._numeric_entry = None
        self._option_texts = []
        self._radio_group = None
        subtype = q["subtype"]
        options = q.get("options", [])

        if subtype == "rc_select_passage":
            self._build_select_in_passage(q, options)
        elif subtype in ("rc_single", "mcq_single", "qc", "data_interp"):
            for opt in options:
                label = (opt["text"] if subtype == "qc"
                         else f"{opt['label']}) {opt['text']}")
                ctrl = self._add_option(label, "radio", opt is options[0])
                self._answer_controls.append(("radio", opt["label"], ctrl))
        elif subtype in ("rc_multi", "mcq_multi", "se"):
            for opt in options:
                ctrl = self._add_option(f"{opt['label']}) {opt['text']}", "check", False)
                self._answer_controls.append(("check", opt["label"], ctrl))
        elif subtype == "tc":
            self._build_tc_columns(options)
        elif subtype == "numeric_entry":
            na = q.get("numeric_answer") or {}
            is_fraction = self._is_fraction_mode(na)
            prefix = na.get("prefix") or None
            suffix = na.get("suffix") or na.get("unit") or None
            self._numeric_entry = NumericEntry(self.answer_panel,
                                               fraction_mode=is_fraction,
                                               prefix=prefix, suffix=suffix)
            self._numeric_entry.set_on_change(lambda _: self._on_answer_change(None))
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.AddStretchSpacer()
            row.Add(self._numeric_entry, 0, wx.ALIGN_CENTER)
            row.AddStretchSpacer()
            self.answer_sizer.Add(row, 0, wx.EXPAND | wx.ALL, 8)
            if hasattr(self._calc_panel, "set_transfer_enabled"):
                self._calc_panel.set_transfer_enabled(not is_fraction)

        if subtype != "numeric_entry" and hasattr(self._calc_panel, "set_transfer_enabled"):
            self._calc_panel.set_transfer_enabled(False)

        self._rewrap_options()
        self.answer_panel.FitInside()
        # A ScrolledWindow added with proportion 0 reports a tiny best height, so
        # claim the content's height as the panel's min size — otherwise the
        # options collapse to a few px and look missing.
        ch = self.answer_sizer.GetMinSize().GetHeight()
        if ch > 0:
            self.answer_panel.SetMinSize((-1, ch))
        self.answer_panel.Layout()

    def _add_option(self, label_text, control_type, is_first):
        from widgets.latex_inline_text import latex_inline_to_text
        if control_type == "radio":
            ctrl = ExamChoice(self.answer_panel, shape="oval")
            if is_first or self._radio_group is None:
                self._radio_group = []
            self._radio_group.append(ctrl)
            ctrl.set_group(self._radio_group)
            ctrl.Bind(wx.EVT_RADIOBUTTON, self._on_answer_change)
        else:
            ctrl = ExamChoice(self.answer_panel, shape="square")
            ctrl.Bind(wx.EVT_CHECKBOX, self._on_answer_change)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(ctrl, 0, wx.RIGHT | wx.ALIGN_TOP, ui_scale.space(2))
        text = wx.StaticText(self.answer_panel, label=latex_inline_to_text(label_text))
        text.SetForegroundColour(ExamColor.TEXT)
        text.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
        text.Bind(wx.EVT_LEFT_DOWN, lambda _e, c=ctrl: c.activate())
        row.Add(text, 1, wx.EXPAND)
        self.answer_sizer.Add(row, 0, wx.EXPAND | wx.ALL, ui_scale.space(2))
        self._option_texts.append(text)
        return ctrl

    def _build_tc_columns(self, options):
        from services.scoring import normalize_tc_options
        blanks = {}
        for blank, choice, opt in normalize_tc_options(options):
            blanks.setdefault(blank, []).append((choice, opt["text"]))
        roman = {"blank1": "(i)", "blank2": "(ii)", "blank3": "(iii)"}
        cols = wx.BoxSizer(wx.HORIZONTAL)
        cols.AddStretchSpacer()
        self._tc_selected = {}
        multi = len(blanks) >= 2
        for blank_name, choices in sorted(blanks.items()):
            col = wx.BoxSizer(wx.VERTICAL)
            if multi:
                hdr = wx.StaticText(self.answer_panel,
                                    label=f"Blank {roman.get(blank_name, '')}".strip(),
                                    style=wx.ALIGN_CENTER)
                hdr.SetForegroundColour(ExamColor.TEXT)
                hdr.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
                col.Add(hdr, 0, wx.ALIGN_CENTER | wx.BOTTOM, ui_scale.space(1))
            # Bordered choice table (each cell is a bordered row).
            tbl_border = wx.Panel(self.answer_panel)
            tbl_border.SetBackgroundColour(ExamColor.TEXT)
            tsz = wx.BoxSizer(wx.VERTICAL)
            for choice_label, choice_text in choices:
                cell = self._make_tc_cell(tbl_border, blank_name, choice_label,
                                          choice_text)
                tsz.Add(cell, 0, wx.EXPAND | wx.ALL, 1)  # 1px black grid
            tbl_border.SetSizer(tsz)
            col.Add(tbl_border, 0)
            cols.Add(col, 0, wx.RIGHT, ui_scale.space(6))
        cols.AddStretchSpacer()
        self.answer_sizer.Add(cols, 0, wx.EXPAND | wx.TOP, ui_scale.space(4))

    def _make_tc_cell(self, parent, blank_name, choice_label, choice_text):
        from widgets.latex_inline_text import latex_inline_to_text
        cell = wx.Panel(parent)
        cell.SetBackgroundColour(ExamColor.CONTENT_BG)
        s = wx.BoxSizer(wx.HORIZONTAL)
        txt = wx.StaticText(cell, label=latex_inline_to_text(choice_text),
                            style=wx.ALIGN_CENTER)
        txt.SetForegroundColour(ExamColor.TEXT)
        txt.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
        s.Add(txt, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, ui_scale.space(2))
        cell.SetSizer(s)

        def _pick(_e, bn=blank_name, cl=choice_label, c=cell):
            self._on_tc_pick(bn, cl, c)
        for w in (cell, txt):
            w.Bind(wx.EVT_LEFT_DOWN, _pick)
            w.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self._answer_controls.append(("tc_cell", blank_name, choice_label, cell))
        return cell

    def _on_tc_pick(self, blank_name, choice_label, cell):
        self._tc_selected[blank_name] = choice_label
        for entry in self._answer_controls:
            if entry[0] == "tc_cell" and entry[1] == blank_name:
                sel = entry[2] == choice_label
                entry[3].SetBackgroundColour(
                    ExamColor.TC_HIGHLIGHT if sel else ExamColor.CONTENT_BG)
                for ch in entry[3].GetChildren():
                    ch.SetBackgroundColour(
                        ExamColor.TC_HIGHLIGHT if sel else ExamColor.CONTENT_BG)
                entry[3].Refresh()
        self._on_answer_change(None)

    def _build_select_in_passage(self, q, options):
        from widgets.latex_inline_text import latex_inline_to_text
        self.sip_sizer.Clear(True)
        self._answer_controls = []
        sentences = self._extract_passage_sentences(
            (q.get("stimulus") or {}).get("content") or "")
        avail = max(340, self.sip_panel.GetClientSize().width - 40)
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
            rs.Add(st, 0, wx.ALL, ui_scale.space(1))
            row.SetSizer(rs)

            def _pick(_e, lbl=label_idx, r=row):
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
                sel = entry[1] == str(label_idx)
                col = ExamColor.SELECT_IN_PASSAGE_HL if sel else ExamColor.CONTENT_BG
                entry[2].SetBackgroundColour(col)
                for ch in entry[2].GetChildren():
                    ch.SetBackgroundColour(col)
                entry[2].Refresh()
        self._sip_selected = str(label_idx)
        self._on_answer_change(None)

    def _on_answer_panel_resize(self, event):
        self._rewrap_options()
        event.Skip()

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
            return ({"selected_sentence": self._sip_selected}
                    if self._sip_selected else {})
        elif subtype in ("rc_single", "mcq_single", "qc", "data_interp"):
            for _ct, label, ctrl in self._answer_controls:
                if ctrl.GetValue():
                    return {"selected": [label]}
            return {}
        elif subtype in ("rc_multi", "mcq_multi", "se"):
            sel = [label for ct, label, ctrl in self._answer_controls
                   if ct == "check" and ctrl.GetValue()]
            return {"selected": sel} if sel else {}
        elif subtype == "tc":
            sel = dict(self._tc_selected)
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
            for _ct, label, ctrl in self._answer_controls:
                ctrl.SetValue(label in sel)
        elif subtype in ("rc_multi", "mcq_multi", "se"):
            sel = set(saved.get("selected", []))
            for _ct, label, ctrl in self._answer_controls:
                ctrl.SetValue(label in sel)
        elif subtype == "tc":
            sel = saved.get("selected", {})
            for blank, choice in sel.items():
                self._tc_selected[blank] = choice
            for entry in self._answer_controls:
                if entry[0] == "tc_cell":
                    _t, bn, cl, cell = entry
                    on = sel.get(bn) == cl
                    col = ExamColor.TC_HIGHLIGHT if on else ExamColor.CONTENT_BG
                    cell.SetBackgroundColour(col)
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
        if self.chrome.has_button("mark"):
            self.chrome.set_button_label("mark", "Marked" if marked else "Mark",
                                         icon="mark")

    def _on_mark(self):
        ss = self._section_state
        if ss:
            ss.toggle_mark()
            self._sync_mark_button()

    def _on_toggle_calc(self):
        self._calc_panel.Show(not self._calc_panel.IsShown())

    def _on_calc_transfer(self, value):
        if self._numeric_entry and not getattr(self._numeric_entry, "fraction_mode", False):
            try:
                self._numeric_entry.set_response({"value": value})
            except Exception:
                pass
            self._on_answer_change(None)

    def _on_help(self):
        menu = wx.Menu()
        d_item = menu.Append(wx.ID_ANY, "Question directions")
        r_item = menu.Append(wx.ID_ANY, "Report a problem with this question…")
        self.Bind(wx.EVT_MENU, lambda _e: self._show_directions_help(), d_item)
        self.Bind(wx.EVT_MENU, lambda _e: self._on_report_question(None), r_item)
        self.PopupMenu(menu)
        menu.Destroy()

    def _show_directions_help(self):
        subtype = (self._current_q or {}).get("subtype", "")
        msg = _TOP_BAND.get(subtype) or _BOTTOM_PILL.get(subtype, "Answer, then Next.")
        wx.MessageBox(msg, "Directions", wx.OK | wx.ICON_INFORMATION, self)

    def _on_prev(self):
        ss = self._section_state
        if ss and ss.current_index > 0:
            self._load_question(ss.current_index - 1)

    def _on_next(self):
        ss = self._section_state
        if ss is None:
            return
        if ss.current_index < ss.total_questions - 1:
            self._load_question(ss.current_index + 1)
        elif self._on_review_callback:
            self._on_review_callback()

    def _on_review(self):
        if self._on_review_callback:
            self._on_review_callback()

    def _on_exit_section(self):
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
                "Confirm End Section", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
            if dlg.ShowModal() != wx.ID_YES:
                dlg.Destroy()
                return
            dlg.Destroy()
        self.chrome.timer.stop()
        if self._on_end_section:
            self._on_end_section()

    def _handle_time_expire(self):
        self.chrome.timer.stop()
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
        self.chrome.enable_button("back", ss.current_index > 0)

    # ── Select-in-passage helpers ─────────────────────────────────────

    _SENT_TAG_RE = re.compile(
        r"<sent\s+id=['\"](\d+)['\"]\s*>(.*?)</sent>", re.IGNORECASE | re.DOTALL)

    @classmethod
    def _extract_passage_sentences(cls, passage_html):
        if not passage_html:
            return {}
        out = {}
        for m in cls._SENT_TAG_RE.finditer(passage_html):
            text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if text:
                out[m.group(1)] = text
        return out

    @staticmethod
    def _escape_html(text):
        return (str(text).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    # ── Report flow (preserved) ───────────────────────────────────────

    def _on_report_question(self, _):
        if not self._current_q:
            return
        qid = self._current_q.get("id")
        if qid is None:
            return
        pending = self._prepare_screenshot(qid)
        from widgets.flag_dialog import FlagQuestionDialog
        from services.question_bank import flag_question, auto_retire_flagged_questions
        dlg = FlagQuestionDialog(self, qid)
        if dlg.ShowModal() == wx.ID_OK:
            reason = dlg.get_reason()
            note = dlg.get_note()
            want_shot = dlg.wants_screenshot()
            if reason:
                if flag_question(qid, reason, note=note, user_id="local"):
                    auto_retire_flagged_questions()
                    attachment = self._finalize_screenshot(pending, enabled=want_shot)
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
            png, _m = capture_main_window_png(_wx.GetTopLevelWindows(),
                                              main_frame_cls=_MainFrame)
            return png
        except Exception as exc:  # pragma: no cover
            import logging
            logging.getLogger(__name__).warning(
                "screenshot capture failed for q%s: %s", qid, exc)
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
            on_clip = copy_png_to_clipboard(png_bytes)
            return {"captured": True, "clipboard": on_clip, "file_path": saved_path,
                    "error": None if on_clip else "clipboard-locked"}
        except Exception as exc:  # pragma: no cover
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
            combined = note or ""
            if reason:
                prefix = f"[reason: {reason}]"
                combined = f"{prefix}\n\n{combined}".strip() if combined else prefix
            url = build_issue_url(payload, combined)
        except Exception as exc:  # pragma: no cover
            import logging
            logging.getLogger(__name__).warning(
                "Failed to build issue URL for q%s: %s", qid, exc)
            wx.MessageBox("Thanks — your report was recorded locally.",
                          "Reported", wx.OK | wx.ICON_INFORMATION, parent=self)
            return
        extra = ""
        if attachment:
            if attachment.get("clipboard"):
                extra = ("\n\nA screenshot has been copied to your clipboard — "
                         "paste it into the GitHub issue with Cmd+V.")
            elif attachment.get("file_path"):
                extra = (f"\n\nScreenshot saved to:\n  {attachment['file_path']}")
        resp = wx.MessageBox(
            "Thanks — your report was recorded locally.\n\n"
            "Your browser will open a pre-filled GitHub issue. Click "
            "“Submit new issue” to send it." + extra + "\n\nOpen it now?",
            "Send report to the developer?", wx.YES_NO | wx.ICON_QUESTION, parent=self)
        if resp == wx.YES:
            try:
                webbrowser.open(url, new=2)
            except Exception as exc:  # pragma: no cover
                import logging
                logging.getLogger(__name__).warning("webbrowser.open failed: %s", exc)
