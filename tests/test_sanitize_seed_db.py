"""
Tests for ``scripts/sanitize_seed_db.py`` and
``scripts/verify_seed_clean.py``.

Guards two invariants:
  1. The sanitizer wipes every user-state table without touching any
     content table.
  2. The verifier exits 0 on a clean DB and 1 on one that ships with
     user data.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sanitize_seed_db  # noqa: E402
from sanitize_seed_db import (  # noqa: E402
    USER_STATE_TABLES,
    CONTENT_TABLES,
    SanitizeError,
    sanitize,
)


def _build_fixture_db(path: Path) -> None:
    """Create a minimal schema mirroring the production tables and seed
    each one with a single row so we can prove sanitize wipes exactly
    the user-state subset.
    """
    conn = sqlite3.connect(str(path))
    try:
        # Build every table named in the deny-list + allow-list. Schema
        # is intentionally minimal — sanitize only needs COUNT(*) and
        # DELETE FROM <table>, so we don't recreate the production
        # Peewee schema.
        for table in USER_STATE_TABLES + CONTENT_TABLES:
            conn.execute(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, payload TEXT)"
            )
            # Put two rows in each so we can tell "wiped" from "only one
            # was there".
            conn.execute(
                f"INSERT INTO {table} (payload) VALUES (?), (?)",
                ("row-a", "row-b"),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "gre_mock.db"
    _build_fixture_db(db)
    return db


def _count(db: Path, table: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


# ── sanitize_seed_db ────────────────────────────────────────────────────


def test_sanitize_apply_wipes_user_tables(seeded_db: Path) -> None:
    # Precondition: every user-state table has 2 rows.
    for t in USER_STATE_TABLES:
        assert _count(seeded_db, t) == 2, t

    sanitize(seeded_db, apply=True)

    for t in USER_STATE_TABLES:
        assert _count(seeded_db, t) == 0, (
            f"{t} should be wiped after --apply, got {_count(seeded_db, t)}"
        )


def test_sanitize_apply_preserves_content_tables(seeded_db: Path) -> None:
    sanitize(seeded_db, apply=True)
    for t in CONTENT_TABLES:
        assert _count(seeded_db, t) == 2, (
            f"{t} is a content table and must survive sanitize, "
            f"got {_count(seeded_db, t)}"
        )


def test_sanitize_dry_run_does_not_modify(seeded_db: Path) -> None:
    before = {t: _count(seeded_db, t)
              for t in USER_STATE_TABLES + CONTENT_TABLES}
    sanitize(seeded_db, apply=False)
    after = {t: _count(seeded_db, t)
             for t in USER_STATE_TABLES + CONTENT_TABLES}
    assert before == after


def test_sanitize_is_idempotent(seeded_db: Path) -> None:
    sanitize(seeded_db, apply=True)
    # Second apply should be a no-op and not raise.
    sanitize(seeded_db, apply=True)
    for t in USER_STATE_TABLES:
        assert _count(seeded_db, t) == 0


def test_sanitize_refuses_gre_user_db(tmp_path: Path) -> None:
    user_db = tmp_path / "gre_user.db"
    _build_fixture_db(user_db)
    with pytest.raises(SanitizeError, match="runtime user DB"):
        sanitize(user_db, apply=True)
    # File content must be untouched.
    for t in USER_STATE_TABLES:
        assert _count(user_db, t) == 2


def test_sanitize_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SanitizeError, match="DB not found"):
        sanitize(tmp_path / "nope.db", apply=True)


def test_sanitize_returns_before_after_counts(seeded_db: Path) -> None:
    counts = sanitize(seeded_db, apply=True)
    # Every user-state row goes 2 -> 0; every content row stays 2 -> 2.
    for t in USER_STATE_TABLES:
        assert counts[t] == (2, 0), t
    for t in CONTENT_TABLES:
        assert counts[t] == (2, 2), t


# ── verify_seed_clean ───────────────────────────────────────────────────


def _run_verify(db_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "verify_seed_clean.py"),
         "--db", str(db_path)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )


def test_verify_clean_db_exits_zero(seeded_db: Path) -> None:
    sanitize(seeded_db, apply=True)
    result = _run_verify(seeded_db)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_verify_dirty_db_exits_one(seeded_db: Path) -> None:
    # seeded_db still has 2 rows in every user-state table.
    result = _run_verify(seeded_db)
    assert result.returncode == 1, (
        f"expected 1, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "ships with user-state data" in result.stderr
    # Each populated user-state table must appear in the failure report
    # so the dev knows what to fix.
    for t in USER_STATE_TABLES:
        assert t in result.stderr


def test_verify_missing_db_exits_two(tmp_path: Path) -> None:
    result = _run_verify(tmp_path / "does-not-exist.db")
    assert result.returncode == 2
    assert "DB not found" in result.stderr


# ── Snapshot of the shipped seed ────────────────────────────────────────


SHIPPED_SEED = PROJECT_ROOT / "data" / "gre_mock.db"


@pytest.mark.skipif(not SHIPPED_SEED.exists(),
                    reason="shipped seed DB not present (fresh clone before "
                           "LFS fetch)")
def test_shipped_seed_is_clean() -> None:
    """The gre_mock.db committed to the tree must have zero user-state
    rows. If this fails, run ``scripts/sanitize_seed_db.py --apply``
    before committing.
    """
    conn = sqlite3.connect(str(SHIPPED_SEED))
    try:
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        dirty = []
        for table in USER_STATE_TABLES:
            if table not in existing:
                continue  # schema hasn't caught up yet
            count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if count:
                dirty.append((table, count))
    finally:
        conn.close()

    assert not dirty, (
        f"gre_mock.db ships with user data: {dirty}. "
        "Run scripts/sanitize_seed_db.py --apply before committing."
    )


@pytest.mark.skipif(not SHIPPED_SEED.exists(),
                    reason="shipped seed DB not present")
def test_shipped_seed_still_has_content() -> None:
    """Guardrail: the sanitize step must not touch content tables. If a
    future refactor moves a content table into the deny-list, this test
    catches the resulting empty question bank before it ships.
    """
    # Tight lower bounds so small legitimate growth/churn doesn't flap
    # the test; we only assert there are "plenty" of rows.
    minimums = {
        "question": 1000,
        "questionoption": 2000,
        "stimulus": 100,
        "awaprompt": 50,
        "vocabword": 1000,
        "lesson": 10,
    }
    conn = sqlite3.connect(str(SHIPPED_SEED))
    try:
        for table, floor in minimums.items():
            count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            assert count >= floor, (
                f"{table} has {count} rows, expected >= {floor}. "
                "Sanitize may have over-reached."
            )
    finally:
        conn.close()
