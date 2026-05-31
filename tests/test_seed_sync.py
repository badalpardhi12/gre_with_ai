"""
Regression tests for `services/seed_sync.py`.

The sync bridges the gap between `git pull` (updates the shipped seed
`data/gre_mock.db`) and the runtime `data/gre_user.db` (untracked,
diverges over time). Each launch fingerprints the seed and runs a
reconcile only when the seed has changed since the last sync.

The tests isolate everything in `/tmp` — neither the shipped seed nor
the real user DB is touched.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

import pytest


SCHEMA_TABLES = {
    "question": """
        CREATE TABLE question (
          id INTEGER PRIMARY KEY,
          version INTEGER NOT NULL DEFAULT 1,
          measure TEXT NOT NULL,
          subtype TEXT NOT NULL,
          stimulus_id INTEGER,
          prompt TEXT NOT NULL,
          difficulty_target INTEGER NOT NULL DEFAULT 3,
          time_target_seconds INTEGER NOT NULL DEFAULT 90,
          concept_tags TEXT NOT NULL DEFAULT '[]',
          provenance TEXT NOT NULL DEFAULT 'seed',
          status TEXT NOT NULL DEFAULT 'live',
          explanation TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT '2026-01-01 00:00:00',
          updated_at TEXT NOT NULL DEFAULT '2026-01-01 00:00:00',
          topic TEXT,
          subtopic TEXT NOT NULL DEFAULT '',
          question_type TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL DEFAULT 'test',
          quality_score REAL,
          mastery_difficulty REAL,
          provenance_json TEXT NOT NULL DEFAULT '{}',
          review_notes TEXT NOT NULL DEFAULT '',
          generated_at TEXT,
          run_id TEXT NOT NULL DEFAULT '',
          pretest_started_at TEXT,
          pretest_n_responses INTEGER NOT NULL DEFAULT 0,
          pretest_p_correct REAL,
          pretest_disc_proxy REAL,
          irt_b_estimate REAL,
          irt_a_estimate REAL,
          promotion_at TEXT,
          source_anchor TEXT NOT NULL DEFAULT '',
          figure_refs TEXT NOT NULL DEFAULT '[]'
        )
    """,
    "questionoption": """
        CREATE TABLE questionoption (
          id INTEGER PRIMARY KEY,
          question_id INTEGER NOT NULL,
          option_label TEXT NOT NULL,
          option_text TEXT NOT NULL,
          is_correct INTEGER NOT NULL
        )
    """,
    "numericanswer": """
        CREATE TABLE numericanswer (
          id INTEGER PRIMARY KEY,
          question_id INTEGER NOT NULL,
          exact_value REAL,
          numerator INTEGER,
          denominator INTEGER,
          tolerance REAL NOT NULL DEFAULT 0,
          mode TEXT
        )
    """,
    "stimulus": """
        CREATE TABLE stimulus (
          id INTEGER PRIMARY KEY,
          stimulus_type TEXT NOT NULL,
          title TEXT NOT NULL,
          content TEXT NOT NULL,
          render_spec TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL DEFAULT '2026-01-01 00:00:00'
        )
    """,
    "response": """
        CREATE TABLE response (
          id INTEGER PRIMARY KEY,
          question_id INTEGER NOT NULL,
          user_id TEXT NOT NULL DEFAULT 'local',
          is_correct INTEGER NOT NULL,
          created_at TEXT NOT NULL DEFAULT '2026-01-01 00:00:00'
        )
    """,
}


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    for sql in SCHEMA_TABLES.values():
        conn.execute(sql)
    return conn


def _seed_question_row(conn, qid: int, prompt: str, explanation: str,
                       status: str = "live", stimulus_id=None):
    conn.execute(
        "INSERT INTO question (id, measure, subtype, prompt, explanation, "
        "status, stimulus_id) VALUES (?, 'quant', 'mcq_single', ?, ?, ?, ?)",
        (qid, prompt, explanation, status, stimulus_id),
    )


@pytest.fixture
def paths(tmp_path):
    seed = tmp_path / "seed.db"
    user = tmp_path / "user.db"
    s = _make_db(seed)
    u = _make_db(user)
    yield seed, user, s, u
    s.close()
    u.close()


# ── Core reconcile behavior ────────────────────────────────────────────

def test_prompt_rewrite_in_seed_propagates_to_user(paths):
    seed, user, s, u = paths
    _seed_question_row(s, 100, "OLD prompt", "OLD expl")
    _seed_question_row(u, 100, "OLD prompt", "OLD expl")
    s.commit(); u.commit()

    # Author edits seed directly (the scenario this module exists for)
    s.execute("UPDATE question SET prompt='NEW prompt', "
              "explanation='NEW expl' WHERE id=100")
    s.commit()

    from services.seed_sync import reconcile_reference_data_from_seed
    stats = reconcile_reference_data_from_seed(seed, user)
    assert stats["question_updated"] == 1
    assert stats["question_inserted"] == 0

    # Reconnect to see committed state
    u.close()
    check = sqlite3.connect(str(user))
    row = check.execute(
        "SELECT prompt, explanation FROM question WHERE id=100"
    ).fetchone()
    assert row == ("NEW prompt", "NEW expl")


def test_retire_in_seed_without_migration_reaches_user(paths):
    """As of 2026-05-31 (the curated quant audit aftermath), ``status``
    and ``provenance_json`` are USER-owned columns: seed_sync no
    longer reconciles them from seed → user. Direct seed edits to
    status are intentional dead-ends now — retirements MUST land
    via a migration in models/migrations.py so the user DB ledger
    records them and the migration's idempotent guards prevent the
    bug where ``git pull`` overwrites the local seed (back to the
    stale tracked version) and seed_sync silently flips retired rows
    back to live.

    This test pins the new contract: a status edit in the seed alone
    does NOT cross over to the user DB.
    """
    seed, user, s, u = paths
    _seed_question_row(s, 200, "p", "e", status="live")
    _seed_question_row(u, 200, "p", "e", status="live")
    s.commit(); u.commit()

    s.execute("UPDATE question SET status='retired' WHERE id=200")
    s.commit()

    from services.seed_sync import reconcile_reference_data_from_seed
    reconcile_reference_data_from_seed(seed, user)

    u.close()
    check = sqlite3.connect(str(user))
    assert check.execute(
        "SELECT status FROM question WHERE id=200"
    ).fetchone()[0] == "live", (
        "status is USER-owned; seed-only status edits must NOT propagate"
    )


def test_new_question_in_seed_is_inserted_to_user(paths):
    seed, user, s, u = paths
    _seed_question_row(s, 300, "p", "e")
    _seed_question_row(s, 301, "NEW Q", "NEW expl")
    _seed_question_row(u, 300, "p", "e")  # user only has 300, not 301
    s.commit(); u.commit()

    from services.seed_sync import reconcile_reference_data_from_seed
    stats = reconcile_reference_data_from_seed(seed, user)
    assert stats["question_inserted"] == 1

    u.close()
    check = sqlite3.connect(str(user))
    row = check.execute(
        "SELECT prompt, explanation FROM question WHERE id=301"
    ).fetchone()
    assert row == ("NEW Q", "NEW expl")


# ── User-state preservation ────────────────────────────────────────────

def test_user_state_columns_on_question_preserved(paths):
    """pretest_* / irt_* / created_at must survive a sync."""
    seed, user, s, u = paths
    _seed_question_row(s, 400, "prompt", "expl")
    _seed_question_row(u, 400, "prompt", "expl")
    # User accumulates pretest stats on their side
    u.execute(
        "UPDATE question SET pretest_n_responses=5, pretest_p_correct=0.6, "
        "irt_b_estimate=1.23, created_at='2020-01-01 00:00:00' WHERE id=400"
    )
    # Seed has different (default) values for the same columns
    s.execute(
        "UPDATE question SET pretest_n_responses=0, pretest_p_correct=NULL, "
        "irt_b_estimate=NULL, created_at='2026-05-06 00:00:00' WHERE id=400"
    )
    s.execute("UPDATE question SET prompt='NEW prompt' WHERE id=400")
    s.commit(); u.commit()

    from services.seed_sync import reconcile_reference_data_from_seed
    reconcile_reference_data_from_seed(seed, user)

    u.close()
    check = sqlite3.connect(str(user))
    row = check.execute(
        "SELECT prompt, pretest_n_responses, pretest_p_correct, "
        "irt_b_estimate, created_at FROM question WHERE id=400"
    ).fetchone()
    # Seed-authored: synced
    assert row[0] == "NEW prompt"
    # User-authored: preserved
    assert row[1] == 5
    assert row[2] == 0.6
    assert row[3] == 1.23
    assert row[4] == "2020-01-01 00:00:00"


def test_response_table_untouched_by_sync(paths):
    """User's answer log must survive the reconcile unscathed."""
    seed, user, s, u = paths
    _seed_question_row(s, 500, "p", "e")
    _seed_question_row(u, 500, "p", "e")
    u.execute(
        "INSERT INTO response (question_id, is_correct) VALUES (500, 1)")
    u.commit()

    s.execute("UPDATE question SET status='retired' WHERE id=500")
    s.commit()

    from services.seed_sync import reconcile_reference_data_from_seed
    reconcile_reference_data_from_seed(seed, user)

    u.close()
    check = sqlite3.connect(str(user))
    assert check.execute(
        "SELECT COUNT(*) FROM response WHERE question_id=500"
    ).fetchone()[0] == 1


