"""
Tests for services.report_screenshot — the "attach a PNG of the main
app window to the user's clipboard" helper that backs the Report flow.

The wx library is not safe to instantiate in a headless CI environment
(no display, no app loop), so every wx-touching path is designed to
be monkeypatched. These tests cover:

* `pick_main_frame` — must prefer the MainFrame over a Dialog even when
  both are top-level. Picks nothing when every candidate is hidden or
  being deleted.

* `capture_main_window_png` — tries wx first; falls back to native on
  failure; returns (None, None) when no main frame exists.

* `copy_png_to_clipboard` / `save_png_to_file` fallback logic — a
  locked clipboard should not block the file-save path.

* `attach_screenshot_for_report` — end-to-end, happy path: captures,
  saves, clips. Failures at any stage downgrade gracefully to a dict
  with the `error` key set; the function NEVER raises.
"""
from __future__ import annotations

import datetime as _dt
import io
import os
import subprocess
from pathlib import Path
from typing import Any, List, Optional
from unittest import mock

import pytest

from services import report_screenshot as mod


# ── Lightweight wx surrogates ───────────────────────────────────────────


class FakeSize:
    def __init__(self, w, h):
        self._w, self._h = w, h

    def GetWidth(self):
        return self._w

    def GetHeight(self):
        return self._h


class FakePoint:
    def __init__(self, x, y):
        self.x, self.y = x, y


class FakeMainFrame:
    """Stand-in for wx.Frame subclass MainFrame."""

    def __init__(self, shown=True, deleted=False, width=800, height=600,
                 origin=(10, 20)):
        self._shown = shown
        self._deleted = deleted
        self._size = FakeSize(width, height)
        self._origin = FakePoint(*origin)

    def IsBeingDeleted(self):
        return self._deleted

    def IsShown(self):
        return self._shown

    def GetSize(self):
        return self._size

    def GetScreenPosition(self):
        return self._origin


class FakeReportDialog:
    """Stand-in for the FlagQuestionDialog — must NEVER be picked."""

    def __init__(self, shown=True, deleted=False):
        self._shown = shown
        self._deleted = deleted

    def IsBeingDeleted(self):
        return self._deleted

    def IsShown(self):
        return self._shown


# ── pick_main_frame ─────────────────────────────────────────────────────


def test_pick_main_frame_selects_frame_over_dialog():
    main = FakeMainFrame()
    dlg = FakeReportDialog()
    # Dialog is first in the list so a naive "take the first window"
    # implementation would pick it; verify we reject dialogs.
    picked = mod.pick_main_frame([dlg, main])
    assert picked is main


def test_pick_main_frame_prefers_exact_class_match():
    main = FakeMainFrame()
    other_frame = FakeMainFrame()
    # Both look like frames, but only the first is a MainFrame instance.
    # Subclass a dummy to act as the MainFrame class.
    class DummyMainFrame(FakeMainFrame):
        pass

    specific = DummyMainFrame()
    picked = mod.pick_main_frame(
        [main, specific, other_frame], main_frame_cls=DummyMainFrame,
    )
    assert picked is specific


def test_pick_main_frame_skips_deleted_windows():
    dead = FakeMainFrame(deleted=True)
    alive = FakeMainFrame(deleted=False)
    picked = mod.pick_main_frame([dead, alive])
    assert picked is alive


def test_pick_main_frame_skips_hidden_windows():
    hidden = FakeMainFrame(shown=False)
    visible = FakeMainFrame(shown=True)
    picked = mod.pick_main_frame([hidden, visible])
    assert picked is visible


def test_pick_main_frame_returns_none_when_only_dialog():
    dlg = FakeReportDialog()
    assert mod.pick_main_frame([dlg]) is None


def test_pick_main_frame_returns_none_on_empty_list():
    assert mod.pick_main_frame([]) is None


# ── capture_main_window_png ─────────────────────────────────────────────


def test_capture_returns_bytes_from_wx_path():
    main = FakeMainFrame()
    wx_cap = mock.Mock(return_value=b"\x89PNG...wx")
    native = mock.Mock()
    png, frame = mod.capture_main_window_png(
        [main], wx_capture=wx_cap, native_capture=native,
    )
    assert png == b"\x89PNG...wx"
    assert frame is main
    native.assert_not_called()


