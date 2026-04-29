"""
Integration tests for scripts/audit_figure_mispairs.py.

Covers the DB-side logic: fetching candidates and flipping confirmed
mispairings to status='draft' with review_notes populated. The LLM
judges themselves are injected as stubs so the test is fully offline.
"""
from __future__ import annotations

import base64
import json
import pathlib
import sqlite3
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.figure_mispair_audit import (
    MispairJudgment,
    MispairVerdict,
)

# Pull the module under test without letting it initialise the real
# Floodgate client (we never call audit() here).
from scripts import audit_figure_mispairs as runner


def _fixture_db(path: pathlib.Path) -> None:
    """Create a tiny schema-compatible SQLite fixture."""
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE stimulus (
            id INTEGER PRIMARY KEY,
            stimulus_type TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            render_spec TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE question (
            id INTEGER PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 1,
            measure TEXT NOT NULL DEFAULT 'quant',
            subtype TEXT NOT NULL DEFAULT 'data_interp',
            stimulus_id INTEGER,
            prompt TEXT NOT NULL,
            difficulty_target INTEGER NOT NULL DEFAULT 3,
            time_target_seconds INTEGER NOT NULL DEFAULT 120,
            concept_tags TEXT NOT NULL DEFAULT '',
            provenance TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            explanation TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            topic TEXT,
            subtopic TEXT NOT NULL DEFAULT '',
            question_type TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            review_notes TEXT NOT NULL DEFAULT ''
        );
    """)
    # One graph stimulus with an embedded fake PNG + one matching live Q.
    fake_png = base64.b64encode(b"\x89PNG\x00pretend-chart").decode()
    cur.execute(
        "INSERT INTO stimulus(id, stimulus_type, title, content) VALUES (?,?,?,?)",
        (100, "graph", "Sales",
         f'<img src="data:image/png;base64,{fake_png}" />'),
    )
    cur.execute(
        "INSERT INTO question(id, stimulus_id, prompt, status, source) "
        "VALUES (?,?,?,?,?)",
        (1, 100, "According to the chart, what were Q2 sales?",
         "live", "kaplan"),
    )
    # A question with no stimulus — should be excluded from candidates.
    cur.execute(
        "INSERT INTO question(id, stimulus_id, prompt, status, source) "
        "VALUES (?,?,?,?,?)",
        (2, None, "Plain question.", "live", "kaplan"),
    )
    conn.commit()
    conn.close()


def test_fetch_audit_candidates_only_returns_image_bearing_live(tmp_path):
    db_path = tmp_path / "test.db"
    _fixture_db(db_path)
    rows = runner.fetch_audit_candidates(str(db_path))
    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert rows[0]["sid"] == 100


def test_mark_mispair_flips_to_draft_and_notes(tmp_path):
    db_path = tmp_path / "test.db"
    _fixture_db(db_path)
    verdict = MispairVerdict(
        question_id=1, stimulus_id=100,
        judgments=[
            MispairJudgment(
                judge="opus_4_7_vision",
                matches=False, confidence="high",
                reasoning="Image shows answer-option grid, not a chart.",
                suspicious=["looks_like_options"],
            ),
            MispairJudgment(
                judge="sonnet_4_6_vision",
                matches=False, confidence="high",
                reasoning="Image unrelated to sales.",
                suspicious=["wrong_subject"],
            ),
        ],
        confirmed_mispair=True,
    )
    runner.mark_mispair_as_draft(str(db_path), 1, verdict, dry_run=False)

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "SELECT status, review_notes FROM question WHERE id=1")
    status, notes = cur.fetchone()
    conn.close()

    assert status == "draft"
    assert "figure-mispair-audit" in notes
    assert "opus_4_7_vision" in notes
    assert "sonnet_4_6_vision" in notes
    assert "looks_like_options" in notes


def test_mark_mispair_dry_run_does_not_touch_db(tmp_path):
    db_path = tmp_path / "test.db"
    _fixture_db(db_path)
    verdict = MispairVerdict(
        question_id=1, stimulus_id=100,
        judgments=[
            MispairJudgment(judge="opus_4_7_vision",
                            matches=False, confidence="high",
                            reasoning="x"),
            MispairJudgment(judge="sonnet_4_6_vision",
                            matches=False, confidence="high",
                            reasoning="y"),
        ],
        confirmed_mispair=True,
    )
    runner.mark_mispair_as_draft(str(db_path), 1, verdict, dry_run=True)

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT status FROM question WHERE id=1")
    status = cur.fetchone()[0]
    conn.close()
    assert status == "live"  # unchanged


def test_cache_roundtrip(tmp_path):
    cache_path = tmp_path / "cache.json"
    runner.save_cache(cache_path, {"1:100": {"confirmed_mispair": False}})
    loaded = runner.load_cache(cache_path)
    assert loaded == {"1:100": {"confirmed_mispair": False}}


def test_summarize_counts_per_source(tmp_path):
    # Two candidates: one match (kaplan), one mismatch (princeton).
    candidates = [
        {"id": 1, "sid": 100, "source": "kaplan"},
        {"id": 2, "sid": 101, "source": "princeton"},
    ]
    cache = {
        "1:100": {
            "question_id": 1, "stimulus_id": 100,
            "confirmed_mispair": False, "tier2_disagreement": False,
            "judgments": [],
        },
        "2:101": {
            "question_id": 2, "stimulus_id": 101,
            "confirmed_mispair": True, "tier2_disagreement": False,
            "judgments": [],
        },
    }
    summary = runner._summarize(candidates, cache)
    assert summary["matches"] == 1
    assert summary["confirmed_mispair"] == 1
    assert summary["by_source"]["kaplan"]["match"] == 1
    assert summary["by_source"]["princeton"]["mismatch"] == 1


def test_fixture_end_to_end_with_stub_judges(tmp_path, monkeypatch):
    """Full `audit()` loop against a stubbed LLM — exercises the DB
    write path, cache persistence, and the summary aggregation."""
    db_path = tmp_path / "test.db"
    _fixture_db(db_path)
    cache_path = tmp_path / "cache.json"

    def _stub_opus(model_id):
        def _call(system, user, img, mt):
            return (
                '{"matches": false, "confidence": "high", '
                '"reasoning": "image is options grid", '
                '"suspicious": ["looks_like_options"]}'
            )
        return _call

    def _stub_sonnet(model_id):
        def _call(system, user, img, mt):
            return (
                '{"matches": false, "confidence": "high", '
                '"reasoning": "wrong subject", '
                '"suspicious": ["wrong_subject"]}'
            )
        return _call

    # First call returns opus stub, second returns sonnet stub. Patch
    # the factory so _make_anthropic_judge doesn't touch the network.
    original = runner._make_anthropic_judge
    calls = {"n": 0}

    def _fake_factory(model_id):
        calls["n"] += 1
        return _stub_opus(model_id) if calls["n"] == 1 else _stub_sonnet(model_id)

    monkeypatch.setattr(runner, "_make_anthropic_judge", _fake_factory)
    # Disable the git commit per batch in tests.
    monkeypatch.setattr(runner, "_git_commit", lambda *a, **k: None)

    summary = runner.audit(
        db_path=str(db_path),
        cache_path=cache_path,
        batch_size=10,
        dry_run=False,
    )
    assert summary["confirmed_mispair"] == 1
    assert summary["matches"] == 0

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT status, review_notes FROM question WHERE id=1")
    status, notes = cur.fetchone()
    conn.close()
    assert status == "draft"
    assert "figure-mispair-audit" in notes

    # Second run is a no-op because of the cache.
    runner._make_anthropic_judge = original  # restore
    summary2 = runner.audit(
        db_path=str(db_path),
        cache_path=cache_path,
        batch_size=10,
        dry_run=False,
    )
    assert summary2["cached"] == 1