# ── Option / stimulus sync ─────────────────────────────────────────────

def test_options_refresh_fully_from_seed(paths):
    """Seed rewriting options (adding / removing / editing) should
    reach the user DB on next sync."""
    seed, user, s, u = paths
    _seed_question_row(s, 600, "p", "e")
    _seed_question_row(u, 600, "p", "e")
    # Seed ships A (correct) + B + C
    for lab, text, correct in [("A", "right", 1), ("B", "wrong1", 0),
                                ("C", "wrong2", 0)]:
        s.execute(
            "INSERT INTO questionoption (question_id, option_label, "
            "option_text, is_correct) VALUES (600, ?, ?, ?)",
            (lab, text, correct),
        )
    # User has a stale pair with A labeled incorrect
    u.execute(
        "INSERT INTO questionoption (question_id, option_label, "
        "option_text, is_correct) VALUES (600, 'A', 'OLD', 0)")
    u.execute(
        "INSERT INTO questionoption (question_id, option_label, "
        "option_text, is_correct) VALUES (600, 'B', 'OLD', 1)")
    s.commit(); u.commit()

    from services.seed_sync import reconcile_reference_data_from_seed
    reconcile_reference_data_from_seed(seed, user)

    u.close()
    check = sqlite3.connect(str(user))
    opts = sorted(check.execute(
        "SELECT option_label, option_text, is_correct FROM questionoption "
        "WHERE question_id=600 ORDER BY option_label"
    ).fetchall())
    assert opts == [("A", "right", 1), ("B", "wrong1", 0), ("C", "wrong2", 0)]