def test_capture_falls_back_to_native_when_wx_returns_none():
    main = FakeMainFrame()
    wx_cap = mock.Mock(return_value=None)
    native = mock.Mock(return_value=b"\x89PNG...native")
    png, frame = mod.capture_main_window_png(
        [main], wx_capture=wx_cap, native_capture=native,
    )
    assert png == b"\x89PNG...native"
    assert frame is main
    wx_cap.assert_called_once_with(main)
    native.assert_called_once_with(main)


def test_capture_returns_none_when_both_strategies_fail():
    main = FakeMainFrame()
    wx_cap = mock.Mock(return_value=None)
    native = mock.Mock(return_value=None)
    png, frame = mod.capture_main_window_png(
        [main], wx_capture=wx_cap, native_capture=native,
    )
    assert png is None
    # We still return the frame — a caller might want to log it even
    # though the capture failed.
    assert frame is main


def test_capture_returns_none_when_no_main_frame():
    dlg = FakeReportDialog()
    png, frame = mod.capture_main_window_png(
        [dlg],
        wx_capture=mock.Mock(return_value=b"x"),
        native_capture=mock.Mock(return_value=b"y"),
    )
    assert png is None
    assert frame is None


def test_capture_never_picks_the_report_dialog():
    """The whole point of the window-selection logic — prove it."""
    main = FakeMainFrame()
    dlg = FakeReportDialog()
    wx_cap = mock.Mock(return_value=b"\x89PNG")
    _png, frame = mod.capture_main_window_png(
        [dlg, main], wx_capture=wx_cap, native_capture=mock.Mock(),
    )
    assert frame is main
    # And the capture function was called with the MainFrame, never the dialog.
    (called_with,), _ = wx_cap.call_args
    assert called_with is main


# ── save_png_to_file ────────────────────────────────────────────────────


def test_save_png_writes_bytes_to_disk(tmp_path):
    dest = tmp_path / "reports" / "shot.png"
    out = mod.save_png_to_file(b"\x89PNG\x00data", dest)
    assert out == dest
    assert dest.read_bytes() == b"\x89PNG\x00data"


def test_save_png_creates_parent_directory(tmp_path):
    dest = tmp_path / "a" / "b" / "c" / "shot.png"
    assert not dest.parent.exists()
    out = mod.save_png_to_file(b"x", dest)
    assert out == dest
    assert dest.parent.is_dir()


def test_save_png_empty_bytes_returns_none(tmp_path):
    dest = tmp_path / "shot.png"
    assert mod.save_png_to_file(b"", dest) is None
    assert not dest.exists()


def test_save_png_returns_none_on_os_error(tmp_path, monkeypatch):
    dest = tmp_path / "shot.png"

    def _boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", _boom)
    assert mod.save_png_to_file(b"\x89PNG", dest) is None


# ── screenshot_path_for ─────────────────────────────────────────────────


def test_screenshot_path_includes_qid_and_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "reports_dir", lambda: tmp_path)
    ts = _dt.datetime(2026, 4, 27, 15, 30, 45)
    path = mod.screenshot_path_for(1234, now=ts)
    assert path.parent == tmp_path
    assert path.name == "screenshot_1234_20260427_153045.png"


def test_screenshot_path_handles_none_qid(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "reports_dir", lambda: tmp_path)
    ts = _dt.datetime(2026, 4, 27, 0, 0, 0)
    path = mod.screenshot_path_for(None, now=ts)
    assert "screenshot_unknown_" in path.name


# ── attach_screenshot_for_report (end-to-end) ───────────────────────────


def test_attach_happy_path_captures_saves_and_clips(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "reports_dir", lambda: tmp_path)
    main = FakeMainFrame()
    clip = mock.Mock(return_value=True)

    result = mod.attach_screenshot_for_report(
        qid=42,
        top_level_windows=[main],
        wx_capture=lambda f: b"\x89PNGfake",
        native_capture=lambda f: None,
        clipboard_fn=clip,
    )
    assert result["captured"] is True
    assert result["clipboard"] is True
    assert result["file_path"] is not None
    assert result["file_path"].exists()
    assert result["file_path"].read_bytes() == b"\x89PNGfake"
    assert result["error"] is None
    clip.assert_called_once_with(b"\x89PNGfake")


def test_attach_saves_file_even_when_clipboard_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "reports_dir", lambda: tmp_path)
    main = FakeMainFrame()

    result = mod.attach_screenshot_for_report(
        qid=7,
        top_level_windows=[main],
        wx_capture=lambda f: b"\x89PNG",
        native_capture=lambda f: None,
        clipboard_fn=lambda data: False,
    )
    assert result["captured"] is True
    assert result["clipboard"] is False
    assert result["file_path"] is not None
    assert result["file_path"].exists()
    assert result["error"] == "clipboard-locked"


