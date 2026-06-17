"""Reconcile reference data from the shipped seed DB onto the runtime user DB.

Context
-------
``config.py`` copies ``data/gre_mock.db`` → ``data/gre_user.db`` the
first time the app launches, after which the user DB is independent.
``apply_pending_migrations`` at each launch replays anything that's
registered in ``MIGRATIONS``, but seed edits that DON'T have a
corresponding migration (prompt/explanation rewrites, new options,
replaced stimulus images, newly-added questions, new lessons / vocab /
AWA prompts, etc.) never reach existing user DBs. Users who ``git pull``
notice content is stale.

This module bridges that gap. On each launch, after migrations run, we
open the shipped seed alongside the user DB and copy reference-data
rows that differ. The runtime user-state tables (``response``,
``session``, ``userstats``, ``schemamigration``, ``awasubmission``,
etc.) are NEVER touched — only the tables that carry the questions,
options, numeric answers, stimulus, lessons, vocab, and AWA prompts the
user sees.

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

Schema-drift tolerance
----------------------
``question`` columns to sync are intersected with the columns the USER
table actually has. If a ``git pull`` ships a seed with a column the
user DB hasn't migrated to yet (or the user DB has a column the seed
lacks), we sync only the columns present in BOTH tables instead of
throwing ``no such column`` and aborting the whole reconcile. Same care
for the copy/replace tables (insert only the shared columns).

Change detection
----------------
Most launches run against the same seed the last sync saw. We need a
signature that is fast on the skip-path but can NEVER false-match when
the content actually changed. The old ``(mtime_ns, size)`` fingerprint
failed that: ``git checkout``/``git pull`` doesn't guarantee a changed
mtime, and a same-byte-size content edit produced an identical
fingerprint, silently skipping the reconcile. We now sign the seed by
``sha256:<hex>``. A cheap size pre-check is folded into the comparison:
if the stored signature's size differs from the current file size we
reconcile without even hashing; otherwise we hash (~tens of ms on the
~23 MB seed) to confirm. The signature is stored in a small single-row
``sync_state`` table in the user DB. Stale old-format values (the bare
``mtime:size`` string from before this change) never equal a
``sha256:...`` signature, so they force exactly one reconcile rather
than crashing.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

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
    # Status + provenance are migration-managed (the answer-key drift,
    # quant audit, and targeted-issue migrations 036/037/038 retire
    # rows + stamp provenance via models/migrations.py). They MUST NOT
    # be reconciled from the seed: when a user pulls a new commit, the
    # tracked seed lags behind the migration's effects and a naive
    # reconciliation flips ``status='retired'`` rows back to ``live``,
    # silently undoing every retirement we shipped. The migrations
    # themselves are the source of truth — seed_sync just protects
    # the columns they own.
    "status",
    "provenance_json",
})


# Pure reference-data tables with NO inbound foreign key from any
# user-state table — safe to wipe-and-replace wholesale.
#   stimulus        ← question.stimulus_id (content table, not user state)
#   questionoption  ← (none)
#   numericanswer   ← (none)
#   lesson          ← (none)
#   vocabroot       ← (none)
# ``response`` links to ``question.id`` (preserved by the question
# UPSERT), never to an option/numeric/stimulus row id, so wiping these
# is non-destructive to the user's answer log.
_WIPE_AND_REPLACE_TABLES = (
    "stimulus",
    "questionoption",
    "numericanswer",
    "lesson",
    "vocabroot",
)


# Reference tables that DO carry an inbound FK from a user-state table,
# so they can't be safely wiped (the DELETE would orphan / cascade user
# rows). We UPSERT them by primary key instead: shared ids are UPDATEd,
# new ids are INSERTed, ids the user has but the seed dropped are left
# alone. Primary keys are stable across the seed regenerate, so the
# user-state FK stays valid.
#   awaprompt  ← awasubmission.prompt_id   (user's essays)
#   vocabword  ← flashcardreview.word_id   (user's SRS state)
_UPSERT_BY_PK_TABLES = (
    "awaprompt",
    "vocabword",
)


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _shared_columns(seed: sqlite3.Connection,
                    user: sqlite3.Connection,
                    table: str) -> List[str]:
    """Columns present in BOTH the seed and user copies of ``table``.

    Preserves the seed's column order. Logs (once per table) any columns
    that exist on only one side so a schema drift is visible without
    aborting the reconcile.
    """
    seed_cols = _table_columns(seed, table)
    user_cols = set(_table_columns(user, table))
    shared = [c for c in seed_cols if c in user_cols]
    seed_only = [c for c in seed_cols if c not in user_cols]
    user_only = [c for c in user_cols if c not in set(seed_cols)]
    if seed_only or user_only:
        logger.warning(
            "schema drift on %s: seed-only cols %s (not synced — user DB "
            "lacks them); user-only cols %s (left untouched). Syncing the "
            "%d shared columns.",
            table, seed_only, sorted(user_only), len(shared),
        )
    return shared


def _ensure_sync_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_state ("
        "  key TEXT PRIMARY KEY,"
        "  value TEXT NOT NULL"
        ")"
    )


def _seed_signature(seed_path: Path) -> str:
    """Content signature for the seed: ``sha256:<size>:<hex>``.

    Hashing the whole file (~23 MB → tens of ms) is the only detector
    that can't false-match: a content edit always changes the digest,
    even when ``git`` preserves the mtime and the byte size is
    unchanged. The size is embedded so the skip-path can short-circuit
    to "definitely reconcile" on a size mismatch without re-hashing (see
    ``_signatures_match``).
    """
    h = hashlib.sha256()
    size = 0
    with open(seed_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return "sha256:%d:%s" % (size, h.hexdigest())


def _signatures_match(stored: Optional[str], seed_path: Path) -> bool:
    """True iff the seed is byte-for-byte what ``stored`` recorded.

    Fast path: if ``stored`` isn't a current-format ``sha256:<size>:...``
    value (None, or a legacy ``mtime:size`` string), it can't match — we
    return False and force a reconcile. If the embedded size differs from
    the current file size, the content definitely changed, so we return
    False WITHOUT hashing. Only when the size matches do we pay for the
    hash to confirm.
    """
    if not stored or not stored.startswith("sha256:"):
        return False
    parts = stored.split(":", 2)
    if len(parts) != 3:
        return False
    try:
        stored_size = int(parts[1])
    except ValueError:
        return False
    try:
        actual_size = seed_path.stat().st_size
    except OSError:
        return False
    if stored_size != actual_size:
        return False  # size differs → content changed; no need to hash
    return stored == _seed_signature(seed_path)


def _get_last_sync_sig(conn: sqlite3.Connection) -> Optional[str]:
    _ensure_sync_state_table(conn)
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key='seed_fingerprint'"
    ).fetchone()
    return row[0] if row else None


def _set_last_sync_sig(conn: sqlite3.Connection, sig: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES ('seed_fingerprint', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (sig,),
    )


def _reconcile_question(seed: sqlite3.Connection,
                        user: sqlite3.Connection) -> Tuple[int, int]:
    """UPDATE seed-authored columns on shared rows; INSERT new qids.

    Returns ``(updated_count, inserted_count)``.

    Schema-drift safe: only columns present in BOTH the seed and user
    ``question`` tables are touched, so a seed that ships a new column
    the user DB hasn't migrated to yet doesn't abort the reconcile.
    """
    shared = _shared_columns(seed, user, "question")
    if "id" not in shared:
        # Without a shared primary key we can't safely match rows.
        raise RuntimeError("question.id missing from seed and/or user DB")
    sync_cols = [c for c in shared if c not in _QUESTION_USER_OWNED_COLS]

    user_qids = {r[0] for r in user.execute("SELECT id FROM question")}

    updated = 0
    inserted = 0

    # Shared rows: UPDATE seed-authored columns only.
    set_clause = ", ".join("%s=?" % c for c in sync_cols)
    seed_cursor = seed.execute(
        "SELECT id, %s FROM question" % ", ".join(sync_cols)
    )
    to_insert: List[int] = []
    for row in seed_cursor:
        qid = row[0]
        if qid in user_qids:
            if sync_cols:  # nothing to update if seed has only user-owned cols
                user.execute(
                    "UPDATE question SET %s WHERE id=?" % set_clause,
                    (row[1:] + (qid,)),
                )
            updated += 1
        else:
            to_insert.append(qid)

    # New rows: insert only the shared columns (defaults fill the rest).
    if to_insert:
        col_list = ", ".join(shared)
        placeholders = ", ".join("?" for _ in shared)
        for qid in to_insert:
            full_row = seed.execute(
                "SELECT %s FROM question WHERE id=?" % col_list, (qid,)
            ).fetchone()
            user.execute(
                "INSERT INTO question (%s) VALUES (%s)" % (col_list, placeholders),
                full_row,
            )
            inserted += 1

    return updated, inserted


def _replace_table_from_seed(
    seed: sqlite3.Connection,
    user: sqlite3.Connection,
    table: str,
) -> int:
    """DELETE all user rows in ``table`` and copy every seed row.

    Safe only for pure reference-data tables whose rows have no foreign
    references from user-state tables. Schema-drift safe: inserts only
    the columns present in both copies. Returns rows inserted; -1 if the
    table is absent on either side (skipped).
    """
    if not _table_exists(seed, table) or not _table_exists(user, table):
        logger.info("table %s absent on seed or user DB; skipping replace",
                    table)
        return -1
    cols = _shared_columns(seed, user, table)
    if not cols:
        logger.warning("table %s has no shared columns; skipping replace",
                       table)
        return -1
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    user.execute("DELETE FROM %s" % table)
    inserted = 0
    for row in seed.execute("SELECT %s FROM %s" % (col_list, table)):
        user.execute(
            "INSERT INTO %s (%s) VALUES (%s)" % (table, col_list, placeholders),
            row,
        )
        inserted += 1
    return inserted


def _upsert_table_by_pk(
    seed: sqlite3.Connection,
    user: sqlite3.Connection,
    table: str,
    pk: str = "id",
) -> Tuple[int, int]:
    """UPSERT every seed row into ``table`` keyed by ``pk``.

    Shared pk → UPDATE the shared non-pk columns; new pk → INSERT the
    shared columns. User rows whose pk the seed no longer has are left
    in place (they may be referenced by user-state FKs). Used for
    reference tables that carry an inbound FK from a user-state table and
    therefore can't be wiped. Returns ``(updated, inserted)``; ``(-1, -1)``
    if the table is absent on either side.
    """
    if not _table_exists(seed, table) or not _table_exists(user, table):
        logger.info("table %s absent on seed or user DB; skipping upsert",
                    table)
        return -1, -1
    shared = _shared_columns(seed, user, table)
    if pk not in shared:
        logger.warning("table %s missing shared pk %s; skipping upsert",
                       table, pk)
        return -1, -1
    non_pk = [c for c in shared if c != pk]

    user_pks = {r[0] for r in user.execute("SELECT %s FROM %s" % (pk, table))}
    updated = 0
    inserted = 0

    set_clause = ", ".join("%s=?" % c for c in non_pk)
    col_list = ", ".join(shared)
    placeholders = ", ".join("?" for _ in shared)

    rows = seed.execute(
        "SELECT %s, %s FROM %s" % (pk, ", ".join(non_pk), table)
    ).fetchall() if non_pk else seed.execute(
        "SELECT %s FROM %s" % (pk, table)
    ).fetchall()

    to_insert: List = []
    for row in rows:
        key = row[0]
        if key in user_pks:
            if non_pk:
                user.execute(
                    "UPDATE %s SET %s WHERE %s=?" % (table, set_clause, pk),
                    (row[1:] + (key,)),
                )
            updated += 1
        else:
            to_insert.append(key)

    for key in to_insert:
        full_row = seed.execute(
            "SELECT %s FROM %s WHERE %s=?" % (col_list, table, pk), (key,)
        ).fetchone()
        user.execute(
            "INSERT INTO %s (%s) VALUES (%s)" % (table, col_list, placeholders),
            full_row,
        )
        inserted += 1

    return updated, inserted


def reconcile_reference_data_from_seed(
    seed_path: Path = SEED_DB_PATH,
    user_path: Path = DB_PATH,
) -> dict:
    """Bring reference-data tables on the user DB in line with the seed.

    - ``question``: UPDATE seed-authored columns on shared rows,
      INSERT new qids, preserve pretest/IRT/status state on the user side.
    - ``stimulus`` / ``questionoption`` / ``numericanswer`` / ``lesson`` /
      ``vocabroot``: full wipe-and-replace. Pure reference data with no
      inbound FK from any user-state table.
    - ``awaprompt`` / ``vocabword``: UPSERT by primary key. These carry an
      inbound FK from a user-state table (``awasubmission.prompt_id`` and
      ``flashcardreview.word_id`` respectively) so a wipe would orphan
      user rows; UPSERT preserves the referenced pk while still landing
      content edits and new rows.

    Not synced (user-state tables, never touched): ``response``,
    ``session``, ``sectionresult``, ``scoringresult``, ``diagnosticresult``,
    ``itemstats``, ``masteryrecord``, ``questionflag``, ``studyplan``,
    ``flashcardreview``, ``itemrating``, ``itemreview``,
    ``vocabcontextitem``, ``awasubmission``, ``awaresult``, ``servedlog``,
    ``telemetryevent``, ``userstats``, ``syntheticgenerationrun``,
    ``schemamigration``, ``sync_state``.

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
        # so the signature write at the end doesn't fail on a first
        # sync of a user DB that never went through ``reconcile_if_stale``.
        _ensure_sync_state_table(user)
        user.execute("BEGIN")
        q_updated, q_inserted = _reconcile_question(seed, user)

        replaced = {}
        for table in _WIPE_AND_REPLACE_TABLES:
            replaced[table] = _replace_table_from_seed(seed, user, table)

        upserted = {}
        for table in _UPSERT_BY_PK_TABLES:
            upserted[table] = _upsert_table_by_pk(seed, user, table)

        _set_last_sync_sig(user, _seed_signature(seed_path))
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
    }
    for table, n in replaced.items():
        stats["%s_replaced" % table] = n
    for table, (upd, ins) in upserted.items():
        stats["%s_updated" % table] = upd
        stats["%s_inserted" % table] = ins
    logger.info("seed sync complete: %s", stats)
    return stats


def reconcile_if_stale(
    seed_path: Path = SEED_DB_PATH,
    user_path: Path = DB_PATH,
) -> dict:
    """Run the reconcile only if the seed content has changed.

    Called from ``init_db`` after migrations. Compares the user DB's
    stored content signature against the seed file. If they match (the
    seed is byte-for-byte unchanged since the last sync) the reconcile is
    skipped; otherwise the full reconcile runs and stores the new
    signature. A stale old-format signature never matches, so it forces
    exactly one reconcile.
    """
    if not seed_path.exists() or not user_path.exists():
        return {"skipped": "missing_path"}

    user = sqlite3.connect(str(user_path))
    try:
        last_sig = _get_last_sync_sig(user)
    finally:
        user.close()

    if _signatures_match(last_sig, seed_path):
        return {"skipped": "fingerprint_match"}

    return reconcile_reference_data_from_seed(seed_path, user_path)
