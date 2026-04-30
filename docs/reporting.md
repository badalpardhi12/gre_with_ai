# Reporting a question bug

Every question screen has a "Report" button (flag icon, top-right) that lets
you send the developer a full report about a rendering issue, a wrong answer
key, a mismatched explanation, or a question that just doesn't make sense.

## What happens when you click Report

1. A small dialog opens. Pick one of the four reasons and (optionally) add
   a free-text note.
2. By default, the dialog has **"Include screenshot of app window"**
   checked. Leave it on if you want the developer to see exactly what you
   were looking at — the tooltip explains: *"Captures the test window
   (not this dialog). The image is copied to the clipboard so you can
   paste it into the GitHub issue body with Cmd+V."* Uncheck it if the
   bug is purely about data (wrong answer key, stale explanation) and a
   screenshot would add nothing.
3. When you click **Submit Report**, the app does three things:
   - **Writes a local audit row** to your `gre_user.db` — this powers the
     auto-retire-after-N-flags mechanism, so repeated reports on the same
     question also stop the app showing it to you.
   - **Captures a PNG of the main test window** (NOT the Report dialog)
     and copies it to your system clipboard. A local copy is also saved
     under `~/.gre_prep/reports/screenshot_<qid>_<timestamp>.png` as an
     audit trail you can attach manually if the clipboard path breaks.
   - **Opens a pre-filled GitHub issue page** in your default browser,
     against <https://github.com/badalpardhi12/gre_with_ai/issues>. The
     title, labels, context, and full question JSON are already filled
     in. Paste the screenshot into the issue body with **Cmd+V** and
     click **Submit new issue**.
4. You'll be prompted to sign in to GitHub the first time. Creating a free
   account takes a minute; after that, all future reports take a single
   click.

## How the screenshot path is captured

The Report dialog is a separate wxPython `wx.Dialog` child — capturing
the "active window" would snap the dialog itself, which isn't useful.
Instead, the app walks `wx.GetTopLevelWindows()` and filters for the
`MainFrame` class so the screenshot always targets the actual test
window. The capture happens *before* the dialog is shown so the dialog
can't occlude the main window in the resulting image.

On macOS, wxPython's `wx.ScreenDC` can return a blank bitmap on Retina
displays; the app automatically falls back to the native
`screencapture -R` CLI in that case, which uses the OS APIs directly
and produces a faithful image of the main window rect.

If the clipboard is locked (another app is holding it), the app tells
you the local file path instead and you can drag-and-drop it onto the
GitHub issue body. If the capture itself fails entirely, the report
still opens the GitHub URL — the screenshot is a bonus, not a blocker.

## What the developer sees

Each issue lands with the labels `user-report` and `question-bug`, plus:

- **qid** — the internal question ID, so the dev can jump straight to the
  row in the seed database.
- **source** — which bank the question came from (internal source tag).
- **subtype** — question type (`mcq_single`, `numeric_entry`,
  `rc_select_passage`, etc.).
- **correct_label** — the answer the app currently marks as correct.
- **Your comment** — verbatim.
- **Stem snippet** — first ~240 chars of the prompt, for quick skimming
  in the issue list.
- **Full JSON payload** — the entire question record in a collapsed
  `<details>` block. If the payload is huge, the app truncates it so
  the URL fits under GitHub's ~8 KB cap; the local DB row is still
  untruncated.
- **Screenshot** (when you paste it) — a PNG of the main test window,
  attached directly to the issue body by GitHub's image-upload handler.

## Privacy

The report contains only the question data, your comment, and (if you
pasted the screenshot) the image of the test window. It does NOT
include your response history, streak stats, session IDs, or any personal
information. You can edit the body on the GitHub page before submitting
if you want to remove something.

The local screenshot copy under `~/.gre_prep/reports/` stays on your
machine until you delete it — the app never uploads it automatically.

## What if I don't have a GitHub account?

The report is still saved locally to your `gre_user.db`, and the
auto-retire mechanism still protects you from seeing the same broken
question again. You can close the browser tab without submitting.

If enough users report the same question locally (three distinct flags,
default threshold), the app retires it automatically.
