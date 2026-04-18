"""
Answer Chat dialog — per-question AI tutor.

Opens after a user reveals the answer (in learning mode). Lets them ask
follow-up questions about the question they just saw.
"""
import threading
import wx

from services.mistake_coach import AnswerChat
from widgets import ui_scale


class AnswerChatDialog(wx.Dialog):
    """Modal dialog with a chat interface scoped to a single question."""

    def __init__(self, parent, q_data, user_response=None):
        super().__init__(
            parent, title="Ask AI Tutor",
            size=(ui_scale.font_size(720), ui_scale.font_size(600)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.q_data = q_data
        self.chat = AnswerChat(q_data, user_response)
        self._pending_placeholder_pos = None
        self._build_ui()

    def _build_ui(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Header
        title = wx.StaticText(self,
                              label="🤖 AI Tutor — Ask anything about this question")
        title.SetFont(wx.Font(ui_scale.large(), wx.FONTFAMILY_DEFAULT,
                              wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(title, 0, wx.ALL, 10)
        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND)

        # Chat history (read-only text)
        self.history_view = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.TE_NO_VSCROLL | wx.HSCROLL | wx.VSCROLL)
        self.history_view.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                          wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        sizer.Add(self.history_view, 1, wx.EXPAND | wx.ALL, 8)

        # Quick suggestions
        suggestions_sizer = wx.BoxSizer(wx.HORIZONTAL)
        for prompt in ["Why is this answer correct?",
                       "Why is the most tempting wrong answer wrong?",
                       "Show me an easier version"]:
            btn = wx.Button(self, label=prompt)
            btn.SetFont(wx.Font(ui_scale.small(), wx.FONTFAMILY_DEFAULT,
                                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            btn.Bind(wx.EVT_BUTTON, lambda e, p=prompt: self._send(p))
            suggestions_sizer.Add(btn, 0, wx.ALL, 4)
        sizer.Add(suggestions_sizer, 0, wx.ALL, 4)

        # Input area
        input_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.input = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.input.SetFont(wx.Font(ui_scale.normal(), wx.FONTFAMILY_DEFAULT,
                                   wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.input.Bind(wx.EVT_TEXT_ENTER, self._on_send)
        input_sizer.Add(self.input, 1, wx.EXPAND | wx.ALL, 4)

        self.send_btn = wx.Button(self, label="Send")
        self.send_btn.Bind(wx.EVT_BUTTON, self._on_send)
        input_sizer.Add(self.send_btn, 0, wx.ALL, 4)
        sizer.Add(input_sizer, 0, wx.EXPAND | wx.ALL, 4)

        # Bottom
        btn_sizer = self.CreateStdDialogButtonSizer(wx.CLOSE)
        sizer.Add(btn_sizer, 0, wx.ALL | wx.ALIGN_RIGHT, 6)
        self.Bind(wx.EVT_BUTTON, lambda _: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)

        self.SetSizer(sizer)
        self._append("Tutor", "Hi! I can help you understand this question. "
                              "Click a suggestion above or type your own question.")

    def _append(self, sender, text):
        """Add a message to the history view."""
        if self.history_view.GetValue():
            self.history_view.AppendText("\n\n")
        self.history_view.AppendText(f"{sender}: {text}")

    def _on_send(self, _):
        text = self.input.GetValue().strip()
        if not text:
            return
        self._send(text)

    def _send(self, message):
        if not message.strip():
            return
        self.input.SetValue("")
        self._append("You", message)
        # Track the start position of the placeholder so we can replace just
        # those characters when the reply arrives — robust to multi-message
        # bursts and to user input that happens to end with the same string.
        self._pending_placeholder_pos = self.history_view.GetLastPosition()
        self._append("Tutor", "(thinking...)")
        self.send_btn.Disable()
        # Run in a thread so UI doesn't freeze
        threading.Thread(target=self._ask_async, args=(message,), daemon=True).start()

    def _ask_async(self, message):
        try:
            reply = self.chat.ask(message, max_tokens=1024)
        except Exception as e:
            reply = f"(error: {e})"
        # Update UI from main thread
        wx.CallAfter(self._receive_reply, reply)

    def _receive_reply(self, reply):
        # Remove the placeholder by character range, not string suffix match.
        if self._pending_placeholder_pos is not None:
            end = self.history_view.GetLastPosition()
            self.history_view.Replace(self._pending_placeholder_pos, end, "")
            self._pending_placeholder_pos = None
        self._append("Tutor", reply)
        self.send_btn.Enable()
