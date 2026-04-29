"""
Capture a PNG screenshot of the MAIN app window and hand it to the user's
clipboard so they can paste it into the pre-filled GitHub issue body.

Design constraints (see `docs/reporting.md` for the user-facing description):

1. The Report dialog is a separate `wx.Dialog` child window. The user wants
   the *main* app window captured, NOT the dialog, so we can't just snap
   "the active window". We look up the `MainFrame` explicitly via
   `wx.GetTopLevelWindows()` and filter the dialog out.

2. Screenshot support is best-effort. If any step fails (no main frame
   alive, a wx error on this platform, a locked clipboard, an OS that
   blocks programmatic capture) we must NOT block the rest of the
   report flow — the URL-only path is still the authoritative channel.

3. We always save a local copy under `~/.gre_prep/reports/` so the user
   can drag-and-drop it manually if the clipboard path breaks, and so
   there's an audit trail even when the user decides not to paste.

4. On macOS (the user's platform) the native `screencapture` CLI is a
   reliable fallback when wxPython's `wx.ScreenDC` pixel-pulling returns
   a blank bitmap (a known issue on recent macOS + Retina displays).

This module is deliberately designed so every wx-touching helper is a
standalone function — unit tests can monkeypatch each one individually
without booting a real wx app.
"""
from __future__ import annotations

import datetime as _dt
import io
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from services.log import get_logger

_log = get_logger("report_screenshot")


# ── Paths ───────────────────────────────────────────────────────────────

def reports_dir() -> Path:
    """Directory where per-report PNG copies live.

    Kept under `~/.gre_prep/reports/` per the task spec. Created lazily.
    """
    d = Path.home() / ".gre_prep" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def screenshot_path_for(qid: Any, now: Optional[_dt.datetime] = None) -> Path:
    """Return the destination path for a per-report screenshot."""
    ts = (now or _dt.datetime.now()).strftime("%Y%m%d_%H%M%S")
    safe_qid = str(qid) if qid is not None else "unknown"
    return reports_dir() / f"screenshot_{safe_qid}_{ts}.png"


# ── Main-frame selection ────────────────────────────────────────────────

def pick_main_frame(
    top_level_windows: List[Any],
    main_frame_cls: Optional[type] = None,
) -> Optional[Any]:
    """Return the main app frame from the list of top-level wx windows.

    The Report dialog is a `wx.Dialog` child, not a `wx.Frame`, so in
    nearly every case filtering on frame-class is enough. We additionally
    drop windows that are being destroyed or hidden.

    `main_frame_cls` is optional — if provided we prefer an exact class
    match over any generic `wx.Frame`. This lets callers pin the lookup
    to the real `MainFrame` class, which makes it impossible to pick a
    subclass of `wx.Dialog` by accident.
    """
    candidates = []
    for w in top_level_windows:
        if w is None:
            continue
        # Skip anything that's mid-teardown.
        try:
            if hasattr(w, "IsBeingDeleted") and w.IsBeingDeleted():
                continue
        except Exception:
            continue
        # Skip hidden windows — a minimized-but-shown frame still counts.
        try:
            if hasattr(w, "IsShown") and not w.IsShown():
                continue
        except Exception:
            pass
        candidates.append(w)

    if main_frame_cls is not None:
        for w in candidates:
            if isinstance(w, main_frame_cls):
                return w

    # Fallback: prefer wx.Frame over wx.Dialog. We detect the distinction
    # via class-name so we don't have to import wx at module scope (the
    # test suite imports this module without a display).
    def _is_frame(w: Any) -> bool:
        # Walk the MRO and look for a class whose name contains "Frame"
        # but not "Dialog". Belt-and-braces so a subclass like MainFrame
        # still matches even if the direct wx.Frame check fails.
        try:
            mro_names = [c.__name__ for c in type(w).__mro__]
        except Exception:
            return False
        if any("Dialog" in n for n in mro_names):
            return False
        return any("Frame" in n for n in mro_names)

    for w in candidates:
        if _is_frame(w):
            return w
    return None


# ── Capture (wx-level) ──────────────────────────────────────────────────

