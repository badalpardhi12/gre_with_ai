"""
AWA screen — the ETS "Test Preview Tool" Analytical Writing page.

Mounts the shared ExamChrome (charcoal header + maroon rule + tool ribbon +
pink section bar carrying the timer) over a black-bordered white content box
split into two panes:

  * LEFT  — the Issue statement (bordered box) above the task instructions
    ("Write a response in which you discuss the extent to which you agree or
    disagree...") in a second bordered box. SERIF text.
  * RIGHT — the essay editor (``wx.TextCtrl`` multiline) under a small toolbar
    of Cut / Paste / Undo / Redo beveled buttons (Undo/Redo greyed when there
    is nothing to undo/redo). The editor field is forced white-with-dark-text
    so it stays legible under macOS dark mode.

The writing screen ribbon is intentionally minimal — ``[help, next]`` — with no
Exit/Calc/Mark/Review during writing (early exit is reached via a separate
confirm page, see ``screens/transition_screen.py``).

Public API consumed by ``main_frame.py`` is preserved verbatim:
``load_prompt(prompt_data, session_id=None)``, ``set_on_submit(cb)``,
``set_on_time_expire(cb)``, ``set_on_exit(cb)``, ``get_essay()``,
``get_word_count()``, ``start_timer()`` — plus the ``editor`` / ``timer``
attributes.
"""
import os

import wx

from config import AWA_TIME, DATA_DIR
from widgets import ui_scale
from widgets.exam_chrome import ExamChrome
from widgets.theme import ExamColor


# Periodic-autosave interval. Writing every ~10s is plenty for a 30-minute
# essay and keeps disk wear minimal.
AWA_AUTOSAVE_INTERVAL_MS = 10_000

# The canonical ETS Issue-task instruction (shown when the prompt carries no
# explicit instructions of its own).
DEFAULT_ISSUE_INSTRUCTIONS = (
    "Write a response in which you discuss the extent to which you agree or "
    "disagree with the statement and explain your reasoning for the position "
    "you take. In developing and supporting your position, you should consider "
    "ways in which the statement might or might not hold true and explain how "
    "these considerations shape your position."
)


