"""
Vocabulary flashcard screen — daily SRS-driven study session.

Two presentation modes, toggleable from the header:

* **Flashcard mode** (default): front = word, back = definition + synonyms
  + root + mnemonic. Reveal button → 1-4 rating → ``services.srs.update_review``.

* **Context mode** (P3.S2): draws from ``VocabContextItem``. For each word in
  the SRS queue, show a ~120-word GRE-register mini-passage, an inference
  question, and 4 options. After the user picks, reveal the correct answer +
  explanation and take a 1-4 SRS rating that flows through the SAME
  ``update_review`` path as flashcard mode — the passage is an alternate
  presentation of the word, not a parallel SRS universe. On-demand LLM
  generation lives in ``services.vocab_context_gen``; cache hits return
  instantly, misses spawn an async fetch while the UI shows a "Generating…"
  message so the main thread never blocks.
"""
import json
import threading

import wx

from services.srs import (
    daily_session, update_review, get_or_create_review, stats,
)
from models.database import VocabWord
from widgets import ui_scale
from widgets.theme import Color


class VocabScreen(wx.Panel):
    """Flashcard study screen with rich back-of-card content."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Color.BG_PAGE)
        self._on_back = None
        self._queue = []
        self._current_card = None
        self._current_word = None
        self._showing_back = False

        # Context-mode state
        self._context_mode = False
        self._current_context_item = None  # VocabContextItem or None
        self._current_options = []        # list[str] in display order
        self._option_buttons = []         # wx.RadioButton handles, same order
        self._context_answered = False    # True after the user picks
        self._context_loading = False     # True while an LLM fetch is in-flight

        self._build_ui()

    # ── UI construction ────────────────────────────────────────────────

    def _build_ui(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Header
        hdr = wx.BoxSizer(wx.HORIZONTAL)
        self.back_btn = wx.Button(self, label="← Back to Today",
                                  size=(-1, ui_scale.space(9)))
        self.back_btn.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.back_btn.Bind(wx.EVT_BUTTON, lambda _: self._on_back() if self._on_back else None)
        hdr.Add(self.back_btn, 0, wx.ALL, ui_scale.space(2))

        # Context-mode toggle (checkbox — compact + keyboard-friendly)
        self.context_toggle = wx.CheckBox(self, label="Context mode")
        self.context_toggle.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                           wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.context_toggle.SetForegroundColour(Color.TEXT_SECONDARY)
        self.context_toggle.Bind(wx.EVT_CHECKBOX, self._on_context_toggle)
        hdr.Add(self.context_toggle, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.LEFT, ui_scale.space(3))

        self.session_info = wx.StaticText(self, label="")
        self.session_info.SetForegroundColour(Color.TEXT_SECONDARY)
        self.session_info.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                          wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        hdr.Add(self.session_info, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, ui_scale.space(3))
        sizer.Add(hdr, 0, wx.EXPAND)

        # Card area: word at top, then scrollable detail panel
        self.card_panel = wx.Panel(self)
        self.card_panel.SetBackgroundColour(Color.BG_SURFACE)
        card_sizer = wx.BoxSizer(wx.VERTICAL)
        card_sizer.AddSpacer(ui_scale.space(6))

        # Word (front-of-card)
        self.word_label = wx.StaticText(self.card_panel, label="")
        self.word_label.SetFont(wx.Font(ui_scale.text_display(), wx.FONTFAMILY_DEFAULT,
                                        wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.word_label.SetForegroundColour(Color.TEXT_PRIMARY)
        card_sizer.Add(self.word_label, 0, wx.ALIGN_CENTER | wx.TOP, ui_scale.space(2))

        self.pos_label = wx.StaticText(self.card_panel, label="")
        self.pos_label.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                       wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL))
        self.pos_label.SetForegroundColour(Color.TEXT_SECONDARY)
        card_sizer.Add(self.pos_label, 0, wx.ALIGN_CENTER | wx.TOP, ui_scale.space(1))

        card_sizer.AddSpacer(ui_scale.space(5))

        # Back-of-card scrolled content
        self.detail_panel = wx.ScrolledWindow(self.card_panel, style=wx.VSCROLL)
        self.detail_panel.SetScrollRate(0, 12)
        self.detail_panel.SetBackgroundColour(Color.BG_SURFACE)
        self.detail_sizer = wx.BoxSizer(wx.VERTICAL)
        self.detail_panel.SetSizer(self.detail_sizer)
        card_sizer.Add(self.detail_panel, 1, wx.EXPAND |
                       wx.LEFT | wx.RIGHT, ui_scale.space(20))

        self.card_panel.SetSizer(card_sizer)
        sizer.Add(self.card_panel, 1, wx.EXPAND | wx.ALL, ui_scale.space(3))

        # Action buttons
        actions = wx.BoxSizer(wx.HORIZONTAL)
        actions.AddStretchSpacer()

        btn_h = ui_scale.space(11)
        btn_w = ui_scale.font_size(140)

        self.reveal_btn = wx.Button(self, label="Reveal Definition",
                                    size=(ui_scale.font_size(220), btn_h))
        self.reveal_btn.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                         wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.reveal_btn.Bind(wx.EVT_BUTTON, self._on_reveal)
        actions.Add(self.reveal_btn, 0, wx.ALL, ui_scale.space(2))

        # "Submit Answer" — shown only in context mode before the user commits
        self.submit_btn = wx.Button(self, label="Submit Answer",
                                    size=(ui_scale.font_size(220), btn_h))
        self.submit_btn.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                        wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.submit_btn.Bind(wx.EVT_BUTTON, self._on_submit_context)
        self.submit_btn.Hide()
        actions.Add(self.submit_btn, 0, wx.ALL, ui_scale.space(2))

        for label, response in [("Again", 1), ("Hard", 2), ("Good", 3), ("Easy", 4)]:
            btn = wx.Button(self, label=label, size=(btn_w, btn_h))
            btn.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                 wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
            btn.Bind(wx.EVT_BUTTON, lambda _, r=response: self._respond(r))
            btn.Hide()
            actions.Add(btn, 0, wx.ALL, ui_scale.space(2))
            setattr(self, f"_btn_{response}", btn)

        actions.AddStretchSpacer()
        sizer.Add(actions, 0, wx.EXPAND | wx.BOTTOM, ui_scale.space(4))

        self.SetSizer(sizer)

    def set_on_back(self, handler):
        self._on_back = handler

    # ── Session lifecycle ──────────────────────────────────────────────

    def start_session(self, new_count: int = 20):
        """Build today's queue: due cards + new cards."""
        due, new = daily_session(new_count=new_count)

        self._queue = []
        for card in due:
            try:
                w = VocabWord.get_by_id(card.word_id)
                self._queue.append(("review", card, w))
            except VocabWord.DoesNotExist:
                continue
        for w in new:
            self._queue.append(("new", None, w))

        if not self._queue:
            self._show_empty_state()
            return

        self._next_card()

    def _show_empty_state(self):
        # Differentiate "you've reviewed everything available today" from
        # "the vocab module isn't populated yet" — same screen, different
        # next-step.
        try:
            s = stats()
            total = s.get("total_words", 0)
        except Exception:
            total = 0

        if total == 0:
            title = "No vocabulary in the bank"
            body = ("The vocab module appears empty. Run "
                    "`scripts/import_vocab.py` to populate it from "
                    "data/external/.")
        else:
            title = "All caught up!"
            body = "No cards due today. Come back tomorrow."

        # Hide all action buttons FIRST so the empty-state isn't paired
        # with a stale Reveal Definition button.
        self._hide_response_buttons()
        self.reveal_btn.Hide()
        self.submit_btn.Hide()

        self.word_label.SetLabel(title)
        self.pos_label.SetLabel("")
        self.session_info.SetLabel("")
        self._clear_detail()
        msg = wx.StaticText(self.detail_panel, label=body)
        msg.SetForegroundColour(Color.TEXT_SECONDARY)
        msg.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.detail_sizer.Add(msg, 0, wx.ALIGN_CENTER | wx.ALL, ui_scale.space(3))
        self.card_panel.Layout()
        self.Layout()

    def _next_card(self):
        if not self._queue:
            self.word_label.SetLabel("Session complete!")
            self.pos_label.SetLabel("")
            self.session_info.SetLabel("")
            self._clear_detail()
            self._hide_response_buttons()
            self.reveal_btn.Hide()
            self.submit_btn.Hide()
            msg = wx.StaticText(self.detail_panel,
                                label="Great job! Come back tomorrow for more.")
            msg.SetForegroundColour(Color.TEXT_SECONDARY)
            msg.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            self.detail_sizer.Add(msg, 0, wx.ALIGN_CENTER | wx.ALL, ui_scale.space(3))
            self.card_panel.Layout()
            self.Layout()
            # Streak: vocab session counted as today's activity.
            try:
                from services.streak import record_activity
                record_activity()
            except Exception:
                pass
            return

        kind, card, word = self._queue[0]
        self._current_card = card
        self._current_word = word
        self._showing_back = False
        self._current_context_item = None
        self._current_options = []
        self._option_buttons = []
        self._context_answered = False
        self._context_loading = False

        if self._context_mode:
            self._present_context_card()
        else:
            self._present_flashcard()

    def _present_flashcard(self):
        """Classic flashcard front-of-card layout."""
        word = self._current_word
        self.word_label.SetLabel(word.word)
        self.pos_label.SetLabel(word.part_of_speech or "")

        # Clear any previous back-of-card content
        self._clear_detail()

        s = stats()
        remaining = len(self._queue)
        self.session_info.SetLabel(
            f"Session: {remaining} remaining  |  Due today: {s['due_today']}  |  Mastered: {s['mastered']}"
        )

        self.reveal_btn.Show()
        self.submit_btn.Hide()
        self._hide_response_buttons()
        self.card_panel.Layout()
        self.Layout()

    def _present_context_card(self):
        """Context-mode layout: passage + inference question + options.

        If the passage for ``self._current_word`` is already cached we
        render immediately; otherwise we show "Generating passage…" and
        spawn a worker thread that calls the LLM and posts back via
        ``wx.CallAfter``. Misses are rare once the user has practiced a
        word once — the first miss is the only network-bound case."""
        word = self._current_word
        self.word_label.SetLabel(word.word)
        self.pos_label.SetLabel(word.part_of_speech or "")
        self._clear_detail()

        s = stats()
        remaining = len(self._queue)
        self.session_info.SetLabel(
            f"Session: {remaining} remaining  |  Context mode  |  "
            f"Due today: {s['due_today']}"
        )
        self.reveal_btn.Hide()
        self.submit_btn.Hide()
        self._hide_response_buttons()

        from services.vocab_context_gen import _cached
        # Difficulty must match the async-generation path below (which
        # uses the ``get_or_generate`` default), or the cache probe
        # silently misses every entry.
        cached = _cached(word.word, "mid")
        if cached is not None:
            self._render_context_item(cached)
            return

        # Cache miss — async generate. Show a placeholder so the UI
        # doesn't freeze.
        self._context_loading = True
        placeholder = wx.StaticText(
            self.detail_panel,
            label=f"Generating a GRE-register passage for "
                  f"“{word.word}”…",
        )
        placeholder.SetForegroundColour(Color.TEXT_SECONDARY)
        placeholder.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                    wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL))
        self.detail_sizer.Add(placeholder, 0, wx.ALIGN_CENTER | wx.ALL,
                              ui_scale.space(3))
        self.card_panel.Layout()
        self.Layout()

        target_word = word.word

        def _worker():
            try:
                from services.vocab_context_gen import get_or_generate
                item = get_or_generate(target_word)
            except Exception:
                item = None
            wx.CallAfter(self._on_context_generated, target_word, item)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_context_generated(self, word_text, item):
        """Main-thread callback after the LLM returns (or fails)."""
        # Guard: the user may have already advanced to the next card.
        if (not self._context_mode or self._current_word is None
                or self._current_word.word != word_text):
            return
        self._context_loading = False
        self._clear_detail()
        if item is None:
            # Graceful fallback — revert to flashcard front for this card.
            err = wx.StaticText(
                self.detail_panel,
                label=(
                    "Couldn't generate a context passage right now. "
                    "Showing the flashcard back instead."
                ),
            )
            err.SetForegroundColour(Color.TEXT_SECONDARY)
            err.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL))
            self.detail_sizer.Add(err, 0, wx.ALIGN_CENTER | wx.ALL,
                                  ui_scale.space(3))
            # Fall through to flashcard reveal so the user still gets the
            # definition + a rating path.
            self.reveal_btn.Show()
            self.card_panel.Layout()
            self.Layout()
            return
        self._render_context_item(item)

    def _render_context_item(self, item):
        """Paint the passage + question + options for a loaded context item.

        Option order is stable (correct answer + 3 distractors, in whatever
        order the LLM produced them). For a production launch we'd shuffle
        per render; leaving deterministic for now so the tests can assert
        behaviour without mocking randomness."""
        self._current_context_item = item
        self._current_options = item.get_options()
        self._option_buttons = []

        # Passage block
        passage_lbl = wx.StaticText(self.detail_panel, label=item.passage_text)
        passage_lbl.SetForegroundColour(Color.TEXT_PRIMARY)
        passage_lbl.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        passage_lbl.Wrap(ui_scale.font_size(700))
        self.detail_sizer.Add(passage_lbl, 0,
                              wx.LEFT | wx.RIGHT | wx.TOP,
                              ui_scale.space(2))

        # Question block
        q_lbl = wx.StaticText(self.detail_panel, label=item.question_text)
        q_lbl.SetForegroundColour(Color.TEXT_PRIMARY)
        q_lbl.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                              wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        q_lbl.Wrap(ui_scale.font_size(700))
        self.detail_sizer.Add(q_lbl, 0,
                              wx.LEFT | wx.RIGHT | wx.TOP,
                              ui_scale.space(3))

        # Option radio buttons
        for idx, option_text in enumerate(self._current_options):
            style = wx.RB_GROUP if idx == 0 else 0
            rb = wx.RadioButton(self.detail_panel, label=option_text, style=style)
            rb.SetForegroundColour(Color.TEXT_PRIMARY)
            rb.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                               wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            self.detail_sizer.Add(rb, 0,
                                  wx.LEFT | wx.RIGHT | wx.TOP,
                                  ui_scale.space(1))
            self._option_buttons.append(rb)

        self.submit_btn.Show()
        self._hide_response_buttons()
        self.detail_panel.FitInside()
        self.card_panel.Layout()
        self.Layout()

    # ── Event handlers ─────────────────────────────────────────────────

    def _on_context_toggle(self, _):
        """Flip between flashcard and context mode. If a card is in flight
        we reset the current-card presentation to the new mode; SRS state
        is unaffected because we only call ``update_review`` on an explicit
        rating click."""
        self._context_mode = bool(self.context_toggle.GetValue())
        if self._queue and self._current_word is not None:
            # Re-present the same card in the new mode.
            if self._context_mode:
                self._present_context_card()
            else:
                self._present_flashcard()

    def _clear_detail(self):
        """Remove all children from the detail panel."""
        self.detail_sizer.Clear(True)

    def _add_section(self, title, body, color=(200, 220, 255)):
        """Add a labeled section to the back-of-card."""
        if not body:
            return
        title_lbl = wx.StaticText(self.detail_panel, label=title)
        title_lbl.SetForegroundColour(wx.Colour(*color))
        title_lbl.SetFont(wx.Font(ui_scale.text_sm(), wx.FONTFAMILY_DEFAULT,
                                  wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.detail_sizer.Add(title_lbl, 0, wx.LEFT | wx.TOP, ui_scale.space(2))

        body_lbl = wx.StaticText(self.detail_panel, label=body)
        body_lbl.SetForegroundColour(Color.TEXT_PRIMARY)
        body_lbl.SetFont(wx.Font(ui_scale.text_md(), wx.FONTFAMILY_DEFAULT,
                                 wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        body_lbl.Wrap(ui_scale.font_size(700))
        self.detail_sizer.Add(body_lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM,
                              ui_scale.space(2))

    def _on_reveal(self, _):
        if not self._current_word:
            return

        w = self._current_word
        # Definition
        self._add_section("DEFINITION", w.definition or "(no definition)",
                          color=(150, 220, 150))

        # Example sentences
        try:
            examples = w.get_examples() if hasattr(w, "get_examples") else []
        except Exception:
            examples = []
        if examples:
            joined = "\n\n".join(f"• {ex}" for ex in examples[:3])
            self._add_section("EXAMPLE SENTENCES", joined, color=(180, 200, 240))

        # Synonyms
        try:
            syns = w.get_synonyms() if hasattr(w, "get_synonyms") else []
        except Exception:
            syns = []
        if syns:
            self._add_section("SYNONYMS", ", ".join(syns), color=(220, 200, 150))

        # Antonyms (if stored separately)
        try:
            ants = json.loads(w.antonyms) if w.antonyms else []
        except Exception:
            ants = []
        if ants:
            self._add_section("ANTONYMS", ", ".join(ants), color=(240, 180, 180))

        # Root analysis
        if w.root_analysis:
            self._add_section("ROOT ANALYSIS", w.root_analysis,
                              color=(200, 180, 230))

        # Mnemonic
        if w.mnemonic:
            self._add_section("MEMORY HOOK", w.mnemonic, color=(255, 220, 150))

        # Themes
        try:
            themes = w.get_themes() if hasattr(w, "get_themes") else []
        except Exception:
            themes = []
        if themes:
            self._add_section("THEMES", ", ".join(themes), color=(180, 220, 200))

        self._showing_back = True
        self.reveal_btn.Hide()
        for response in (1, 2, 3, 4):
            getattr(self, f"_btn_{response}").Show()

        self.detail_panel.FitInside()
        self.card_panel.Layout()
        self.Layout()

    def _on_submit_context(self, _):
        """User clicked Submit in context mode.

        We reveal the correct answer + a short explanation, then surface
        the 1-4 SRS rating buttons so the user self-grades their
        comprehension. The grade flows through the SAME vocab SRS path
        as flashcard mode (``update_review``) so both modes share one
        stability/difficulty history per word."""
        if self._current_context_item is None:
            return
        # Find selected radio
        picked = None
        for rb in self._option_buttons:
            if rb.GetValue():
                picked = rb.GetLabel()
                break
        if picked is None:
            # No selection — do nothing (user can still pick).
            return

        item = self._current_context_item
        is_correct = (picked == item.correct_answer)
        self._context_answered = True

        header = "Correct!" if is_correct else "Not quite."
        color = (150, 220, 150) if is_correct else (240, 180, 180)
        self._add_section(header,
                          f"Correct answer: {item.correct_answer}",
                          color=color)
        # Brief coaching line — show the word's definition as the anchor
        # so the user links passage meaning to dictionary meaning.
        w = self._current_word
        if w and w.definition:
            self._add_section("DEFINITION", w.definition,
                              color=(180, 200, 240))

        self._add_section(
            "RATE YOUR CONTEXT COMPREHENSION",
            "1=Again, 2=Hard, 3=Good, 4=Easy. Rating updates the SRS "
            "interval for this word.",
            color=(200, 200, 200),
        )

        self.submit_btn.Hide()
        for response in (1, 2, 3, 4):
            getattr(self, f"_btn_{response}").Show()
        self.detail_panel.FitInside()
        self.card_panel.Layout()
        self.Layout()

    def _respond(self, response: int):
        if not self._current_word:
            return
        if self._current_card is None:
            self._current_card = get_or_create_review(self._current_word)
        update_review(self._current_card, response)
        self._queue.pop(0)
        self._next_card()

    def _hide_response_buttons(self):
        for response in (1, 2, 3, 4):
            getattr(self, f"_btn_{response}").Hide()
