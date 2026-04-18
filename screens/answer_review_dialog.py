"""
Modal that walks through every question in the just-finished section,
showing the prompt, the user's answer, the correct answer, and the
stored explanation. Reachable from ResultsScreen via the
"📖 Review Answers + Explanations" button.

Pure presentation — receives a list of detail dicts already enriched
with prompt/options/user_response/explanation by
`main_frame._build_question_details`.
"""
import wx
import wx.lib.scrolledpanel as scrolled

from widgets.theme import Color


def _format_user_answer(detail: dict) -> str:
    """Render the user's response payload as a short human string.

    Mirrors `services.scoring._normalize_*` shapes — kept here as
    presentation rather than imported because the scoring engine's
    internals shouldn't leak into UI code.
    """
    resp = detail.get("user_response")
    if resp is None:
        return "— Unanswered"
    subtype = detail.get("subtype", "")

    if subtype == "numeric_entry":
        if resp.get("value"):
            return str(resp["value"])
        if resp.get("numerator") is not None and resp.get("denominator") is not None:
            return f"{resp['numerator']}/{resp['denominator']}"
        return "— Unanswered"

    if subtype == "rc_select_passage":
        s = resp.get("selected_sentence")
        return f"Sentence {s}" if s is not None else "— Unanswered"

    if subtype == "tc":
        sel = resp.get("selected") or {}
        if not sel:
            return "— Unanswered"
        parts = [f"{blank}: {label}" for blank, label in sorted(sel.items())]
        return ", ".join(parts)

    sel = resp.get("selected") or []
    if not sel:
        return "— Unanswered"
    return ", ".join(sel)


def _format_correct_answer(detail: dict) -> str:
    """Build the canonical correct answer string from option flags."""
    subtype = detail.get("subtype", "")
    if subtype == "numeric_entry":
        na = detail.get("numeric_answer") or {}
        if na.get("exact_value") is not None:
            return str(na["exact_value"])
        if na.get("numerator") is not None and na.get("denominator") is not None:
            return f"{na['numerator']}/{na['denominator']}"
        return "—"

    options = detail.get("options") or []
    correct = [o for o in options if o.get("is_correct")]
    if not correct:
        return "—"
    if subtype == "tc":
        # Group by blank prefix (blank1_A → blank 1: A).
        from collections import defaultdict
        by_blank = defaultdict(list)
        for o in correct:
            label = o.get("label", "")
            if "_" in label:
                blank, choice = label.split("_", 1)
                by_blank[blank].append(choice)
            else:
                by_blank["blank1"].append(label)
        return ", ".join(
            f"{b}: {','.join(by_blank[b])}" for b in sorted(by_blank)
        )
    # Show label) text for each correct option.
    return "; ".join(
        f"{o.get('label', '?')}) {(o.get('text') or '')[:80]}"
        for o in correct
    )