class _ToolbarButton(wx.Panel):
    """Small beveled Cut/Paste/Undo/Redo button for the editor toolbar.

    Native wx buttons ignore SetBackgroundColour under macOS dark mode, so this
    is owner-drawn to keep the light ETS bevel. Emits wx.EVT_BUTTON; greys out
    when disabled (so Undo/Redo read inert when there is nothing to do)."""

    def __init__(self, parent, label):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self._label = label
        self._hover = False
        self._pressed = False
        self._enabled = True
        self.SetBackgroundColour(ExamColor.CONTENT_BG)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self.SetMinSize(self._compute_size())
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ENTER_WINDOW, self._enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._leave)
        self.Bind(wx.EVT_LEFT_DOWN, self._down)
        self.Bind(wx.EVT_LEFT_UP, self._up)

    def _compute_size(self):
        dc = wx.MemoryDC(); dc.SelectObject(wx.Bitmap(1, 1))
        dc.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT))
        tw, _th = dc.GetTextExtent(self._label)
        dc.SelectObject(wx.NullBitmap)
        return wx.Size(tw + ui_scale.space(5), ui_scale.font_size(28))

    def DoGetBestClientSize(self):  # noqa: N802 — wx idiom
        return self._compute_size()

    def Enable(self, enable=True):  # noqa: N802 — wx idiom
        self._enabled = bool(enable)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND if enable else wx.CURSOR_ARROW))
        self.Refresh()
        return super().Enable(enable)

    def Disable(self):  # noqa: N802
        return self.Enable(False)

    def _emit(self):
        evt = wx.CommandEvent(wx.wxEVT_BUTTON, self.GetId())
        evt.SetEventObject(self)
        wx.PostEvent(self, evt)

    def _enter(self, _):
        if self._enabled:
            self._hover = True; self.Refresh()

    def _leave(self, _):
        self._hover = False; self._pressed = False; self.Refresh()

    def _down(self, _):
        if self._enabled:
            self._pressed = True
            if not self.HasCapture():
                self.CaptureMouse()
            self.Refresh()

    def _up(self, evt):
        if not self._enabled:
            return
        if self.HasCapture():
            self.ReleaseMouse()
        was = self._pressed
        self._pressed = False
        self.Refresh()
        if was and self.GetClientRect().Contains(evt.GetPosition()):
            self._emit()

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        gc.SetBrush(wx.Brush(ExamColor.CONTENT_BG)); gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRectangle(0, 0, w, h)

        if not self._enabled:
            face, ink = ExamColor.TOOL_BTN_FACE, ExamColor.BTN_DISABLED
        elif self._pressed or self._hover:
            face, ink = ExamColor.TOOL_BTN_FACE_HOVER, ExamColor.TOOL_BTN_TEXT
        else:
            face, ink = ExamColor.TOOL_BTN_FACE, ExamColor.TOOL_BTN_TEXT

        bx, by, bw, bh = 1, 1, w - 2, h - 2
        gc.SetBrush(wx.Brush(face)); gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(bx, by, bw, bh, 3)
        if self._enabled:
            gc.SetPen(wx.Pen(ExamColor.TOOL_BTN_BEVEL_HI, 1))
            gc.StrokeLine(bx + 1, by + 1, bx + bw - 1, by + 1)
            gc.StrokeLine(bx + 1, by + 1, bx + 1, by + bh - 1)
            gc.SetPen(wx.Pen(ExamColor.TOOL_BTN_BEVEL_LO, 1))
            gc.StrokeLine(bx + 1, by + bh - 1, bx + bw - 1, by + bh - 1)
            gc.StrokeLine(bx + bw - 1, by + 1, bx + bw - 1, by + bh - 1)

        gc.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT), ink)
        tw, th = gc.GetTextExtent(self._label)
        gc.DrawText(self._label, (w - tw) / 2, (h - th) / 2)


