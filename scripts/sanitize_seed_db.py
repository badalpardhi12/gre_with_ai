"""
Strip all user-state rows from the shipped seed database.

The seed at ``data/gre_mock.db`` is meant to ship ONLY content
(questions, stimuli, options, numeric answers, AWA prompts, lessons,
vocabulary, and the schema-migration ledger). During local development
the dev's runtime traffic bleeds into the seed — responses, sessions,
mastery, streaks, flashcard SRS state, etc. — and those rows end up
tracked in Git LFS, so every fresh clone boots pre-populated with the
dev's stats.

This script deletes every row from the user-state tables, leaves the
content tables untouched, and VACUUMs the file so the LFS pointer
actually shrinks.

Usage::

    # Preview (no writes, no lock):
    venv/bin/python scripts/sanitize_seed_db.py \
        --db data/gre_mock.db --dry-run

    # Apply (opens the DB read/write, VACUUMs when done):
    venv/bin/python scripts/sanitize_seed_db.py \
        --db data/gre_mock.db --apply

Idempotent: running ``--apply`` a second time deletes 0 rows.

Runtime DB (``data/gre_user.db``) should never be passed here. If the
script detects the file is named ``gre_user.db`` it refuses to touch it.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple


# Tables that hold per-user state. Wiping them from the seed means fresh
# clones start clean. Any table added in the future that depends on a
# user's study history MUST be added here.
USER_STATE_TABLES: Tuple[str, ...] = (
    # Sessions + answers
    "session",
    "sectionresult",
    "response",
    "scoringresult",
    # AWA user submissions (prompts are content — preserved)
    "awasubmission",
    "awaresult",
    # Telemetry + per-item stats derived from responses
    "telemetryevent",
    "itemstats",
    # Flashcard SRS + mastery
    "flashcardreview",
    "masteryrecord",
    # Personalization / goals
    "studyplan",
    "diagnosticresult",
    "userstats",
    # User-submitted bug reports
    "questionflag",
)

# Tables that MUST survive sanitization. Listed explicitly so an
# accidental delete on a content table shows up as a stop-condition.
CONTENT_TABLES: Tuple[str, ...] = (
    "stimulus",
    "question",
    "questionoption",
    "numericanswer",
    "awaprompt",
    "lesson",
    "vocabword",
    "vocabroot",
    # Infra, not user data — the migration ledger must be preserved so a
    # fresh clone doesn't re-apply every schema migration on top of an
    # already-migrated DB.
    "schemamigration",
    # Content-pipeline audit (dev-side, not user-side). Not a study
    # artifact, and current seed carries 0 rows anyway.
    "syntheticgenerationrun",
)


class SanitizeError(RuntimeError):
    """Raised when the script would do something unsafe."""


def _table_row_counts(conn: sqlite3.Connection, tables: Tuple[str, ...]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for table in tables:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        counts[table] = int(row[0]) if row else 0
    return counts


def _assert_expected_tables(conn: sqlite3.Connection) -> None:
    """Abort if a table we planned to delete doesn't exist, or if an
    unknown user-state-ish table appeared that we don't recognise."""
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing = [t for t in USER_STATE_TABLES if t not in existing]
    if missing:
        raise SanitizeError(
            f"User-state tables missing from DB: {missing}. "
            "Schema has drifted; update USER_STATE_TABLES."
        )
    missing_content = [t for t in CONTENT_TABLES if t not in existing]
    if missing_content:
        raise SanitizeError(
            f"Content tables missing from DB: {missing_content}. "
            "Schema has drifted; update CONTENT_TABLES."
        )
    # Surface unknown tables so a new user-state table doesn't silently
    # slip past the sanitizer.
    known = set(USER_STATE_TABLES) | set(CONTENT_TABLES)
    unknown = sorted(existing - known)
    if unknown:
        print(
            "  [warn] unknown tables present (not classified content vs "
            f"user-state): {unknown}",
            file=sys.stderr,
        )


def _refuse_user_db(db_path: Path) -> None:
    name = db_path.name
    if "gre_user" in name:
        raise SanitizeError(
            f"Refusing to sanitize {db_path!s} — looks like a runtime user DB. "
            "This script only operates on the shipped seed (gre_mock.db)."
        )


def sanitize(db_path: Path, apply: bool) -> Dict[str, Tuple[int, int]]:
    """Delete rows from user-state tables.

    Args:
        db_path: path to the seed SQLite file.
        apply: if False, only report what would be deleted.

    Returns:
        Mapping of table -> (before_count, after_count).
    """
    _refuse_user_db(db_path)
    if not db_path.exists():
        raise SanitizeError(f"DB not found: {db_path}")

    # Open the DB. Dry-run callers still connect read/write (no writes
    # are issued), because URI mode=ro fails on WAL-mode DBs without the
    # sidecar files — which is the normal state for a ``.bak`` snapshot.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        _assert_expected_tables(conn)

        before_user = _table_row_counts(conn, USER_STATE_TABLES)
        before_content = _table_row_counts(conn, CONTENT_TABLES)

        if apply:
            with conn:
                for table in USER_STATE_TABLES:
                    conn.execute(f"DELETE FROM {table}")
            # VACUUM must run outside a transaction.
            conn.execute("VACUUM")

        after_user = _table_row_counts(conn, USER_STATE_TABLES)
        after_content = _table_row_counts(conn, CONTENT_TABLES)
    finally:
        conn.close()

    # Stop condition — content rows must not have changed.
    content_diff = {
        t: (before_content[t], after_content[t])
        for t in CONTENT_TABLES
        if before_content[t] != after_content[t]
    }
    if content_diff:
        raise SanitizeError(
            f"Content tables changed during sanitize (should never happen): "
            f"{content_diff}"
        )

    combined: Dict[str, Tuple[int, int]] = {}
    for t in USER_STATE_TABLES:
        combined[t] = (before_user[t], after_user[t])
    for t in CONTENT_TABLES:
        combined[t] = (before_content[t], after_content[t])
    return combined


def _format_report(counts: Dict[str, Tuple[int, int]], apply: bool) -> str:
    lines: List[str] = []
    verb = "DELETED" if apply else "WOULD DELETE"
    lines.append("User-state tables (wiped on apply):")
    for t in USER_STATE_TABLES:
        before, after = counts[t]
        if apply:
            marker = f"  [{verb} {before}]" if before > 0 else "  [already empty]"
        else:
            marker = f"  [{verb} {before}]" if before > 0 else "  [already empty]"
        lines.append(f"  {t:<24} before={before:>7}  after={after:>7}  {marker}")
    lines.append("")
    lines.append("Content tables (must be unchanged):")
    for t in CONTENT_TABLES:
        before, after = counts[t]
        flag = "OK" if before == after else "CHANGED (error)"
        lines.append(f"  {t:<24} before={before:>7}  after={after:>7}  [{flag}]")
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--db", required=True, type=Path,
        help="Path to the seed DB (typically data/gre_mock.db).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run", action="store_true",
        help="Report deletion plan without modifying the DB.",
    )
    group.add_argument(
        "--apply", action="store_true",
        help="Delete rows from user-state tables and VACUUM.",
    )
    args = parser.parse_args(argv)

    try:
        counts = sanitize(args.db, apply=args.apply)
    except SanitizeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(_format_report(counts, apply=args.apply))
    if not args.apply:
        print()
        print("Dry run only. Re-run with --apply to modify the DB.")
    else:
        print()
        print(f"Sanitized {args.db}. VACUUM complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