class AnswerReviewDialog(wx.Dialog):
    """Scrollable per-question review with prompts, answers, explanations."""

    def __init__(self, parent, question_details: list):
        super().__init__(
            parent,
            title="Section Review — Answers & Explanations",
            size=(880, 720),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._details = question_details or []
        self.SetBackgroundColour(Color.BG_PAGE)
        self._build_ui()

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # Header summary
        n = len(self._details)
        n_correct = sum(1 for d in self._details if d.get("is_correct") is True)
        n_wrong = sum(1 for d in self._details if d.get("is_correct") is False)
        n_blank = n - n_correct - n_wrong
        header = wx.StaticText(
            self,
            label=(
                f"{n} questions  •  "
                f"{n_correct} correct  •  "
                f"{n_wrong} incorrect  •  "
                f"{n_blank} unanswered"
            ),
        )
        f = header.GetFont()
        f.SetPointSize(f.GetPointSize() + 1)
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        header.SetFont(f)
        outer.Add(header, 0, wx.ALL, 12)

        # Scrolling panel of cards
        self._scroll = scrolled.ScrolledPanel(self, style=wx.TAB_TRAVERSAL)
        self._scroll.SetBackgroundColour(Color.BG_PAGE)
        self._scroll.SetupScrolling(scroll_x=False, scroll_y=True, rate_y=24)
        scroll_sizer = wx.BoxSizer(wx.VERTICAL)
        for i, d in enumerate(self._details, 1):
            scroll_sizer.Add(self._make_card(self._scroll, i, d),
                             0, wx.EXPAND | wx.ALL, 8)
        self._scroll.SetSizer(scroll_sizer)
        outer.Add(self._scroll, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        # Close button
        btn_sizer = wx.StdDialogButtonSizer()
        close_btn = wx.Button(self, wx.ID_OK, "Close")
        close_btn.SetDefault()
        btn_sizer.AddButton(close_btn)
        btn_sizer.Realize()
        outer.Add(btn_sizer, 0, wx.ALL | wx.ALIGN_RIGHT, 12)

        self.SetSizer(outer)

    def _make_card(self, parent, idx: int, detail: dict) -> wx.Panel:
        """Render one question card."""
        card = wx.Panel(parent, style=wx.BORDER_SIMPLE)
        card.SetBackgroundColour(Color.BG_SURFACE)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Status pill + meta
        is_correct = detail.get("is_correct")
        if is_correct is True:
            pill_text, pill_bg = "✓ Correct", Color.MASTERY[4]
        elif is_correct is False:
            pill_text, pill_bg = "✗ Incorrect", Color.MASTERY[1]
        else:
            pill_text, pill_bg = "— Unanswered", Color.BG_PAGE

        meta_row = wx.BoxSizer(wx.HORIZONTAL)
        num_label = wx.StaticText(card, label=f"Q{idx}")
        f = num_label.GetFont()
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        f.SetPointSize(f.GetPointSize() + 2)
        num_label.SetFont(f)
        meta_row.Add(num_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        pill = wx.StaticText(card, label=f"  {pill_text}  ")
        pill.SetBackgroundColour(pill_bg)
        pill.SetForegroundColour(Color.TEXT_INVERSE if is_correct is not None else Color.TEXT_PRIMARY)
        meta_row.Add(pill, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        meta = wx.StaticText(
            card,
            label=(
                f"{detail.get('measure', '').title()} • "
                f"{detail.get('subtype', '')} • "
                f"qid={detail.get('question_id', '?')}"
            ),
        )
        meta.SetForegroundColour(Color.TEXT_SECONDARY)
        meta_row.Add(meta, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(meta_row, 0, wx.ALL, 10)

        # Stimulus (if present, truncated)
        stim = detail.get("stimulus")
        if stim and stim.get("content"):
            stim_label = wx.StaticText(card, label="Passage:")
            stim_label.SetForegroundColour(Color.TEXT_SECONDARY)
            sizer.Add(stim_label, 0, wx.LEFT | wx.RIGHT, 12)
            stim_body = wx.StaticText(card, label=(stim["content"] or "")[:600])
            stim_body.Wrap(800)
            sizer.Add(stim_body, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        # Prompt
        prompt = detail.get("prompt") or "(no prompt)"
        prompt_text = wx.StaticText(card, label=prompt)
        prompt_text.Wrap(820)
        f = prompt_text.GetFont()
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        prompt_text.SetFont(f)
        sizer.Add(prompt_text, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)

        # Options listing
        options = detail.get("options") or []
        if options:
            sizer.Add(wx.StaticLine(card), 0, wx.EXPAND | wx.ALL, 6)
            for o in options:
                label = o.get("label", "")
                text = o.get("text", "")
                marker = "  "
                if o.get("is_correct"):
                    marker = "✓ "
                opt_str = f"{marker}{label}) {text}"
                line = wx.StaticText(card, label=opt_str)
                if o.get("is_correct"):
                    f = line.GetFont()
                    f.SetWeight(wx.FONTWEIGHT_BOLD)
                    line.SetFont(f)
                    line.SetForegroundColour(Color.SUCCESS)
                line.Wrap(820)
                sizer.Add(line, 0, wx.LEFT | wx.RIGHT, 18)

        sizer.Add(wx.StaticLine(card), 0, wx.EXPAND | wx.ALL, 6)

        # Answer rows
        your_label = wx.StaticText(
            card, label=f"Your answer: {_format_user_answer(detail)}",
        )
        your_label.SetForegroundColour(
            Color.SUCCESS if is_correct is True else
            Color.DANGER if is_correct is False else
            Color.TEXT_SECONDARY
        )
        sizer.Add(your_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)

        correct_label = wx.StaticText(
            card, label=f"Correct answer: {_format_correct_answer(detail)}",
        )
        correct_label.SetForegroundColour(Color.SUCCESS)
        f = correct_label.GetFont()
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        correct_label.SetFont(f)
        correct_label.Wrap(820)
        sizer.Add(correct_label, 0, wx.LEFT | wx.RIGHT, 12)

        # Explanation
        expl = (detail.get("explanation") or "").strip()
        if expl:
            sizer.Add(wx.StaticLine(card), 0, wx.EXPAND | wx.ALL, 6)
            heading = wx.StaticText(card, label="EXPLANATION")
            heading.SetForegroundColour(Color.ACCENT)
            f = heading.GetFont()
            f.SetWeight(wx.FONTWEIGHT_BOLD)
            heading.SetFont(f)
            sizer.Add(heading, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
            body = wx.StaticText(card, label=expl)
            body.Wrap(820)
            sizer.Add(body, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        else:
            no_expl = wx.StaticText(
                card,
                label="(No stored explanation. Use the AI Tutor inside the "
                      "section to generate one on demand.)",
            )
            no_expl.SetForegroundColour(Color.TEXT_SECONDARY)
            sizer.Add(no_expl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        card.SetSizer(sizer)
        return card