def _capture_via_wx(main_frame: Any) -> Optional[bytes]:
    """Capture the given wx window to PNG bytes via wxPython DC APIs.

    Returns None on failure — caller is responsible for trying the native
    fallback. We blit the screen DC for the window's on-screen rect so
    the title bar + borders are included (a client-area-only capture
    loses important context for debugging rendering issues).
    """
    try:
        import wx  # local import — unit tests don't need wx
    except Exception:  # pragma: no cover — wx missing means nothing to do
        _log.warning("wx import failed; cannot capture via wx")
        return None

    try:
        size = main_frame.GetSize()
        width, height = size.GetWidth(), size.GetHeight()
        if width <= 0 or height <= 0:
            return None
        origin = main_frame.GetScreenPosition()

        screen_dc = wx.ScreenDC()
        bmp = wx.Bitmap(width, height)
        mem_dc = wx.MemoryDC(bmp)
        try:
            mem_dc.Blit(
                0, 0, width, height,
                screen_dc,
                origin.x, origin.y,
            )
        finally:
            mem_dc.SelectObject(wx.NullBitmap)

        img = bmp.ConvertToImage()
        buf = io.BytesIO()
        # wx.Image.SaveFile accepts a file-like object on wxPython >=4.
        if not img.SaveFile(buf, wx.BITMAP_TYPE_PNG):
            return None
        data = buf.getvalue()
        if not data:
            return None
        return data
    except Exception as exc:
        _log.warning("wx capture failed: %s", exc)
        return None