class AWAScreen(wx.Panel):
    """Analytical Writing Assessment screen (ETS Test Preview Tool styling)."""

    _WRAP_BASE = 360   # serif wrap width for the left-pane statement/instructions

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(ExamColor.PAGE_GRAY)
        self._on_submit = None
        self._on_exit = None
        self._prompt_data = None
        self._draft_path = None
        self._autosave_timer = None
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # ── Shared chrome: ribbon = [Help, Next]; pink bar carries timer ──
        self.chrome = ExamChrome(self, with_timer=True)
        self.chrome.set_buttons(["help", "next"])
        self.chrome.set_section_label("Analytical Writing")
        # Next == submit the essay (ETS advances off the writing page via Next).
        self.chrome.set_on("next", self._on_submit_click)
        self.chrome.set_on("help", self._on_help)
        outer.Add(self.chrome, 0, wx.EXPAND)

        # The chrome owns the timer; expose it as self.timer for callers.
        self.timer = self.chrome.timer
        if self.timer is not None:
            self.timer.set_time(AWA_TIME)

        # ── Black-bordered white content box on the gray page ───────────
        self.content_border = wx.Panel(self)
        self.content_border.SetBackgroundColour(ExamColor.CONTENT_BORDER)
        border_sizer = wx.BoxSizer(wx.VERTICAL)

        self.content_box = wx.Panel(self.content_border)
        self.content_box.SetBackgroundColour(ExamColor.CONTENT_BG)
        box_sizer = wx.BoxSizer(wx.HORIZONTAL)

        box_sizer.Add(self._build_prompt_pane(self.content_box), 1,
                      wx.EXPAND | wx.ALL, ui_scale.space(3))
        # Vertical divider between the two panes.
        divider = wx.Panel(self.content_box, size=(max(1, ui_scale.font_size(1)), -1))
        divider.SetBackgroundColour(ExamColor.DIVIDER)
        box_sizer.Add(divider, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, ui_scale.space(3))
        box_sizer.Add(self._build_editor_pane(self.content_box), 1,
                      wx.EXPAND | wx.ALL, ui_scale.space(3))

        self.content_box.SetSizer(box_sizer)
        border_sizer.Add(self.content_box, 1, wx.EXPAND | wx.ALL,
                         max(1, ui_scale.font_size(2)))
        self.content_border.SetSizer(border_sizer)
        outer.Add(self.content_border, 1, wx.EXPAND | wx.ALL, ui_scale.space(4))

        self.SetSizer(outer)

    def _build_prompt_pane(self, parent):
        """Left pane: bordered Issue statement above bordered task directions."""
        col = wx.BoxSizer(wx.VERTICAL)

        # Issue statement in a bordered box.
        stmt_border, self.statement_text = self._bordered_serif(parent)
        col.Add(stmt_border, 0, wx.EXPAND | wx.BOTTOM, ui_scale.space(4))

        # Task instructions in a second bordered box.
        instr_border, self.instructions_text = self._bordered_serif(parent)
        self.instructions_text.SetLabel(DEFAULT_ISSUE_INSTRUCTIONS)
        self.instructions_text.Wrap(ui_scale.font_size(self._WRAP_BASE))
        col.Add(instr_border, 0, wx.EXPAND)

        col.AddStretchSpacer()
        return col

    def _bordered_serif(self, parent):
        """A thin-bordered white box wrapping a serif StaticText.

        Returns ``(border_panel, label)``. The border is a 1px panel framing an
        inner white panel; the serif label lives inside with padding."""
        border = wx.Panel(parent)
        border.SetBackgroundColour(ExamColor.DIVIDER)
        bs = wx.BoxSizer(wx.VERTICAL)
        inner = wx.Panel(border)
        inner.SetBackgroundColour(ExamColor.CONTENT_BG)
        is_ = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(inner, label="")
        label.SetForegroundColour(ExamColor.TEXT)
        label.SetFont(ui_scale.exam_serif(ui_scale.EXAM_STEM_PT))
        is_.Add(label, 0, wx.ALL, ui_scale.space(3))
        inner.SetSizer(is_)
        bs.Add(inner, 1, wx.EXPAND | wx.ALL, max(1, ui_scale.font_size(1)))
        border.SetSizer(bs)
        return border, label

    def _build_editor_pane(self, parent):
        """Right pane: Cut/Paste/Undo/Redo toolbar above the essay editor."""
        col = wx.BoxSizer(wx.VERTICAL)

        # ── Toolbar strip (Cut · Paste · Undo · Redo) ──────────────────
        toolbar = wx.BoxSizer(wx.HORIZONTAL)
        self.cut_btn = _ToolbarButton(parent, "Cut")
        self.paste_btn = _ToolbarButton(parent, "Paste")
        self.undo_btn = _ToolbarButton(parent, "Undo")
        self.redo_btn = _ToolbarButton(parent, "Redo")
        self.cut_btn.Bind(wx.EVT_BUTTON, self._on_cut)
        self.paste_btn.Bind(wx.EVT_BUTTON, self._on_paste)
        self.undo_btn.Bind(wx.EVT_BUTTON, self._on_undo)
        self.redo_btn.Bind(wx.EVT_BUTTON, self._on_redo)
        for b in (self.cut_btn, self.paste_btn, self.undo_btn, self.redo_btn):
            toolbar.Add(b, 0, wx.RIGHT, ui_scale.space(1))
        col.Add(toolbar, 0, wx.BOTTOM, ui_scale.space(2))

        # ── Essay editor (white field, dark text, dark-mode-proof) ──────
        # A white-painted wrapper panel guarantees the field reads white even
        # when macOS dark mode ignores SetBackgroundColour on the TextCtrl.
        editor_wrap = wx.Panel(parent)
        editor_wrap.SetBackgroundColour(wx.Colour(0xFF, 0xFF, 0xFF))
        editor_wrap.Bind(wx.EVT_PAINT, lambda e, p=editor_wrap: self._paint_white(e, p))
        wrap_sizer = wx.BoxSizer(wx.VERTICAL)
        self.editor = wx.TextCtrl(
            editor_wrap,
            style=wx.TE_MULTILINE | wx.TE_WORDWRAP | wx.BORDER_SIMPLE)
        self.editor.SetBackgroundColour(wx.Colour(0xFF, 0xFF, 0xFF))
        self.editor.SetForegroundColour(ExamColor.TEXT)
        try:
            self.editor.SetDefaultStyle(
                wx.TextAttr(ExamColor.TEXT, wx.Colour(0xFF, 0xFF, 0xFF)))
        except Exception:
            pass
        self.editor.SetFont(ui_scale.exam_serif(ui_scale.EXAM_CHOICE_PT))
        self.editor.Bind(wx.EVT_TEXT, self._on_text_change)
        wrap_sizer.Add(self.editor, 1, wx.EXPAND | wx.ALL,
                       max(2, ui_scale.font_size(2)))
        editor_wrap.SetSizer(wrap_sizer)
        col.Add(editor_wrap, 1, wx.EXPAND)

        # ── Word count ─────────────────────────────────────────────────
        self.word_count_label = wx.StaticText(parent, label="Words: 0")
        self.word_count_label.SetForegroundColour(ExamColor.TEXT_MUTED)
        self.word_count_label.SetFont(ui_scale.exam_sans(ui_scale.EXAM_DIRECTIONS_PT))
        col.Add(self.word_count_label, 0, wx.TOP, ui_scale.space(2))

        self._sync_edit_buttons()
        return col

    @staticmethod
    def _paint_white(event, panel):
        dc = wx.PaintDC(panel)
        w, h = panel.GetClientSize()
        dc.SetBrush(wx.Brush(wx.Colour(0xFF, 0xFF, 0xFF)))
        dc.SetPen(wx.Pen(ExamColor.OVAL_BORDER, 1))
        dc.DrawRectangle(0, 0, w, h)

    # ── Editor toolbar handlers ────────────────────────────────────────

    def _on_cut(self, _event):
        self.editor.Cut()
        self._sync_edit_buttons()

    def _on_paste(self, _event):
        self.editor.Paste()
        self._sync_edit_buttons()

    def _on_undo(self, _event):
        if self.editor.CanUndo():
            self.editor.Undo()
        self._sync_edit_buttons()

    def _on_redo(self, _event):
        if self.editor.CanRedo():
            self.editor.Redo()
        self._sync_edit_buttons()

    def _sync_edit_buttons(self):
        """Grey Undo/Redo when there is nothing to undo/redo (ETS behavior)."""
        try:
            self.undo_btn.Enable(self.editor.CanUndo())
            self.redo_btn.Enable(self.editor.CanRedo())
        except Exception:
            pass

    def _on_help(self):
        wx.MessageBox(
            "Read the Issue statement and the task directions on the left, then "
            "type your response in the box on the right. Use Cut / Paste / Undo "
            "/ Redo to edit. Select Next when you are finished.",
            "Help", wx.OK | wx.ICON_INFORMATION)

    # ── load / draft autosave (preserved behavior) ─────────────────────

    def load_prompt(self, prompt_data, session_id=None):
        """Load an AWA prompt. prompt_data = {"prompt_text": ..., "instructions": ...}.

        ``prompt_text`` populates the Issue-statement box; ``instructions``
        populates the task-directions box (falling back to the canonical ETS
        Issue instruction when the prompt carries none).

        If ``session_id`` is given, the editor autosaves to
        ``data/awa_draft_<session_id>.txt`` every ~10s; on load, an existing
        draft for that session id offers to restore.
        """
        self._prompt_data = prompt_data
        statement = prompt_data.get("prompt_text", "")
        instructions = prompt_data.get("instructions", "") or DEFAULT_ISSUE_INSTRUCTIONS

        self.statement_text.SetLabel(statement)
        self.statement_text.Wrap(ui_scale.font_size(self._WRAP_BASE))
        self.instructions_text.SetLabel(instructions)
        self.instructions_text.Wrap(ui_scale.font_size(self._WRAP_BASE))

        self.editor.Clear()
        try:
            self.editor.DiscardEdits()
        except Exception:
            pass
        self.word_count_label.SetLabel("Words: 0")
        self._sync_edit_buttons()
        self.Layout()

        # Set up draft autosave for this session.
        if session_id is not None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self._draft_path = DATA_DIR / "awa_draft_{}.txt".format(session_id)
            self._maybe_restore_draft()
            self._start_autosave()
        else:
            self._draft_path = None

    def _maybe_restore_draft(self):
        if not self._draft_path or not self._draft_path.exists():
            return
        try:
            text = self._draft_path.read_text(encoding="utf-8")
        except OSError:
            return
        if not text.strip():
            return
        dlg = wx.MessageDialog(
            self,
            "We found an unfinished draft from a previous session for this "
            "AWA prompt. Restore it?",
            "Restore Draft?",
            wx.YES_NO | wx.YES_DEFAULT | wx.ICON_QUESTION,
        )
        try:
            if dlg.ShowModal() == wx.ID_YES:
                self.editor.SetValue(text)
                self.word_count_label.SetLabel(
                    "Words: {}".format(self.get_word_count()))
                self._sync_edit_buttons()
        finally:
            dlg.Destroy()

    def _start_autosave(self):
        if self._autosave_timer is None:
            self._autosave_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_autosave_tick, self._autosave_timer)
        self._autosave_timer.Start(AWA_AUTOSAVE_INTERVAL_MS)

    def _stop_autosave(self):
        if self._autosave_timer is not None:
            self._autosave_timer.Stop()

    def _on_autosave_tick(self, _):
        if not self._draft_path:
            return
        try:
            tmp = self._draft_path.with_suffix(self._draft_path.suffix + ".tmp")
            tmp.write_text(self.editor.GetValue(), encoding="utf-8")
            os.replace(tmp, self._draft_path)
        except OSError:
            pass

    def _delete_draft(self):
        self._stop_autosave()
        if self._draft_path and self._draft_path.exists():
            try:
                self._draft_path.unlink()
            except OSError:
                pass
        self._draft_path = None

    # ── timer / callbacks (preserved signatures) ───────────────────────

    def start_timer(self):
        if self.timer is not None:
            self.timer.set_time(AWA_TIME)
            self.timer.start()

    def set_on_submit(self, callback):
        """callback(essay_text, word_count)"""
        self._on_submit = callback

    def set_on_time_expire(self, callback):
        if self.timer is not None:
            self.timer.set_on_expire(callback)

    def set_on_exit(self, callback):
        """callback() — exit AWA back to dashboard"""
        self._on_exit = callback

    # ── accessors (preserved) ──────────────────────────────────────────

    def get_essay(self):
        return self.editor.GetValue()

    def get_word_count(self):
        text = self.editor.GetValue().strip()
        if not text:
            return 0
        return len(text.split())

    # ── event plumbing ────────────────────────────────────────────────

    def _on_text_change(self, _event):
        self.word_count_label.SetLabel("Words: {}".format(self.get_word_count()))
        self._sync_edit_buttons()

    def _on_submit_click(self, _event=None):
        essay = self.get_essay()
        wc = self.get_word_count()
        if wc < 10:
            dlg = wx.MessageDialog(
                self,
                "Your essay is very short. Are you sure you want to submit?",
                "Confirm Submit",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
            confirmed = dlg.ShowModal() == wx.ID_YES
            dlg.Destroy()
            if not confirmed:
                return
        # Submission complete — clean up the draft so we don't show "restore?"
        # next time this prompt is opened in a future session.
        self._delete_draft()
        if self._on_submit:
            self._on_submit(essay, wc)
