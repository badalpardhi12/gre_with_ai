"""
Tests for `scripts.repair_di_chart_integrity`.

Builds a miniature sqlite DB mimicking the production `question` /
`stimulus` shape, seeds a few of each defect class, runs the repair, and
asserts the expected post-state.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# Minimal subset of columns the repair script reads / writes.
_CREATE_STIMULUS = """
CREATE TABLE stimulus (
    id INTEGER PRIMARY KEY,
    stimulus_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    render_spec TEXT NOT NULL DEFAULT ''
);
"""

_CREATE_QUESTION = """
CREATE TABLE question (
    id INTEGER PRIMARY KEY,
    subtype TEXT NOT NULL,
    stimulus_id INTEGER,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    review_notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""


def _seed(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.executescript(_CREATE_STIMULUS + _CREATE_QUESTION)

    # Stimulus 1: empty chart shell
    c.execute(
        "INSERT INTO stimulus (id, stimulus_type, title, content, render_spec)"
        " VALUES (1, 'graph', 'empty_cluster', '', '')"
    )
    # Stimulus 2: populated chart (retired-twin will carry this)
    c.execute(
        "INSERT INTO stimulus (id, stimulus_type, title, content, render_spec)"
        " VALUES (2, 'graph', 'populated_cluster', "
        "'<img src=\"data:image/png;base64,xxxx\">' || printf('%0200d', 0), '')"
    )
    # Stimulus 3: empty, no twin anywhere
    c.execute(
        "INSERT INTO stimulus (id, stimulus_type, title, content, render_spec)"
        " VALUES (3, 'graph', 'lost_cluster', '', '')"
    )

    # Q10: live, points at empty stim 1 (prompt-twinned to retired Q11)
    c.execute(
        "INSERT INTO question (id, subtype, stimulus_id, prompt, status)"
        " VALUES (10, 'data_interp', 1, 'In year X, ratio was?', 'live')"
    )
    # Q11: retired, same prompt, populated stim 2
    c.execute(
        "INSERT INTO question (id, subtype, stimulus_id, prompt, status)"
        " VALUES (11, 'data_interp', 2, 'In year X, ratio was?', 'retired')"
    )

    # Q12: live, points at empty stim 1, NO prompt twin — but cluster has
    # exactly one populated option (stim 2) so it should inherit.
    c.execute(
        "INSERT INTO question (id, subtype, stimulus_id, prompt, status)"
        " VALUES (12, 'data_interp', 1, 'What is the trend?', 'live')"
    )

    # Q13: live, points at empty stim 3 with no populated sibling anywhere
    # → retire.
    c.execute(
        "INSERT INTO question (id, subtype, stimulus_id, prompt, status)"
        " VALUES (13, 'mcq_single', 3, 'In the graph above, find X.', 'live')"
    )

    # Q14: live, stimulus_id IS NULL, prompt cites "figure above" → retire.
    c.execute(
        "INSERT INTO question (id, subtype, stimulus_id, prompt, status)"
        " VALUES (14, 'mcq_single', NULL, 'In the figure above, y = ?', 'live')"
    )

    # Q15: live, NULL stim but NO figure reference → untouched.
    c.execute(
        "INSERT INTO question (id, subtype, stimulus_id, prompt, status)"
        " VALUES (15, 'mcq_single', NULL, 'If x = 5, what is 2x + 3?', 'live')"
    )

    # Q16: live, healthy stimulus → untouched.
    c.execute(
        "INSERT INTO question (id, subtype, stimulus_id, prompt, status)"
        " VALUES (16, 'data_interp', 2, 'Healthy DI question', 'live')"
    )

    conn.commit()
    conn.close()


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "chart.db"
    _seed(db)
    return db


def test_dry_run_reports_counts_and_does_not_mutate(seeded_db, monkeypatch):
    import scripts.repair_di_chart_integrity as repair
    importlib.reload(repair)

    conn = sqlite3.connect(seeded_db)
    try:
        result = repair.apply_fixes(conn, dry_run=True)
    finally:
        conn.close()

    assert result["relinked"] == 2       # Q10 (direct), Q12 (single-option inherit)
    assert result["retired_empty_stim"] == 1  # Q13
    assert result["retired_null_stim"] == 1   # Q14
    assert result["total_changed"] == 4

    # DB state untouched
    conn = sqlite3.connect(seeded_db)
    rows = dict(conn.execute(
        "SELECT id, status || ':' || IFNULL(stimulus_id, 'NULL') FROM question"
    ).fetchall())
    conn.close()
    assert rows[10] == "live:1"
    assert rows[12] == "live:1"
    assert rows[13] == "live:3"
    assert rows[14] == "live:NULL"
    assert rows[15] == "live:NULL"
    assert rows[16] == "live:2"


def test_apply_relinks_retires_and_is_idempotent(
    seeded_db, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "audits").mkdir()
    (tmp_path / "scripts").symlink_to(Path(PROJECT_ROOT) / "scripts")

    # Copy the seeded DB to the expected relative path so the ledger writer
    # inside apply_fixes has somewhere legal to land its CSV.
    target_db = tmp_path / "data" / "gre_user.db"
    target_db.write_bytes(seeded_db.read_bytes())

    import scripts.repair_di_chart_integrity as repair
    importlib.reload(repair)

    conn = sqlite3.connect(target_db)
    try:
        first = repair.apply_fixes(conn, dry_run=False)
    finally:
        conn.close()
    assert first["total_changed"] == 4

    conn = sqlite3.connect(target_db)
    rows = dict(conn.execute(
        "SELECT id, status || ':' || IFNULL(stimulus_id, 'NULL') FROM question"
    ).fetchall())
    notes = dict(conn.execute(
        "SELECT id, review_notes FROM question"
    ).fetchall())
    conn.close()

    # Q10: relinked 1 -> 2
    assert rows[10] == "live:2"
    assert "stim 1 -> 2" in notes[10]
    # Q12: single-option inherit 1 -> 2
    assert rows[12] == "live:2"
    # Q13: retired, empty-stim orphan
    assert rows[13].startswith("retired:")
    assert "empty chart stimulus" in notes[13]
    # Q14: retired, null-stim figure-ref
    assert rows[14].startswith("retired:")
    assert "figure but stimulus_id is NULL" in notes[14]
    # Unaffected
    assert rows[15] == "live:NULL"
    assert rows[16] == "live:2"

    # Ledger exists and has four rows
    ledger = tmp_path / "data" / "audits" / "di_chart_integrity_2026_04_28_ledger.csv"
    assert ledger.exists()
    import csv
    with ledger.open() as fh:
        entries = list(csv.DictReader(fh))
    assert len(entries) == 4
    actions = {e["action"] for e in entries}
    assert actions == {"relink", "retire_empty_stim", "retire_null_stim"}

    # Idempotent: second pass finds nothing to do.
    conn = sqlite3.connect(target_db)
    try:
        second = repair.apply_fixes(conn, dry_run=True)
    finally:
        conn.close()
    assert second["total_changed"] == 0
