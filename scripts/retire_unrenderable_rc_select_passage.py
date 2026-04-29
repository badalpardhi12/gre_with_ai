"""Retire rc_select_passage items that lack the runtime's required structure.

An `rc_select_passage` question is unanswerable at runtime unless:

  1. Its stimulus contains `<sent id='N'>…</sent>` markers that
     `screens.question_screen._annotate_passage_sentences` can rewrite into
     visible `[N]` sentinels.
  2. The question has one or more `QuestionOption` rows whose `option_label`
     is the target sentence index (so the UI can render one radio per
     sentence).

Historically the Princeton extractor produced neither. Six live rows made it
into the live rotation with 0 options and 0 sentence markers — the UI shows
the passage but no answer choices, making the item impossible to answer.

This script retires such rows (status='retired') with a `review_notes` entry
so the data gap is visible in later audits. It is idempotent — already-retired
rows are skipped.

Usage:
    venv/bin/python scripts/retire_unrenderable_rc_select_passage.py \\
        --db data/gre_user.db
    venv/bin/python scripts/retire_unrenderable_rc_select_passage.py \\
        --db data/gre_mock.db
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


RETIRE_NOTE = (
    "[render-forensic-2026-04-28]\n"
    "  Retired: rc_select_passage with 0 QuestionOption rows AND stimulus "
    "lacks <sent id='N'> markers. Neither the radio list nor the [N] passage "
    "sentinels can render, so the item is impossible to answer at runtime. "
    "Princeton extractor did not inject sentence-level structure."
)


def _select_broken(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    cur = conn.execute(
        """
        SELECT q.id, q.stimulus_id
          FROM question AS q
          JOIN stimulus AS s ON q.stimulus_id = s.id
         WHERE q.subtype = 'rc_select_passage'
           AND q.status  = 'live'
           AND NOT EXISTS (
               SELECT 1 FROM questionoption o
                WHERE o.question_id = q.id
           )
           AND s.content NOT LIKE '%<sent %'
        """
    )
    return cur.fetchall()


def run(db_path: Path, dry_run: bool) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = _select_broken(conn)
        if not rows:
            print(f"{db_path.name}: no broken live rc_select_passage rows")
            return 0
        for qid, sid in rows:
            print(f"{db_path.name}: retire qid={qid} stim={sid}")
            if not dry_run:
                # Preserve any pre-existing notes.
                existing = conn.execute(
                    "SELECT review_notes FROM question WHERE id = ?",
                    (qid,),
                ).fetchone()[0] or ""
                new_notes = (existing + "\n" + RETIRE_NOTE).strip()
                conn.execute(
                    "UPDATE question SET status='retired', review_notes=? "
                    "WHERE id=?",
                    (new_notes, qid),
                )
        if not dry_run:
            conn.commit()
        return len(rows)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    n = run(args.db.resolve(), args.dry_run)
    print(f"{args.db}: {n} rows {'would be ' if args.dry_run else ''}retired")


if __name__ == "__main__":
    main()
