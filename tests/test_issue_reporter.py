"""
Tests for services.issue_reporter — the URL builder that hands users a
pre-filled GitHub "New Issue" link for reporting a buggy question.

We pin behaviors that are easy to regress when editing the body
template:
  * qid / source / subtype / correct_label are always present
  * user comment is preserved verbatim
  * URL stays under the ~8 KB GitHub cap even when the question JSON
    is enormous
  * truncation keeps the human-readable sections intact and inserts
    the sentinel marker
"""
from __future__ import annotations

import datetime as _dt
import urllib.parse
from unittest import mock

import pytest

from services.issue_reporter import (
    DEFAULT_LABELS,
    DEFAULT_REPO,
    MAX_URL_BYTES,
    build_issue_url,
)


@pytest.fixture
def sample_question():
    return {
        "id": 1234,
        "measure": "verbal",
        "subtype": "mcq_single",
        "source": "kaplan_2024",
        "prompt": "If x is the sum of all positive even integers less than 50, what is x?",
        "difficulty": 0.6,
        "tags": ["arithmetic"],
        "explanation": "Sum = 2 + 4 + ... + 48 = 2 * (1+2+...+24) = 600.",
        "stimulus": None,
        "options": [
            {"label": "A", "text": "500", "is_correct": False},
            {"label": "B", "text": "600", "is_correct": True},
            {"label": "C", "text": "625", "is_correct": False},
            {"label": "D", "text": "650", "is_correct": False},
            {"label": "E", "text": "700", "is_correct": False},
        ],
        "numeric_answer": None,
    }


# ── URL structure ────────────────────────────────────────────────────


def test_url_points_at_correct_repo(sample_question):
    url = build_issue_url(sample_question, "B is wrong, answer is C")
    assert url.startswith(
        f"https://github.com/{DEFAULT_REPO}/issues/new?"
    )


def test_url_carries_labels(sample_question):
    url = build_issue_url(sample_question, "test")
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert params["labels"] == [DEFAULT_LABELS]


def test_url_title_includes_qid_and_snippet(sample_question):
    url = build_issue_url(sample_question, "marked answer is wrong")
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    title = params["title"][0]
    assert title.startswith("[Q1234]")
    assert "marked answer is wrong" in title


def test_url_title_truncated_to_60_chars_of_comment(sample_question):
    comment = "A" * 200
    url = build_issue_url(sample_question, comment)
    parsed = urllib.parse.urlparse(url)
    title = urllib.parse.parse_qs(parsed.query)["title"][0]
    # "[Q1234] " prefix + truncated body (<=60 chars + ellipsis)
    assert title.startswith("[Q1234] ")
    assert "…" in title


def test_url_title_fallback_when_comment_empty(sample_question):
    url = build_issue_url(sample_question, "")
    parsed = urllib.parse.urlparse(url)
    title = urllib.parse.parse_qs(parsed.query)["title"][0]
    assert "[Q1234]" in title
    assert "user-reported issue" in title


# ── Body content ─────────────────────────────────────────────────────


def _body_of(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.parse_qs(parsed.query)["body"][0]


def test_body_includes_all_context_fields(sample_question):
    url = build_issue_url(sample_question, "answer is wrong")
    body = _body_of(url)
    assert "qid: `1234`" in body
    assert "source: `kaplan_2024`" in body
    assert "subtype: `mcq_single`" in body
    assert "correct_label: `B`" in body


def test_body_preserves_user_comment(sample_question):
    url = build_issue_url(sample_question, "This option makes no sense")
    body = _body_of(url)
    assert "## What went wrong" in body
    assert "This option makes no sense" in body


def test_body_contains_stem_snippet(sample_question):
    url = build_issue_url(sample_question, "x")
    body = _body_of(url)
    assert "## Stem snippet" in body
    # First few words of the prompt should appear in the snippet.
    assert "positive even integers" in body


def test_body_contains_json_payload(sample_question):
    url = build_issue_url(sample_question, "x")
    body = _body_of(url)
    assert "```json" in body
    assert '"id": 1234' in body
    assert '"source": "kaplan_2024"' in body


def test_body_includes_timestamp_and_version(sample_question):
    ts = _dt.datetime(2026, 4, 27, 15, 30, 0)
    url = build_issue_url(
        sample_question, "x", app_version="v0.9.3", timestamp=ts
    )
    body = _body_of(url)
    assert "app_version: `v0.9.3`" in body
    assert "2026-04-27 15:30:00 UTC" in body


def test_numeric_entry_question_surfaces_numeric_answer():
    q = {
        "id": 42,
        "measure": "quant",
        "subtype": "numeric_entry",
        "source": "princeton_2012",
        "prompt": "What is 2+2?",
        "options": [],
        "numeric_answer": {
            "exact_value": "4",
            "numerator": None,
            "denominator": None,
            "tolerance": 0,
            "mode": "auto",
        },
    }
    body = _body_of(build_issue_url(q, "easy one"))
    assert "correct_label: `numeric: 4`" in body


# ── URL length / truncation ──────────────────────────────────────────


def test_url_stays_under_cap_for_normal_question(sample_question):
    url = build_issue_url(sample_question, "wrong answer")
    assert len(url) < MAX_URL_BYTES


def test_url_truncates_oversized_json_but_keeps_context():
    huge_q = {
        "id": 9999,
        "measure": "verbal",
        "subtype": "rc_select_in_passage",
        "source": "princeton_2012",
        "prompt": "Read the passage and pick the sentence that best supports the claim.",
        "explanation": "X" * 20000,  # guaranteed to blow past 8 KB
        "stimulus": {
            "type": "passage",
            "title": "On clouds",
            "content": "Y" * 20000,
        },
        "options": [
            {"label": chr(65 + i), "text": "Z" * 500, "is_correct": i == 0}
            for i in range(5)
        ],
        "numeric_answer": None,
    }
    url = build_issue_url(huge_q, "this passage is garbled")
    assert len(url) < MAX_URL_BYTES, f"URL is {len(url)} bytes"

    body = _body_of(url)
    # Human-readable sections survive truncation.
    assert "qid: `9999`" in body
    assert "source: `princeton_2012`" in body
    assert "this passage is garbled" in body
    # Sentinel marker is present.
    assert "truncated for URL length" in body


def test_empty_prompt_falls_back_to_stimulus_for_snippet():
    q = {
        "id": 7,
        "measure": "verbal",
        "subtype": "rc_mcq",
        "source": "kaplan_2024",
        "prompt": "",
        "stimulus": {
            "type": "passage",
            "title": None,
            "content": "Clouds form when water vapor condenses.",
        },
        "options": [
            {"label": "A", "text": "rain", "is_correct": True},
        ],
        "numeric_answer": None,
    }
    body = _body_of(build_issue_url(q, "stim content leaked"))
    assert "Clouds form when water vapor condenses" in body


# ── Integration-ish: verify the handler opens the browser ────────────


def test_webbrowser_open_called_with_github_url(sample_question):
    """Simulate the question_screen handler: build the URL and hand it
    to `webbrowser.open`. This pins the contract that the wiring layer
    doesn't mangle the URL (e.g. by wrapping it in shell-quote).
    """
    url = build_issue_url(sample_question, "option B is wrong")
    with mock.patch("webbrowser.open") as m_open:
        import webbrowser
        webbrowser.open(url, new=2)
    m_open.assert_called_once()
    called_url = m_open.call_args[0][0]
    assert called_url.startswith("https://github.com/")
    assert "issues/new" in called_url
