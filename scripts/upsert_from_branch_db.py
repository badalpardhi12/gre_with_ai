"""Row-level upsert from a branch DB into the main DB.

When two feature branches both mutate the LFS-tracked ``data/gre_mock.db``,
a naive merge picks one binary blob and loses the other branch's work.
This script opens the branch DB read-only and applies its row-level
diffs (figure_refs, review_notes, subtopic, status demotions,
difficulty_target edits, etc.) on top of the main DB.

Invariants:
  * The script never INSERTs or DELETEs — both DBs are expected to share
    the same primary keys; new rows must arrive through their normal
    persist/consolidate path.
  * Every UPDATE is keyed by ``(source, source_anchor)`` when the source
    row has a non-empty anchor; otherwise by question.id (best-effort —
    logged as ambiguous).
  * Idempotent: re-running writes the same rows with the same values.

Columns applied (when they exist on both sides):
  - figure_refs          (migration 015 — Princeton vision figures)
  - review_notes         (expert-review verdict JSON)
  - subtopic             (Haiku subtopic backfill)
  - status               (draft/live demotion/promotion decisions)
  - difficulty_target    (difficulty-backfill overrides)

Usage:
    venv/bin/python scripts/upsert_from_branch_db.py \
        --source-db /path/to/branch_gre_mock.db \
        --target-db data/gre_mock.db \
        [--dry-run]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


COLUMNS_TO_APPLY = (
    "figure_refs",
    "review_notes",
    "subtopic",
    # NOTE: ``difficulty_target`` is intentionally omitted. The main
    # branch's difficulty-backfill (commit c5139b3) writes a per-
    # (source, subtype) quintile distribution; older branch DBs still
    # carry the uniform-3 values and would silently regress the fix if
    # included in the upsert column set. Callers that need to sync
    # difficulty_target must pass it explicitly via --extra-column.
    # NOTE: ``status`` is also omitted by default because multiple branches
    # demote/promote items from different baselines, and ingesting one
    # branch's status diff can revert another's decisions. Pass
    # ``--include-status`` only when the source branch is the sole
    # authority for the subset of rows it changes (e.g. the vision
    # review's 6 figure-item demotions, safely filtered by source +
    # review_notes presence).
)

OPTIONAL_STATUS_COLUMN = "status"


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def upsert(
    source_db: Path,
    target_db: Path,
    dry_run: bool = False,
    include_status: bool = False,
    status_only_with_review_notes: bool = False,
) -> Dict[str, int]:
    """Apply row-level diffs from source_db to target_db.

    Returns counters: updated, skipped (not found), unchanged.
    """
    counters = {"updated": 0, "skipped_missing": 0, "unchanged": 0,
                "per_column": defaultdict(int)}

    src = sqlite3.connect(str(source_db))
    src.row_factory = sqlite3.Row
    tgt = sqlite3.connect(str(target_db))
    tgt.row_factory = sqlite3.Row

    try:
        src_cols = _table_columns(src, "question")
        tgt_cols = _table_columns(tgt, "question")
        apply_cols = [
            c for c in COLUMNS_TO_APPLY if c in src_cols and c in tgt_cols
        ]
        if include_status:
            if OPTIONAL_STATUS_COLUMN in src_cols and OPTIONAL_STATUS_COLUMN in tgt_cols:
                apply_cols.append(OPTIONAL_STATUS_COLUMN)
        missing = [c for c in COLUMNS_TO_APPLY
                   if not (c in src_cols and c in tgt_cols)]
        print(f"columns to sync: {apply_cols}")
        if missing:
            print(f"columns skipped (not on both sides): {missing}")
        if status_only_with_review_notes:
            print("status changes gated on non-empty source review_notes")

        # Build target index: (source, source_anchor) -> qid; also fallback
        # by qid alone when anchor missing.
        tgt_by_anchor: Dict[Tuple[str, str], int] = {}
        tgt_by_id: Dict[int, sqlite3.Row] = {}
        for row in tgt.execute("SELECT * FROM question"):
            tgt_by_id[row["id"]] = row
            anchor = row["source_anchor"] if "source_anchor" in row.keys() else ""
            if anchor:
                tgt_by_anchor[(row["source"], anchor)] = row["id"]

        # Walk source rows
        for srow in src.execute("SELECT * FROM question"):
            src_id = srow["id"]
            src_anchor = srow["source_anchor"] if "source_anchor" in srow.keys() else ""
            tgt_qid = None
            if src_anchor:
                tgt_qid = tgt_by_anchor.get((srow["source"], src_anchor))
            if tgt_qid is None:
                # Fall back to id match
                if src_id in tgt_by_id:
                    tgt_qid = src_id

            if tgt_qid is None:
                counters["skipped_missing"] += 1
                continue

            trow = tgt_by_id[tgt_qid]
            set_parts = []
            set_vals = []
            src_review = srow["review_notes"] if "review_notes" in srow.keys() else ""
            for col in apply_cols:
                sv = srow[col]
                tv = trow[col] if col in trow.keys() else None
                if sv is None:
                    continue  # don't overwrite with null
                # Only overwrite if the source has a non-default value that
                # differs from target. For text fields, the defaults are
                # ''/[], so treat those as "no change".
                if col in ("figure_refs",) and sv in ("", "[]"):
                    continue
                if col == "review_notes" and not sv:
                    continue
                if col == "subtopic" and not sv:
                    continue
                if col == "status" and status_only_with_review_notes:
                    # Gate status changes on the source having a real
                    # review verdict — otherwise we'd import stale status
                    # values from a branch that never touched this row.
                    if not src_review:
                        continue
                if sv == tv:
                    continue
                set_parts.append(f"{col}=?")
                set_vals.append(sv)
                counters["per_column"][col] += 1

            if not set_parts:
                counters["unchanged"] += 1
                continue

            set_vals.append(tgt_qid)
            sql = (
                "UPDATE question SET " + ",".join(set_parts) +
                " WHERE id=?"
            )
            if not dry_run:
                tgt.execute(sql, set_vals)
            counters["updated"] += 1

        if not dry_run:
            tgt.commit()
    finally:
        src.close()
        tgt.close()

    return counters


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-db", required=True, type=Path)
    ap.add_argument("--target-db", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--include-status", action="store_true",
        help="Also sync the status column. Off by default because "
        "cross-branch status diffs can revert another branch's decisions.",
    )
    ap.add_argument(
        "--status-only-with-review-notes", action="store_true",
        help="Gate status syncs on the source row having a non-empty "
        "review_notes value. Useful for vision-review merges where only "
        "reviewed items should have their status updated.",
    )
    args = ap.parse_args()

    if not args.source_db.exists():
        print(f"error: source DB not found: {args.source_db}", file=sys.stderr)
        sys.exit(1)
    if not args.target_db.exists():
        print(f"error: target DB not found: {args.target_db}", file=sys.stderr)
        sys.exit(1)

    print(f"source : {args.source_db}")
    print(f"target : {args.target_db}")
    print(f"dry-run: {args.dry_run}")
    counters = upsert(
        args.source_db,
        args.target_db,
        dry_run=args.dry_run,
        include_status=args.include_status,
        status_only_with_review_notes=args.status_only_with_review_notes,
    )
    print("\n=== Summary ===")
    print(f"updated         : {counters['updated']}")
    print(f"unchanged       : {counters['unchanged']}")
    print(f"skipped_missing : {counters['skipped_missing']}")
    print("per column:")
    for col, n in sorted(counters["per_column"].items()):
        print(f"  {col:<20} {n}")


if __name__ == "__main__":
    main()