def _capture_via_screencapture(
    main_frame: Any,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Optional[bytes]:
    """macOS fallback: shell out to `/usr/sbin/screencapture -R`.

    wx.ScreenDC on recent macOS + Retina displays sometimes yields an
    entirely black bitmap because of the window-server sandbox. The
    native `screencapture` CLI uses the OS APIs directly and produces
    a correct image of the exact window rect.

    `runner` is injectable so unit tests can simulate success/failure
    without actually calling the binary.
    """
    if platform.system() != "Darwin":
        return None
    screencapture = shutil.which("screencapture") or "/usr/sbin/screencapture"
    if not os.path.exists(screencapture):
        return None

    try:
        origin = main_frame.GetScreenPosition()
        size = main_frame.GetSize()
        width, height = size.GetWidth(), size.GetHeight()
        if width <= 0 or height <= 0:
            return None
        rect = f"{origin.x},{origin.y},{width},{height}"
    except Exception as exc:
        _log.warning("could not read main-frame geometry: %s", exc)
        return None

    tmp = Path(tempfile.mkstemp(suffix=".png", prefix="gre_report_")[1])
    try:
        # -x: no sound. -R: rect. -t png: format. -o: no shadow (we're
        # blitting a rect, not a window handle, but this is a belt-and-
        # braces option in case the path changes in the future).
        result = runner(
            [screencapture, "-x", "-t", "png", "-R", rect, str(tmp)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            _log.warning(
                "screencapture returned %s: %s",
                result.returncode,
                getattr(result, "stderr", b""),
            )
            return None
        data = tmp.read_bytes()
        if not data:
            return None
        return data
    except Exception as exc:
        _log.warning("screencapture fallback failed: %s", exc)
        return None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def capture_main_window_png(
    top_level_windows: List[Any],
    main_frame_cls: Optional[type] = None,
    wx_capture: Callable[[Any], Optional[bytes]] = _capture_via_wx,
    native_capture: Callable[[Any], Optional[bytes]] = _capture_via_screencapture,
) -> Tuple[Optional[bytes], Optional[Any]]:
    """High-level entry point: pick the main frame and return (png_bytes, frame).

    We try the wx API first (works on Linux + Windows + older macOS).
    If it returns None, we fall back to the native OS screenshot CLI.
    Returning the frame alongside the bytes lets the caller pass it to
    downstream clipboard code without re-running the selection logic.
    """
    main = pick_main_frame(top_level_windows, main_frame_cls)
    if main is None:
        _log.warning("no main frame found among %d top-level windows",
                     len(top_level_windows))
        return None, None

    png = wx_capture(main)
    if png is None:
        png = native_capture(main)
    if png is None:
        _log.warning("all capture strategies failed")
    return png, main


# ── Clipboard + file save ───────────────────────────────────────────────

def save_png_to_file(
    png_bytes: bytes,
    path: Path,
) -> Optional[Path]:
    """Persist the PNG bytes to disk. Returns the final path or None."""
    if not png_bytes:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes)
        return path
    except OSError as exc:
        _log.warning("failed to write screenshot to %s: %s", path, exc)
        return None


def copy_png_to_clipboard(png_bytes: bytes) -> bool:
    """Put the PNG on the system clipboard as an image. Returns success.

    wx.Clipboard's `wx.BitmapDataObject` is the portable path: it takes
    a `wx.Bitmap`, and every modern OS clipboard manager understands
    the image-bitmap format. We convert via `wx.Image` so we never have
    to care about the source PNG's pixel format.
    """
    if not png_bytes:
        return False
    try:
        import wx
    except Exception:  # pragma: no cover
        _log.warning("wx import failed; clipboard copy skipped")
        return False

    try:
        img = wx.Image(io.BytesIO(png_bytes), wx.BITMAP_TYPE_PNG)
        if not img.IsOk():
            _log.warning("wx.Image could not load captured PNG")
            return False
        bmp = img.ConvertToBitmap()
    except Exception as exc:
        _log.warning("PNG → wx.Bitmap conversion failed: %s", exc)
        return False

    clipboard = wx.TheClipboard
    if not clipboard.Open():
        _log.warning("clipboard is locked; could not copy screenshot")
        return False
    try:
        data_obj = wx.BitmapDataObject(bmp)
        clipboard.SetData(data_obj)
        # Flush so the data survives the app losing focus when the
        # browser window takes over.
        try:
            clipboard.Flush()
        except Exception:
            # Flush is a no-op on platforms that don't support it;
            # don't fail the whole operation on that.
            pass
        return True
    except Exception as exc:
        _log.warning("clipboard SetData failed: %s", exc)
        return False
    finally:
        clipboard.Close()


# ── Public orchestrator ─────────────────────────────────────────────────

def attach_screenshot_for_report(
    qid: Any,
    top_level_windows: List[Any],
    main_frame_cls: Optional[type] = None,
    now: Optional[_dt.datetime] = None,
    wx_capture: Callable[[Any], Optional[bytes]] = _capture_via_wx,
    native_capture: Callable[[Any], Optional[bytes]] = _capture_via_screencapture,
    clipboard_fn: Callable[[bytes], bool] = copy_png_to_clipboard,
    save_fn: Callable[[bytes, Path], Optional[Path]] = save_png_to_file,
) -> dict:
    """End-to-end: capture the main window, copy to clipboard, save to disk.

    Returns a dict describing the outcome so the caller can surface an
    appropriate toast/message:

        {
            "captured":   bool,   # PNG bytes acquired?
            "clipboard":  bool,   # did it land on the clipboard?
            "file_path":  Path | None,   # local audit copy (may be None)
            "error":      str | None,    # human-readable reason on failure
        }

    This function NEVER raises — every exception is swallowed and logged,
    because a screenshot failure must not block the URL-only report path.
    """
    result = {
        "captured": False,
        "clipboard": False,
        "file_path": None,
        "error": None,
    }
    try:
        png, _main = capture_main_window_png(
            top_level_windows,
            main_frame_cls=main_frame_cls,
            wx_capture=wx_capture,
            native_capture=native_capture,
        )
        if not png:
            result["error"] = "capture-failed"
            return result
        result["captured"] = True

        dest = screenshot_path_for(qid, now=now)
        saved = save_fn(png, dest)
        if saved is not None:
            result["file_path"] = saved

        if clipboard_fn(png):
            result["clipboard"] = True
        else:
            result["error"] = "clipboard-locked"
        return result
    except Exception as exc:  # pragma: no cover — defensive
        _log.warning("attach_screenshot_for_report swallowed: %s", exc)
        result["error"] = f"exception: {exc}"
        return result


__all__ = [
    "attach_screenshot_for_report",
    "capture_main_window_png",
    "copy_png_to_clipboard",
    "pick_main_frame",
    "reports_dir",
    "save_png_to_file",
    "screenshot_path_for",
]
