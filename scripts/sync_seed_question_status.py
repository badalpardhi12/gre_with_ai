"""Sync status + review_notes from gre_user.db -> gre_mock.db (seed).

The runtime DB (`data/gre_user.db`) accumulates status changes from audits
(mispair demotions, visual sweep demotions, retirements, subtype fixes).
The seed DB (`data/gre_mock.db`, shipped via Git LFS and used to bootstrap
fresh installs) lags because those audits ran against the runtime DB only.

A fresh install, or anyone who deletes `gre_user.db` to reset, bootstraps
from the stale seed and therefore sees retired/broken items back in the
live rotation.

This script copies the `status`, `review_notes`, and any other audit-
driven metadata for every question where the runtime and seed disagree.
Stimulus content is NOT copied — content-level fixes already landed in
both DBs (see `scripts/inline_image_srcs.py`).

Idempotent: already-matching rows are skipped.

Usage:
    venv/bin/python scripts/sync_seed_question_status.py
    venv/bin/python scripts/sync_seed_question_status.py --dry-run
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def run(user_db: Path, seed_db: Path, dry_run: bool) -> int:
    conn = sqlite3.connect(str(seed_db))
    try:
        conn.execute(f"ATTACH DATABASE '{user_db}' AS src")
        cur = conn.execute(
            """
            SELECT s.id, s.status, r.status, s.review_notes, r.review_notes
              FROM question AS s
              JOIN src.question AS r ON s.id = r.id
             WHERE s.status != r.status
                OR IFNULL(s.review_notes, '') != IFNULL(r.review_notes, '')
            """
        )
        rows = cur.fetchall()
        if not rows:
            print(f"{seed_db.name}: already in sync with {user_db.name}")
            return 0
        status_changed = 0
        notes_changed = 0
        for qid, seed_status, run_status, seed_notes, run_notes in rows:
            if seed_status != run_status:
                status_changed += 1
            if (seed_notes or "") != (run_notes or ""):
                notes_changed += 1
            if not dry_run:
                conn.execute(
                    "UPDATE question SET status=?, review_notes=? WHERE id=?",
                    (run_status, run_notes, qid),
                )
        if not dry_run:
            conn.commit()
        print(
            f"{seed_db.name}: {len(rows)} rows "
            f"({status_changed} status-changed, {notes_changed} notes-changed) "
            f"{'would be ' if dry_run else ''}synced from {user_db.name}"
        )
        return len(rows)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-db",
        type=Path,
        default=Path("data/gre_user.db"),
    )
    parser.add_argument(
        "--seed-db",
        type=Path,
        default=Path("data/gre_mock.db"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.user_db.resolve(), args.seed_db.resolve(), args.dry_run)


if __name__ == "__main__":
    main()
