"""
Programmatic UI screenshot capture for documentation.

Walks every sidebar tab (Today, Learn, Practice, Vocab, Insights) plus the
onboarding wizard and saves a PNG of each to `docs/screenshots/`.

Two capture strategies:
  1. macOS — the native `/usr/sbin/screencapture` CLI captures the actual
     window pixels including any wx.html2.WebView contents (which a pure
     wx.WindowDC.Blit often misses on macOS because the WebView is a native
     subview owned by WebKit, not by wxPython).
  2. Other platforms — `wx.WindowDC` + `wx.MemoryDC.Blit` + `Bitmap.SaveFile`.

Usage:
    venv/bin/python scripts/capture_screenshots.py
    # → writes docs/screenshots/{today,learn,practice,vocab,insights,
    #                            onboarding_step_1,onboarding_step_2,
    #                            onboarding_step_3,settings,answer_chat}.png

The screenshots are committed to the repo so they can render in the README
without forcing readers to clone + run anything.

Honors:
  --tabs today,learn       only capture a subset
  --window WIDTHxHEIGHT    override window size (default 1400x900)
  --out PATH               override output directory
  --no-onboarding          skip the wizard pages
"""
import argparse
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

# Make the project importable when this script runs from any cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import wx  # noqa: E402


# Capture targets in render order. Some require special wiring (onboarding
# wizard isn't a sidebar tab; settings is a modal dialog).
TAB_TARGETS = [
    ("today",    "Today"),
    ("learn",    "Learn"),
    ("practice", "Practice"),
    ("vocab",    "Vocab"),
    ("insights", "Insights"),
]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tabs", default="",
                   help="Comma-separated subset of tabs (default: all)")
    p.add_argument("--window", default="1400x900",
                   help="WIDTHxHEIGHT (default 1400x900)")
    p.add_argument("--out", default=str(PROJECT_ROOT / "docs" / "screenshots"),
                   help="Output directory")
    p.add_argument("--no-onboarding", action="store_true",
                   help="Skip onboarding wizard captures")
    p.add_argument("--no-modals", action="store_true",
                   help="Skip Settings + AnswerChat dialog captures")
    p.add_argument("--strategy", choices=("auto", "screencapture", "blit"),
                   default="auto",
                   help="auto = screencapture on macOS, blit elsewhere")
    return p.parse_args()


