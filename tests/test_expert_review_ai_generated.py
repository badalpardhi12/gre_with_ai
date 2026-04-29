"""Tests for ``scripts.expert_review_ai_generated``.

These tests cover the review-to-demote pipeline in isolation:
- The Kaplan-style ``expert_review`` verdict is translated to a
  ``status='live'`` vs ``'draft'`` update.
- The ``review_notes`` reflect the verdict + reviewer notes.
- Cache round-trips survive a disk write.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import expert_review_ai_generated as script  # noqa: E402


def test_apply_verdict_live_keeps_item_live(monkeypatch):
    """A 'live' verdict with passing notes leaves status=live and stamps the
    review_notes so the audit trail shows the item was reviewed."""
    calls: Dict[str, Any] = {}

    class _FakeUpdateQuery:
        def __init__(self, **kwargs):
            calls.update(kwargs)

        def where(self, *args, **kwargs):
            return self

        def execute(self):
            calls["executed"] = True
            return 1

    class _FakeID:
        def __eq__(self, other):
            calls["where_id"] = other
            return True

    class _FakeQuestion:
        id = _FakeID()

        @staticmethod
        def update(**kwargs):
            return _FakeUpdateQuery(**kwargs)

    class _FakeDB:
        def connect(self, **kwargs):
            calls["connected"] = True

    verdict = {
        "verdict": "live",
        "reviewer_notes": "all axes passed",
        "axis_mean": {"correctness": 5.0},
    }
    status = script.apply_verdict(_FakeDB(), _FakeQuestion, 123, verdict)
    assert status == "live"
    assert calls["status"] == "live"
    assert "LIVE" in calls["review_notes"]
    assert "2026-04-28" in calls["review_notes"]


def test_apply_verdict_draft_demotes(monkeypatch):
    recorded: Dict[str, Any] = {}

    class _Q:
        def __init__(self, **kw):
            recorded.update(kw)

        def where(self, *a, **k):
            return self

        def execute(self):
            return 1

    class _FakeID:
        def __eq__(self, other):
            return True

    class _FakeQuestion:
        id = _FakeID()

        @staticmethod
        def update(**kwargs):
            return _Q(**kwargs)

    class _FakeDB:
        def connect(self, **kwargs):
            pass

    verdict = {
        "verdict": "draft",
        "reviewer_notes": ("Failing axes (need >= 2 judges at >= 4): "
                           "correctness ; Scores: correctness=3.0 (2-4)"),
        "axis_mean": {"correctness": 3.0},
    }
    status = script.apply_verdict(_FakeDB(), _FakeQuestion, 456, verdict)
    assert status == "draft"
    assert recorded["status"] == "draft"
    assert "DEMOTED" in recorded["review_notes"]
    assert "correctness" in recorded["review_notes"]


def test_apply_verdict_caps_absurdly_long_notes():
    recorded: Dict[str, Any] = {}

    class _Q:
        def __init__(self, **kw):
            recorded.update(kw)

        def where(self, *a, **k):
            return self

        def execute(self):
            return 1

    class _FakeID:
        def __eq__(self, other):
            return True

    class _FakeQuestion:
        id = _FakeID()

        @staticmethod
        def update(**kwargs):
            return _Q(**kwargs)

    class _FakeDB:
        def connect(self, **kwargs):
            pass

    long_notes = "x" * 20000
    verdict = {"verdict": "draft", "reviewer_notes": long_notes}
    status = script.apply_verdict(_FakeDB(), _FakeQuestion, 789, verdict)
    assert status == "draft"
    assert len(recorded["review_notes"]) <= 8000


def test_cache_round_trip(tmp_path, monkeypatch):
    fake_cache = tmp_path / "cache.json"
    monkeypatch.setattr(script, "CACHE_PATH", fake_cache)
    data = {"1": {"verdict": "live", "axis_mean": {"correctness": 5.0}}}
    script.save_cache(data)
    loaded = script.load_cache()
    assert loaded == data


def test_cache_load_returns_empty_on_missing(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(script, "CACHE_PATH", missing)
    assert script.load_cache() == {}


def test_cache_load_tolerates_corrupt_file(tmp_path, monkeypatch):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    monkeypatch.setattr(script, "CACHE_PATH", corrupt)
    assert script.load_cache() == {}


def test_axis_mean_aggregates_across_verdicts():
    verdicts = [
        {"axis_mean": {"correctness": 4.0}},
        {"axis_mean": {"correctness": 5.0}},
        {"axis_mean": {"correctness": 3.0}},
    ]
    m = script._axis_mean(verdicts, "correctness")
    assert pytest.approx(m) == 4.0


def test_axis_mean_skips_missing_verdicts():
    verdicts = [
        {"axis_mean": {"correctness": 4.0}},
        {},  # missing
        {"axis_mean": {}},
    ]
    m = script._axis_mean(verdicts, "correctness")
    assert m == 4.0  # only one valid observation
