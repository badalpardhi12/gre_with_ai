# Reporting a question bug

Every question screen has a "Report" button (flag icon, top-right) that lets
you send the developer a full report about a rendering issue, a wrong answer
key, a mismatched explanation, or a question that just doesn't make sense.

## What happens when you click Report

1. A small dialog opens. Pick one of the four reasons and (optionally) add
   a free-text note.
2. When you click **Submit Report**, the app does two things:
   - **Writes a local audit row** to your `gre_user.db` — this powers the
     auto-retire-after-N-flags mechanism, so repeated reports on the same
     question also stop the app showing it to you.
   - **Opens a pre-filled GitHub issue page** in your default browser,
     against <https://github.com/badalpardhi12/gre_with_ai/issues>. The
     title, labels, context, and full question JSON are already filled
     in. You just need to click **Submit new issue** on the GitHub page.
3. You'll be prompted to sign in to GitHub the first time. Creating a free
   account takes a minute; after that, all future reports take a single
   click.

## What the developer sees

Each issue lands with the labels `user-report` and `question-bug`, plus:

- **qid** — the internal question ID, so the dev can jump straight to the
  row in the seed database.
- **source** — which bank the question came from (`kaplan_2024`,
  `princeton_2012`, `ai_synthetic`, etc.).
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

## Privacy

The report contains only the question data and your comment. It does NOT
include your response history, streak stats, session IDs, or any personal
information. You can edit the body on the GitHub page before submitting
if you want to remove something.

## What if I don't have a GitHub account?

The report is still saved locally to your `gre_user.db`, and the
auto-retire mechanism still protects you from seeing the same broken
question again. You can close the browser tab without submitting.

If enough users report the same question locally (three distinct flags,
default threshold), the app retires it automatically.