def capture(frame: wx.Frame, path: Path, strategy: str = "auto"):
    """Save a PNG of `frame` to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)

    use_screencapture = (
        strategy == "screencapture"
        or (strategy == "auto" and platform.system() == "Darwin"
            and Path("/usr/sbin/screencapture").exists())
    )

    if use_screencapture:
        # Lift the window so it's not occluded; small sleep so the OS has a
        # chance to actually composite it.
        frame.Raise()
        wx.Yield()
        time.sleep(0.4)
        # `-l <window-id>` captures the specific wx window. wxPython exposes
        # the native handle via Window.GetHandle() on macOS as an NSView*; we
        # need the *window* id. The simplest robust path is `-R x,y,w,h`
        # using the frame's screen geometry.
        sx, sy = frame.ClientToScreen((0, 0))
        cw, ch = frame.GetClientSize()
        # Include the title bar by walking up to the actual frame top.
        fx, fy = frame.GetScreenPosition()
        fw, fh = frame.GetSize()
        rect = f"{fx},{fy},{fw},{fh}"
        try:
            subprocess.run(
                ["/usr/sbin/screencapture", "-R", rect, "-x", str(path)],
                check=True, timeout=10,
            )
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(f"  [warn] screencapture failed ({exc}); falling back to Blit")

    # Cross-platform fallback.
    size = frame.GetClientSize()
    bmp = wx.Bitmap(size.width, size.height)
    src_dc = wx.WindowDC(frame)
    mem_dc = wx.MemoryDC(bmp)
    mem_dc.Blit(0, 0, size.width, size.height, src_dc, 0, 0)
    mem_dc.SelectObject(wx.NullBitmap)
    bmp.SaveFile(str(path), wx.BITMAP_TYPE_PNG)


def settle(ms: int = 350):
    """Pump the event loop for `ms` so paint and layout finish."""
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        wx.Yield()
        time.sleep(0.02)


def capture_tabs(frame, tab_ids, out_dir, strategy):
    for tab_id, label in TAB_TARGETS:
        if tab_ids and tab_id not in tab_ids:
            continue
        print(f"  → {label} ({tab_id})")
        frame._on_sidebar_select(tab_id)
        # Vocab needs an extra moment for the SRS query.
        settle(450 if tab_id in ("learn", "vocab") else 350)
        capture(frame, out_dir / f"{tab_id}.png", strategy=strategy)


def capture_onboarding(frame, out_dir, strategy):
    """Force the onboarding wizard onto the screen and snap each step.

    The wizard is normally only shown for un-onboarded users; we surface it
    explicitly here, take screenshots, then restore the user back to Today.
    """
    print("  → Onboarding wizard")
    wizard = frame.screens["onboarding"]
    # Show the wizard (overrides the launch-time onboarding gate).
    for sname, panel in frame.screens.items():
        panel.Show(sname == "onboarding")
    frame.panel_container.Layout()
    wizard._step = wizard.STEP_WELCOME
    wizard._render_step()
    settle()
    capture(frame, out_dir / "onboarding_step_1.png", strategy=strategy)

    wizard._step = wizard.STEP_GOAL
    wizard._render_step()
    settle()
    capture(frame, out_dir / "onboarding_step_2.png", strategy=strategy)

    wizard._step = wizard.STEP_DIAGNOSTIC
    wizard._render_step()
    settle()
    capture(frame, out_dir / "onboarding_step_3.png", strategy=strategy)

    # Restore: back to Today.
    frame._on_sidebar_select("today")
    settle()


def capture_settings_modal(frame, out_dir, strategy):
    """Open the Settings dialog non-modally so we can snap the parent frame
    while the dialog is on top."""
    print("  → Settings dialog")
    from screens.llm_settings import LLMSettingsDialog
    dlg = LLMSettingsDialog(frame)
    dlg.Show()      # non-modal so we can keep iterating wx.Yield()
    settle(500)
    capture(frame, out_dir / "settings.png", strategy=strategy)
    dlg.Destroy()
    settle(150)


def capture_answer_chat_modal(frame, out_dir, strategy):
    """Open the AnswerChat dialog scoped to the most-recent question."""
    print("  → AnswerChat dialog")
    from models.database import Response
    from services.question_bank import QuestionBankService
    from screens.answer_chat_screen import AnswerChatDialog
    qb = QuestionBankService()
    last = (Response.select()
            .order_by(Response.created_at.desc())
            .first())
    q_data = qb.get_question(last.question_id) if last else None
    if q_data is None:
        # Pick any live question so the chat has context.
        from models.database import Question
        any_q = Question.select().where(Question.status == "live").first()
        q_data = qb.get_question(any_q.id) if any_q else None
    if q_data is None:
        print("    [skip] no live questions in DB")
        return
    dlg = AnswerChatDialog(frame, q_data)
    dlg.Show()
    settle(500)
    capture(frame, out_dir / "answer_chat.png", strategy=strategy)
    dlg.Destroy()
    settle(150)


def main():
    args = parse_args()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        w_str, h_str = args.window.split("x")
        win_size = (int(w_str), int(h_str))
    except ValueError:
        sys.exit(f"Bad --window {args.window!r}; expected WIDTHxHEIGHT")

    tab_ids = {t.strip() for t in args.tabs.split(",") if t.strip()}

    app = wx.App(False)

    # Build the actual app frame.
    from main_frame import MainFrame
    frame = MainFrame()
    frame.SetSize(win_size)
    frame.Show()
    frame.Raise()
    settle(700)   # give the OS time to composite the window for the first time

    print(f"\nCapturing screenshots to {out_dir}")
    print(f"  window: {win_size[0]}x{win_size[1]}    strategy: {args.strategy}")
    print()

    capture_tabs(frame, tab_ids, out_dir, strategy=args.strategy)

    if not args.no_onboarding:
        capture_onboarding(frame, out_dir, strategy=args.strategy)

    if not args.no_modals:
        capture_settings_modal(frame, out_dir, strategy=args.strategy)
        capture_answer_chat_modal(frame, out_dir, strategy=args.strategy)

    print()
    pngs = sorted(out_dir.glob("*.png"))
    print(f"Wrote {len(pngs)} screenshot(s):")
    for p in pngs:
        size_kb = p.stat().st_size / 1024
        print(f"  {p.relative_to(PROJECT_ROOT)}  ({size_kb:.1f} KB)")

    frame.Close(force=True)
    app.ExitMainLoop()


if __name__ == "__main__":
    main()
