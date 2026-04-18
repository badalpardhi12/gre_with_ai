"""
Vocabulary flashcard screen — daily SRS-driven study session.

Front of card: word + part of speech.
Back of card: definition, examples, synonyms, antonyms, root analysis, mnemonic.
"""
import json
import wx

from services.srs import (
    daily_session, update_review, get_or_create_review, stats,
)
from models.database import VocabWord
from widgets import ui_scale


class VocabScreen(wx.Panel):
    """Flashcard study screen with rich back-of-card content."""

    def __init__(self, parent):
        super().__init__(parent)
        self._on_back = None
        self._queue = []
        self._current_card = None
        self._current_word = None
        self._showing_back = False
        self._build_ui()

    def _build_ui(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Header
        hdr = wx.BoxSizer(wx.HORIZONTAL)
        self.back_btn = wx.Button(self, label="← Back to Dashboard",
                                  size=(-1, ui_scale.font_size(36)))
        self.back_btn.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.back_btn.Bind(wx.EVT_BUTTON, lambda _: self._on_back() if self._on_back else None)
        hdr.Add(self.back_btn, 0, wx.ALL, 8)

        self.session_info = wx.StaticText(self, label="")
        self.session_info.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                          wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        hdr.Add(self.session_info, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        sizer.Add(hdr, 0, wx.EXPAND)
        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND)

        # Card area: word at top, then scrollable detail panel
        self.card_panel = wx.Panel(self)
        self.card_panel.SetBackgroundColour(wx.Colour(40, 40, 40))
        card_sizer = wx.BoxSizer(wx.VERTICAL)
        card_sizer.AddSpacer(ui_scale.font_size(24))

        # Word (front-of-card)
        self.word_label = wx.StaticText(self.card_panel, label="")
        self.word_label.SetFont(wx.Font(ui_scale.font_size(40), wx.FONTFAMILY_DEFAULT,
                                        wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.word_label.SetForegroundColour(wx.Colour(255, 255, 255))
        card_sizer.Add(self.word_label, 0, wx.ALIGN_CENTER | wx.TOP, 6)

        self.pos_label = wx.StaticText(self.card_panel, label="")
        self.pos_label.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                       wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL))
        self.pos_label.SetForegroundColour(wx.Colour(180, 180, 180))
        card_sizer.Add(self.pos_label, 0, wx.ALIGN_CENTER | wx.TOP, 4)

        card_sizer.AddSpacer(ui_scale.font_size(20))

        # Back-of-card scrolled content
        self.detail_panel = wx.ScrolledWindow(self.card_panel, style=wx.VSCROLL)
        self.detail_panel.SetScrollRate(0, 12)
        self.detail_panel.SetBackgroundColour(wx.Colour(40, 40, 40))
        self.detail_sizer = wx.BoxSizer(wx.VERTICAL)
        self.detail_panel.SetSizer(self.detail_sizer)
        card_sizer.Add(self.detail_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 80)

        self.card_panel.SetSizer(card_sizer)
        sizer.Add(self.card_panel, 1, wx.EXPAND | wx.ALL, 12)

        # Action buttons
        actions = wx.BoxSizer(wx.HORIZONTAL)
        actions.AddStretchSpacer()

        btn_h = ui_scale.font_size(44)
        btn_w = ui_scale.font_size(140)

        self.reveal_btn = wx.Button(self, label="Reveal Definition",
                                    size=(ui_scale.font_size(220), btn_h))
        self.reveal_btn.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                         wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.reveal_btn.Bind(wx.EVT_BUTTON, self._on_reveal)
        actions.Add(self.reveal_btn, 0, wx.ALL, 6)

        for label, response in [("Again", 1), ("Hard", 2), ("Good", 3), ("Easy", 4)]:
            btn = wx.Button(self, label=label, size=(btn_w, btn_h))
            btn.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                 wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
            btn.Bind(wx.EVT_BUTTON, lambda _, r=response: self._respond(r))
            btn.Hide()
            actions.Add(btn, 0, wx.ALL, 6)
            setattr(self, f"_btn_{response}", btn)

        actions.AddStretchSpacer()
        sizer.Add(actions, 0, wx.EXPAND | wx.BOTTOM, 14)

        self.SetSizer(sizer)

    def set_on_back(self, handler):
        self._on_back = handler

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
        self.word_label.SetLabel("All caught up!")
        self.pos_label.SetLabel("")
        self.session_info.SetLabel("")
        self._clear_detail()
        msg = wx.StaticText(self.detail_panel,
                            label="No cards due today. Come back tomorrow.")
        msg.SetForegroundColour(wx.Colour(220, 220, 220))
        msg.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.detail_sizer.Add(msg, 0, wx.ALIGN_CENTER | wx.ALL, 12)
        self._hide_response_buttons()
        self.reveal_btn.Hide()
        self.card_panel.Layout()

    def _next_card(self):
        if not self._queue:
            self.word_label.SetLabel("Session complete!")
            self.pos_label.SetLabel("")
            self.session_info.SetLabel("")
            self._clear_detail()
            msg = wx.StaticText(self.detail_panel,
                                label="Great job! Come back tomorrow for more.")
            msg.SetForegroundColour(wx.Colour(220, 220, 220))
            msg.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            self.detail_sizer.Add(msg, 0, wx.ALIGN_CENTER | wx.ALL, 12)
            self._hide_response_buttons()
            self.reveal_btn.Hide()
            self.card_panel.Layout()
            return

        kind, card, word = self._queue[0]
        self._current_card = card
        self._current_word = word
        self._showing_back = False

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
        self._hide_response_buttons()
        self.card_panel.Layout()
        self.Layout()

    def _clear_detail(self):
        """Remove all children from the detail panel."""
        self.detail_sizer.Clear(True)

    def _add_section(self, title, body, color=(200, 220, 255)):
        """Add a labeled section to the back-of-card."""
        if not body:
            return
        title_lbl = wx.StaticText(self.detail_panel, label=title)
        title_lbl.SetForegroundColour(wx.Colour(*color))
        title_lbl.SetFont(wx.Font(ui_scale.small(), wx.FONTFAMILY_DEFAULT,
                                  wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.detail_sizer.Add(title_lbl, 0, wx.LEFT | wx.TOP, 6)

        body_lbl = wx.StaticText(self.detail_panel, label=body)
        body_lbl.SetForegroundColour(wx.Colour(220, 220, 220))
        body_lbl.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                 wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        body_lbl.Wrap(900)
        self.detail_sizer.Add(body_lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

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
