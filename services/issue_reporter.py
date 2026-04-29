"""
Build a pre-filled GitHub "New issue" URL so end users can report a
buggy question without needing a GitHub API token on their machine.

Design: the app keeps its existing `QuestionFlag` DB row as a local
audit trail, then ALSO opens `https://github.com/<repo>/issues/new`
with title/body/labels pre-filled. The user clicks "Submit" on the
GitHub page (authenticating with their own account), and the report
lands in the dev's issue tracker where it can be triaged centrally.

GitHub caps the URL at ~8 KB; we truncate the embedded question JSON
before hitting that ceiling so the link always opens successfully.
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.parse
from typing import Any, Dict, Optional


# GitHub rejects `issues/new?...` URLs that exceed roughly 8 KB total.
# We budget a bit of headroom for the scheme/host/path + encoded title
# + labels so the body alone can't push us over.
MAX_URL_BYTES = 8000
TRUNCATION_MARKER = "\n\n<!-- [full JSON truncated for URL length limit] -->"

DEFAULT_REPO = "badalpardhi12/gre_with_ai"
DEFAULT_LABELS = "user-report,question-bug"


def _stem_snippet(question: Dict[str, Any], max_chars: int = 240) -> str:
    """Return a short plain-text preview of the question prompt."""
    prompt = (question.get("prompt") or "").strip()
    if not prompt:
        # Fall back to the stimulus content if the prompt is empty
        stim = question.get("stimulus") or {}
        prompt = (stim.get("content") or "").strip()
    # Strip HTML tags for a cleaner snippet (cheap, not full sanitization)
    import re
    prompt = re.sub(r"<[^>]+>", " ", prompt)
    prompt = re.sub(r"\s+", " ", prompt).strip()
    if len(prompt) > max_chars:
        prompt = prompt[: max_chars - 1].rstrip() + "…"
    return prompt


def _correct_label(question: Dict[str, Any]) -> str:
    """Return the correct option label (A/B/C/...) or '-' if none."""
    for opt in question.get("options") or []:
        if opt.get("is_correct"):
            return str(opt.get("label") or "").strip() or "?"
    # Numeric-entry questions don't have options; surface the numeric answer.
    numeric = question.get("numeric_answer")
    if numeric:
        val = numeric.get("exact_value")
        if val is not None:
            return f"numeric: {val}"
        num, den = numeric.get("numerator"), numeric.get("denominator")
        if num is not None and den is not None:
            return f"numeric: {num}/{den}"
    return "-"


def _build_title(question: Dict[str, Any], user_comment: str) -> str:
    qid = question.get("id", "?")
    summary = (user_comment or "").strip().replace("\n", " ")
    if not summary:
        summary = "user-reported issue"
    if len(summary) > 60:
        summary = summary[:59].rstrip() + "…"
    return f"[Q{qid}] {summary}"


def _build_body(
    question: Dict[str, Any],
    user_comment: str,
    app_version: Optional[str] = None,
    timestamp: Optional[_dt.datetime] = None,
) -> str:
    """Render the Markdown issue body from a question dict + user note.

    Structure matches `.github/ISSUE_TEMPLATE/question-bug.md` so dev-side
    triage reads the same sections regardless of which entry point was
    used.
    """
    qid = question.get("id", "?")
    source = question.get("source") or "(unknown)"
    subtype = question.get("subtype") or "(unknown)"
    correct = _correct_label(question)
    stem = _stem_snippet(question)

    ts = (timestamp or _dt.datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S UTC")
    version = app_version or "unknown"

    comment = (user_comment or "").strip() or "_(no comment provided)_"

    payload_json = json.dumps(question, indent=2, sort_keys=True, default=str)

    body = (
        "## What went wrong\n"
        f"{comment}\n\n"
        "## Question context (auto-filled by the app)\n"
        f"- qid: `{qid}`\n"
        f"- source: `{source}`\n"
        f"- subtype: `{subtype}`\n"
        f"- correct_label: `{correct}`\n"
        f"- app_version: `{version}`\n"
        f"- reported_at: `{ts}`\n\n"
        "## Stem snippet\n"
        f"> {stem}\n\n"
        "## Question JSON\n"
        "<details>\n"
        "<summary>Full payload</summary>\n\n"
        "```json\n"
        f"{payload_json}\n"
        "```\n"
        "</details>\n"
    )
    return body


def _truncate_body_for_url(
    body: str,
    base_url_bytes: int,
    encoded_title_bytes: int,
    encoded_labels_bytes: int,
) -> str:
    """Shrink the embedded JSON payload so the final URL stays under 8 KB.

    We keep every non-JSON section intact (context + user comment +
    stem snippet) because those are the sections a human triages first.
    Only the `<details>` JSON block gets truncated, and we always leave
    a visible marker so the dev knows to check the local DB for the
    untruncated record.
    """
    # Conservative ceiling for the body's *encoded* size.
    budget = MAX_URL_BYTES - base_url_bytes - encoded_title_bytes - encoded_labels_bytes
    if budget <= 0:
        # Pathological — the title/labels alone blew the budget. Return
        # a stub body so url-building still succeeds; the local DB row
        # is authoritative anyway.
        return "_Body omitted: URL length limit exceeded._" + TRUNCATION_MARKER

    encoded_len = len(urllib.parse.quote(body, safe=""))
    if encoded_len <= budget:
        return body

    # Binary-search the largest prefix of the JSON block that fits.
    # Splitting the body at the JSON fence keeps the human-readable
    # sections intact.
    json_fence = "```json\n"
    fence_idx = body.find(json_fence)
    if fence_idx < 0:
        # No JSON block to trim — fall back to a hard cut with marker.
        keep = _longest_prefix_under_budget(
            body, budget - len(urllib.parse.quote(TRUNCATION_MARKER, safe=""))
        )
        return body[:keep] + TRUNCATION_MARKER

    prefix = body[: fence_idx + len(json_fence)]
    # `suffix` is everything after the JSON payload: the closing ``` and
    # the </details> line. We want both the prefix and suffix preserved.
    close_fence = "\n```\n</details>\n"
    close_idx = body.rfind(close_fence)
    if close_idx < 0:
        suffix = "\n```\n</details>\n"
        json_body = body[fence_idx + len(json_fence):]
    else:
        suffix = body[close_idx:]
        json_body = body[fence_idx + len(json_fence): close_idx]

    marker_encoded = len(urllib.parse.quote(TRUNCATION_MARKER, safe=""))
    fixed_encoded = (
        len(urllib.parse.quote(prefix, safe=""))
        + len(urllib.parse.quote(suffix, safe=""))
        + marker_encoded
    )
    json_budget = budget - fixed_encoded
    if json_budget <= 0:
        # Even the scaffolding is too large — drop the JSON entirely.
        return (
            prefix.rstrip()
            + "\n(truncated)\n"
            + suffix
            + TRUNCATION_MARKER
        )

    kept = _longest_prefix_under_budget(json_body, json_budget)
    # Round back to a newline so we don't truncate mid-token.
    nl = json_body.rfind("\n", 0, kept)
    if nl > 0:
        kept = nl
    return prefix + json_body[:kept] + suffix + TRUNCATION_MARKER


def _longest_prefix_under_budget(text: str, budget: int) -> int:
    """Return the largest index i such that quote(text[:i]) <= budget."""
    if budget <= 0:
        return 0
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(urllib.parse.quote(text[:mid], safe="")) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def build_issue_url(
    question: Dict[str, Any],
    user_comment: str,
    repo: str = DEFAULT_REPO,
    labels: str = DEFAULT_LABELS,
    app_version: Optional[str] = None,
    timestamp: Optional[_dt.datetime] = None,
) -> str:
    """Return a pre-filled `issues/new` URL for this question report.

    The URL is safe to hand to `webbrowser.open()`. If the fully-rendered
    body would blow past GitHub's ~8 KB URL cap, the embedded question
    JSON is truncated (with a visible marker) while all human-readable
    context (user comment, qid/source/subtype/correct_label, stem
    snippet) is preserved.
    """
    title = _build_title(question, user_comment)
    body = _build_body(
        question,
        user_comment,
        app_version=app_version,
        timestamp=timestamp,
    )

    base = f"https://github.com/{repo}/issues/new"
    # Size the "fixed" portion so we know how much room the body has.
    encoded_title = urllib.parse.quote(title, safe="")
    encoded_labels = urllib.parse.quote(labels, safe="")
    # `?title=...&body=...&labels=...` scaffolding costs ~30 bytes;
    # include it in the base so the budget is honest.
    scaffold = len(base) + len("?title=&body=&labels=")

    body = _truncate_body_for_url(
        body,
        base_url_bytes=scaffold,
        encoded_title_bytes=len(encoded_title),
        encoded_labels_bytes=len(encoded_labels),
    )

    params = urllib.parse.urlencode(
        {"title": title, "body": body, "labels": labels}
    )
    return f"{base}?{params}"


__all__ = ["build_issue_url", "MAX_URL_BYTES", "DEFAULT_REPO", "DEFAULT_LABELS"]
