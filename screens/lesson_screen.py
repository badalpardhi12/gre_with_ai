"""
Lesson reader screen — displays a single lesson with full HTML/math rendering.
"""
import wx

from models.database import Lesson
from widgets.math_view import MathView


class LessonScreen(wx.Panel):
    """Show a single lesson with title, body, and back button."""

    def __init__(self, parent):
        super().__init__(parent)
        self._on_back = None
        self._on_practice = None
        self._current_lesson = None
        self._build_ui()

    def _build_ui(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Header
        hdr = wx.BoxSizer(wx.HORIZONTAL)
        self.back_btn = wx.Button(self, label="← Back to Topics")
        self.back_btn.Bind(wx.EVT_BUTTON, lambda _: self._on_back() if self._on_back else None)
        hdr.Add(self.back_btn, 0, wx.ALL, 8)

        self.title_label = wx.StaticText(self, label="")
        self.title_label.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                         wx.FONTWEIGHT_BOLD))
        hdr.Add(self.title_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)

        self.practice_btn = wx.Button(self, label="Practice this Topic →")
        self.practice_btn.Bind(wx.EVT_BUTTON, self._handle_practice)
        hdr.Add(self.practice_btn, 0, wx.ALL, 8)

        sizer.Add(hdr, 0, wx.EXPAND)
        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND)

        # Lesson body
        self.body_view = MathView(self)
        sizer.Add(self.body_view, 1, wx.EXPAND | wx.ALL, 12)

        self.SetSizer(sizer)

    def set_on_back(self, handler):
        self._on_back = handler

    def set_on_practice(self, handler):
        self._on_practice = handler

    def load_lesson(self, lesson_id_or_subtopic):
        """Load a lesson by ID or subtopic slug."""
        if isinstance(lesson_id_or_subtopic, int):
            lesson = Lesson.get_or_none(Lesson.id == lesson_id_or_subtopic)
        else:
            lesson = Lesson.get_or_none(Lesson.subtopic == lesson_id_or_subtopic)

        if not lesson:
            self.title_label.SetLabel("Lesson not found")
            self.body_view.set_content("<p>This lesson is not available yet.</p>")
            return

        self._current_lesson = lesson
        self.title_label.SetLabel(lesson.title)
        self.body_view.set_content(lesson.body_html)
        self.Layout()

    def _handle_practice(self, _):
        if self._on_practice and self._current_lesson:
            self._on_practice(self._current_lesson.subtopic)
