"""Reconcile reference data from the shipped seed DB onto the runtime user DB.

Context
-------
``config.py`` copies ``data/gre_mock.db`` → ``data/gre_user.db`` the
first time the app launches, after which the user DB is independent.
``apply_pending_migrations`` at each launch replays anything that's
registered in ``MIGRATIONS``, but seed edits that DON'T have a
corresponding migration (prompt/explanation rewrites, new options,
replaced stimulus images, newly-added questions, etc.) never reach
existing user DBs. Users who ``git pull`` notice content is stale.

This module bridges that gap. On each launch, after migrations run, we
open the shipped seed alongside the user DB and copy reference-data
rows that differ. The runtime user-state tables (``response``,
``session``, ``userstats``, ``schemamigration``, ``awasubmission``,
etc.) are NEVER touched — only the tables that carry the questions,
options, numeric answers, and stimulus content the user sees.

Per-column policy on ``question``
---------------------------------
A handful of columns on ``question`` track per-user state that the
seed doesn't own: the synthetic-item pretest counters
(``pretest_started_at``, ``pretest_n_responses``, ``pretest_p_correct``,
``pretest_disc_proxy``), the IRT estimates (``irt_b_estimate``,
``irt_a_estimate``), and ``created_at`` (the row's first-import
timestamp on this machine). Those are preserved; every other column
is overwritten from the seed so stem rewrites, retires, difficulty
tweaks, etc. land on the user DB.

Fast-skip
---------
Most launches run against the same seed the last sync saw. We
fingerprint the seed file by ``(mtime_ns, size)`` and store it in a
small single-row ``sync_state`` table in the user DB. When the
fingerprint matches, the reconcile is a pure metadata check and
returns instantly.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from config import DB_PATH, SEED_DB_PATH
from services.log import get_logger

logger = get_logger("seed_sync")


# Columns on ``question`` whose seed value we should NOT clobber on the
# user DB — they accumulate per-user state (pretest stats, IRT
# estimates) or are the user's first-copy timestamp.
_QUESTION_USER_OWNED_COLS = frozenset({
    "id",  # primary key — never update
    "created_at",
    "pretest_started_at",
    "pretest_n_responses",
    "pretest_p_correct",
    "pretest_disc_proxy",
    "irt_b_estimate",
    "irt_a_estimate",
})


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _ensure_sync_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_state ("
        "  key TEXT PRIMARY KEY,"
        "  value TEXT NOT NULL"
        ")"
    )


def _seed_fingerprint(seed_path: Path) -> str:
    """Short stable identifier for the seed's current content.

    Uses ``(mtime_ns, size)`` rather than hashing the whole file — the
    seed is ~23 MB and the read cost would dominate the reconcile
    skip-path it's meant to gate. Git preserves mtime on checkout
    across normal workflows, but any seed edit (including `git pull`
    that touches the file) bumps the mtime.
    """
    st = seed_path.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def _get_last_sync_fp(conn: sqlite3.Connection) -> str | None:
    _ensure_sync_state_table(conn)
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key='seed_fingerprint'"
    ).fetchone()
    return row[0] if row else None


def _set_last_sync_fp(conn: sqlite3.Connection, fp: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES ('seed_fingerprint', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (fp,),
    )


def _reconcile_question(seed: sqlite3.Connection,
                        user: sqlite3.Connection) -> tuple[int, int]:
    """UPDATE seed-authored columns on shared rows; INSERT new qids.

    Returns ``(updated_count, inserted_count)``.
    """
    cols = _table_columns(seed, "question")
    sync_cols = [c for c in cols if c not in _QUESTION_USER_OWNED_COLS]

    user_qids = {r[0] for r in user.execute("SELECT id FROM question")}

    updated = 0
    inserted = 0

    # Shared rows: UPDATE seed-authored columns only.
    set_clause = ", ".join(f"{c}=?" for c in sync_cols)
    seed_cursor = seed.execute(
        f"SELECT id, {', '.join(sync_cols)} FROM question"
    )
    to_insert: list[int] = []
    for row in seed_cursor:
        qid = row[0]
        if qid in user_qids:
            user.execute(
                f"UPDATE question SET {set_clause} WHERE id=?",
                (*row[1:], qid),
            )
            updated += 1
        else:
            to_insert.append(qid)

    # New rows: full-column INSERT. Done after the UPDATE loop so the
    # SELECT cursor on the seed stays clean.
    if to_insert:
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(cols)
        for qid in to_insert:
            full_row = seed.execute(
                f"SELECT {col_list} FROM question WHERE id=?", (qid,)
            ).fetchone()
            user.execute(
                f"INSERT INTO question ({col_list}) VALUES ({placeholders})",
                full_row,
            )
            inserted += 1

    return updated, inserted


def _replace_table_from_seed(
    seed: sqlite3.Connection,
    user: sqlite3.Connection,
    table: str,
) -> int:
    """DELETE all user rows in ``table`` and copy every row from seed.

    Safe for pure reference-data tables whose rows have no foreign
    references from user-state tables (or whose references are stable
    across the delete + re-insert because primary keys are preserved).
    Returns the number of rows inserted.
    """
    cols = _table_columns(seed, table)
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    user.execute(f"DELETE FROM {table}")
    inserted = 0
    for row in seed.execute(f"SELECT {col_list} FROM {table}"):
        user.execute(
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
            row,
        )
        inserted += 1
    return inserted


def reconcile_reference_data_from_seed(
    seed_path: Path = SEED_DB_PATH,
    user_path: Path = DB_PATH,
) -> dict:
    """Bring reference-data tables on the user DB in line with the seed.

    - ``question``: UPDATE seed-authored columns on shared rows,
      INSERT new qids, preserve pretest/IRT state on the user side.
    - ``stimulus`` / ``questionoption`` / ``numericanswer``: full
      wipe-and-replace. These are pure reference data; the user's
      ``response`` table links to ``question.id`` (not option id), so
      the delete is non-destructive to user state.

    Not synced: ``response``, ``session``, ``userstats``, ``itemstats``,
    ``scoringresult``, ``sectionresult``, ``diagnosticresult``,
    ``awasubmission``, ``awaresult``, ``flashcardreview``,
    ``masteryrecord``, ``questionflag``, ``studyplan``,
    ``telemetryevent``, ``schemamigration``, ``sync_state``.

    The write is atomic — a single transaction — so a mid-reconcile
    crash leaves the user DB unchanged rather than half-synced.
    """
    if not seed_path.exists():
        logger.info("seed not present at %s; skipping reference-data sync",
                    seed_path)
        return {"skipped": "no_seed"}
    if not user_path.exists():
        logger.info("user DB not present at %s; first-launch bootstrap "
                    "will handle the initial copy",
                    user_path)
        return {"skipped": "no_user_db"}

    seed = sqlite3.connect(str(seed_path))
    user = sqlite3.connect(str(user_path))
    try:
        # Make sure the bookkeeping table exists before the transaction
        # so the fingerprint write at the end doesn't fail on a first
        # sync of a user DB that never went through ``reconcile_if_stale``.
        _ensure_sync_state_table(user)
        user.execute("BEGIN")
        q_updated, q_inserted = _reconcile_question(seed, user)
        stim_n = _replace_table_from_seed(seed, user, "stimulus")
        opt_n = _replace_table_from_seed(seed, user, "questionoption")
        num_n = _replace_table_from_seed(seed, user, "numericanswer")
        _set_last_sync_fp(user, _seed_fingerprint(seed_path))
        user.commit()
    except Exception:
        user.rollback()
        raise
    finally:
        seed.close()
        user.close()

    stats = {
        "question_updated": q_updated,
        "question_inserted": q_inserted,
        "stimulus_replaced": stim_n,
        "questionoption_replaced": opt_n,
        "numericanswer_replaced": num_n,
    }
    logger.info("seed sync complete: %s", stats)
    return stats


def reconcile_if_stale(
    seed_path: Path = SEED_DB_PATH,
    user_path: Path = DB_PATH,
) -> dict:
    """Run the reconcile only if the seed fingerprint has changed.

    Called from ``init_db`` after migrations. The first launch after a
    ``git pull`` that updated the seed does the full reconcile;
    subsequent launches on the same seed short-circuit in <1 ms.
    """
    if not seed_path.exists() or not user_path.exists():
        return {"skipped": "missing_path"}

    current_fp = _seed_fingerprint(seed_path)
    user = sqlite3.connect(str(user_path))
    try:
        last_fp = _get_last_sync_fp(user)
    finally:
        user.close()

    if last_fp == current_fp:
        return {"skipped": "fingerprint_match"}

    return reconcile_reference_data_from_seed(seed_path, user_path)