def test_stimulus_content_sync(paths):
    """Seed updates to stimulus.content propagate to the user."""
    seed, user, s, u = paths
    for c in (s, u):
        c.execute(
            "INSERT INTO stimulus (id, stimulus_type, title, content) "
            "VALUES (1, 'graph', 'T', 'OLD content')")
    _seed_question_row(s, 700, "p", "e", stimulus_id=1)
    _seed_question_row(u, 700, "p", "e", stimulus_id=1)
    s.execute("UPDATE stimulus SET content='NEW content' WHERE id=1")
    s.commit(); u.commit()

    from services.seed_sync import reconcile_reference_data_from_seed
    reconcile_reference_data_from_seed(seed, user)

    u.close()
    check = sqlite3.connect(str(user))
    assert check.execute(
        "SELECT content FROM stimulus WHERE id=1"
    ).fetchone()[0] == "NEW content"


# ── Fingerprint fast-skip ──────────────────────────────────────────────

def test_reconcile_skips_when_fingerprint_matches(paths):
    seed, user, s, u = paths
    _seed_question_row(s, 800, "p", "e")
    _seed_question_row(u, 800, "p", "e")
    s.commit(); u.commit()
    s.close(); u.close()

    from services.seed_sync import reconcile_if_stale
    # First run does the sync
    stats1 = reconcile_if_stale(seed, user)
    assert "question_updated" in stats1
    # Second run (seed unchanged) short-circuits
    stats2 = reconcile_if_stale(seed, user)
    assert stats2 == {"skipped": "fingerprint_match"}


def test_reconcile_reruns_after_seed_mtime_bumps(paths, tmp_path):
    seed, user, s, u = paths
    _seed_question_row(s, 900, "p", "e")
    _seed_question_row(u, 900, "p", "e")
    s.commit(); u.commit()
    s.close(); u.close()

    from services.seed_sync import reconcile_if_stale
    reconcile_if_stale(seed, user)

    # Simulate a `git pull` that touched the seed file + edited content
    s2 = sqlite3.connect(str(seed))
    s2.execute("UPDATE question SET prompt='fresh prompt' WHERE id=900")
    s2.commit()
    s2.close()
    # Bump mtime to simulate git pull's file rewrite
    future = time.time() + 60
    import os
    os.utime(seed, (future, future))

    stats = reconcile_if_stale(seed, user)
    assert stats.get("question_updated") == 1
    check = sqlite3.connect(str(user))
    assert check.execute(
        "SELECT prompt FROM question WHERE id=900"
    ).fetchone()[0] == "fresh prompt"


# ── Safety / edge cases ────────────────────────────────────────────────

def test_reconcile_is_atomic_under_failure(paths, monkeypatch):
    """If the reconcile raises mid-way, the user DB should be unchanged."""
    seed, user, s, u = paths
    _seed_question_row(s, 1000, "p", "e")
    _seed_question_row(u, 1000, "OLD", "OLD")
    s.commit(); u.commit()
    s.close(); u.close()

    # Force the replace step to blow up after the question update lands.
    import services.seed_sync as mod
    orig = mod._replace_table_from_seed
    def boom(seed_conn, user_conn, table):
        if table == "questionoption":
            raise RuntimeError("simulated crash")
        return orig(seed_conn, user_conn, table)
    monkeypatch.setattr(mod, "_replace_table_from_seed", boom)

    with pytest.raises(RuntimeError):
        mod.reconcile_reference_data_from_seed(seed, user)

    # User DB should still show OLD prompt because the transaction rolled back.
    check = sqlite3.connect(str(user))
    assert check.execute(
        "SELECT prompt FROM question WHERE id=1000"
    ).fetchone()[0] == "OLD"


def test_missing_seed_returns_skipped(tmp_path):
    user = tmp_path / "user.db"
    _make_db(user).close()
    from services.seed_sync import reconcile_reference_data_from_seed
    result = reconcile_reference_data_from_seed(tmp_path / "nonexistent.db",
                                                  user)
    assert result == {"skipped": "no_seed"}