def test_attach_returns_capture_failed_when_no_main_window():
    result = mod.attach_screenshot_for_report(
        qid=1,
        top_level_windows=[FakeReportDialog()],
        wx_capture=lambda f: b"ignored",
        native_capture=lambda f: b"ignored",
        clipboard_fn=lambda data: True,
    )
    assert result["captured"] is False
    assert result["clipboard"] is False
    assert result["file_path"] is None
    assert result["error"] == "capture-failed"


def test_attach_returns_capture_failed_when_all_strategies_return_none(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "reports_dir", lambda: tmp_path)
    main = FakeMainFrame()
    result = mod.attach_screenshot_for_report(
        qid=2,
        top_level_windows=[main],
        wx_capture=lambda f: None,
        native_capture=lambda f: None,
        clipboard_fn=lambda data: True,
    )
    assert result["captured"] is False
    assert result["error"] == "capture-failed"


def test_attach_never_raises_on_internal_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "reports_dir", lambda: tmp_path)
    main = FakeMainFrame()

    def _explode(f):
        raise RuntimeError("synthetic")

    # Any exception in the pipeline should downgrade to error-in-dict,
    # not propagate — the URL-only report flow must never break.
    result = mod.attach_screenshot_for_report(
        qid=3,
        top_level_windows=[main],
        wx_capture=_explode,
        native_capture=lambda f: None,
        clipboard_fn=lambda data: True,
    )
    assert result["captured"] is False
    assert "error" in result


# ── _capture_via_screencapture (Darwin only, mocked binary) ─────────────


def test_screencapture_skipped_on_non_darwin(monkeypatch):
    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    out = mod._capture_via_screencapture(FakeMainFrame(), runner=mock.Mock())
    assert out is None


def test_screencapture_invokes_binary_on_darwin(monkeypatch):
    monkeypatch.setattr(mod.platform, "system", lambda: "Darwin")
    # Force the binary path to be "findable".
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/sbin/screencapture")
    monkeypatch.setattr(mod.os.path, "exists", lambda p: True)

    captured_args = {}

    def _fake_runner(args, capture_output, check):
        captured_args["args"] = args
        # Write a stub PNG to the output path so read_bytes returns non-empty.
        out_path = args[-1]
        Path(out_path).write_bytes(b"\x89PNG-from-screencapture")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    data = mod._capture_via_screencapture(
        FakeMainFrame(width=400, height=300, origin=(5, 6)),
        runner=_fake_runner,
    )
    assert data == b"\x89PNG-from-screencapture"
    # Rect arg includes the origin + size.
    assert "-R" in captured_args["args"]
    rect = captured_args["args"][captured_args["args"].index("-R") + 1]
    assert rect == "5,6,400,300"


def test_screencapture_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/sbin/screencapture")
    monkeypatch.setattr(mod.os.path, "exists", lambda p: True)

    def _fail_runner(args, capture_output, check):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout=b"", stderr=b"denied")

    data = mod._capture_via_screencapture(FakeMainFrame(), runner=_fail_runner)
    assert data is None


# ── Integration: simulate the full question_screen flow ────────────────


def test_report_flow_picks_main_frame_not_dialog(tmp_path, monkeypatch):
    """End-to-end simulation of the question_screen code path.

    We mimic `wx.GetTopLevelWindows()` returning [MainFrame, ReportDialog]
    (the common case once the dialog is open) and verify:

      1. The capture function receives the MainFrame, not the dialog.
      2. The PNG lands in both the clipboard mock and the on-disk audit
         path.
    """
    monkeypatch.setattr(mod, "reports_dir", lambda: tmp_path)

    main = FakeMainFrame()
    dlg = FakeReportDialog()

    captured_target = {"frame": None}

    def _fake_wx_capture(frame):
        captured_target["frame"] = frame
        return b"\x89PNG\x0Dreport-flow"

    clipboard = mock.Mock(return_value=True)

    result = mod.attach_screenshot_for_report(
        qid=9001,
        top_level_windows=[main, dlg],   # dialog is present
        wx_capture=_fake_wx_capture,
        native_capture=lambda f: None,
        clipboard_fn=clipboard,
    )

    assert captured_target["frame"] is main, \
        "capture must target the MainFrame, not the dialog"
    assert result["clipboard"] is True
    assert result["file_path"].read_bytes() == b"\x89PNG\x0Dreport-flow"
    clipboard.assert_called_once()
