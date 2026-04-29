"""
Repair DI chart-integrity defects in data/gre_user.db.

Categories handled:

  A. Empty-content chart stimulus, retired-twin exists with populated
     stimulus  →  relink `question.stimulus_id` to the populated row.

  B. Empty-content chart stimulus, no populated twin  →  retire question.

  C. `stimulus_id IS NULL`, prompt contains an unambiguous figure
     reference  →  retire question (no chart ever stored).

Writes a ledger under `data/audits/di_chart_integrity_2026_04_28_ledger.csv`
for traceability.

Idempotent: running twice is a no-op because criteria all key off live
status + empty stimulus; relinked questions no longer match.
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/gre_user.db")
LEDGER_PATH = Path("data/audits/di_chart_integrity_2026_04_28_ledger.csv")

FIGURE_REF_PATTERNS = (
    "figure above", "figure below", "shown above", "shown below",
    "graph above", "graph below", "graph shown", "figure shown",
    "chart above", "chart below", "in the figure",
)


def _sql_like_any(col: str, patterns: tuple[str, ...]) -> str:
    return "(" + " OR ".join(f"LOWER({col}) LIKE '%{p}%'" for p in patterns) + ")"


def collect_remap(conn: sqlite3.Connection) -> dict[int, int]:
    """Return qid -> target populated stimulus id for safe relinks."""
    c = conn.cursor()

    # A1: direct prompt-match twins (same prompt, retired, populated stim).
    c.execute(
        """
        SELECT q_live.id, q_ret.stimulus_id, length(s_ret.content)
        FROM question q_live
        JOIN question q_ret ON q_live.prompt = q_ret.prompt
                           AND q_live.id != q_ret.id
        JOIN stimulus s_live ON s_live.id = q_live.stimulus_id
        JOIN stimulus s_ret ON s_ret.id = q_ret.stimulus_id
        WHERE q_live.status = 'live'
          AND q_ret.status = 'retired'
          AND length(s_live.content) < 20 AND length(s_live.render_spec) < 20
          AND length(s_ret.content) > 100
          AND s_live.stimulus_type IN ('chart','graph','table','data_interp')
        ORDER BY q_live.id, length(s_ret.content) DESC
        """
    )
    direct: dict[int, int] = {}
    for qid, ps, _ in c.fetchall():
        direct.setdefault(qid, ps)

    # A2: per-empty-stim populated options derivable from retired twins.
    c.execute(
        """
        SELECT DISTINCT s_live.id, s_ret.id
        FROM question q_live
        JOIN question q_ret ON q_live.prompt = q_ret.prompt
                           AND q_live.id != q_ret.id
        JOIN stimulus s_live ON s_live.id = q_live.stimulus_id
        JOIN stimulus s_ret ON s_ret.id = q_ret.stimulus_id
        WHERE q_live.status = 'live'
          AND q_ret.status = 'retired'
          AND length(s_live.content) < 20 AND length(s_live.render_spec) < 20
          AND length(s_ret.content) > 100
          AND s_live.stimulus_type IN ('chart','graph','table','data_interp')
        """
    )
    options: dict[int, set[int]] = {}
    for es, ps in c.fetchall():
        options.setdefault(es, set()).add(ps)

    # All live questions on empty chart stims
    c.execute(
        """
        SELECT q.id, q.stimulus_id FROM question q
        JOIN stimulus s ON s.id = q.stimulus_id
        WHERE q.status = 'live'
          AND s.stimulus_type IN ('chart','graph','table','data_interp')
          AND length(s.content) < 20 AND length(s.render_spec) < 20
        """
    )
    remap: dict[int, int] = {}
    for qid, es in c.fetchall():
        if qid in direct:
            remap[qid] = direct[qid]
        elif len(options.get(es, set())) == 1:
            remap[qid] = next(iter(options[es]))
    return remap


def collect_empty_stim_orphans(
    conn: sqlite3.Connection, remapped: set[int]
) -> list[int]:
    c = conn.cursor()
    c.execute(
        """
        SELECT q.id FROM question q
        JOIN stimulus s ON s.id = q.stimulus_id
        WHERE q.status = 'live'
          AND s.stimulus_type IN ('chart','graph','table','data_interp')
          AND length(s.content) < 20 AND length(s.render_spec) < 20
        """
    )
    return [qid for (qid,) in c.fetchall() if qid not in remapped]


def collect_null_stim_figure_refs(conn: sqlite3.Connection) -> list[int]:
    c = conn.cursor()
    where = _sql_like_any("q.prompt", FIGURE_REF_PATTERNS)
    c.execute(
        f"""
        SELECT q.id FROM question q
        WHERE q.status = 'live' AND q.stimulus_id IS NULL AND {where}
          AND q.subtype IN ('mcq_single','mcq_multi','numeric_entry',
                            'data_interp','qc')
        """
    )
    return [qid for (qid,) in c.fetchall()]


def apply_fixes(conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    remap = collect_remap(conn)
    empty_orphans = collect_empty_stim_orphans(conn, set(remap))
    null_orphans = collect_null_stim_figure_refs(conn)
    now = datetime.utcnow().isoformat(timespec="seconds")

    ledger_rows = []
    c = conn.cursor()
    if not dry_run:
        for qid, new_stim in remap.items():
            c.execute(
                "SELECT stimulus_id, review_notes FROM question WHERE id = ?",
                (qid,),
            )
            old_stim, notes = c.fetchone()
            tag = (
                f"[di-chart-integrity {now}] stim {old_stim} -> {new_stim} "
                "(empty shell replaced via retired-twin lookup)"
            )
            new_notes = (notes + "\n" + tag).strip() if notes else tag
            c.execute(
                "UPDATE question SET stimulus_id = ?, review_notes = ?, "
                "updated_at = ? WHERE id = ?",
                (new_stim, new_notes, now, qid),
            )
            ledger_rows.append(
                {"action": "relink", "qid": qid, "old_stim": old_stim,
                 "new_stim": new_stim, "reason": "retired-twin found"}
            )

        for qid in empty_orphans:
            c.execute(
                "SELECT stimulus_id, review_notes FROM question WHERE id = ?",
                (qid,),
            )
            old_stim, notes = c.fetchone()
            tag = (
                f"[di-chart-integrity {now}] retired — empty chart stimulus "
                f"{old_stim} with no recoverable twin"
            )
            new_notes = (notes + "\n" + tag).strip() if notes else tag
            c.execute(
                "UPDATE question SET status = 'retired', review_notes = ?, "
                "updated_at = ? WHERE id = ?",
                (new_notes, now, qid),
            )
            ledger_rows.append(
                {"action": "retire_empty_stim", "qid": qid,
                 "old_stim": old_stim, "new_stim": "",
                 "reason": "empty chart stimulus, no recoverable chart"}
            )

        for qid in null_orphans:
            c.execute("SELECT review_notes FROM question WHERE id = ?", (qid,))
            (notes,) = c.fetchone()
            tag = (
                f"[di-chart-integrity {now}] retired — prompt cites a "
                "figure but stimulus_id is NULL (chart never stored)"
            )
            new_notes = (notes + "\n" + tag).strip() if notes else tag
            c.execute(
                "UPDATE question SET status = 'retired', review_notes = ?, "
                "updated_at = ? WHERE id = ?",
                (new_notes, now, qid),
            )
            ledger_rows.append(
                {"action": "retire_null_stim", "qid": qid,
                 "old_stim": "", "new_stim": "",
                 "reason": "figure-referencing prompt with no stimulus"}
            )
        conn.commit()

    if ledger_rows:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER_PATH.open("w", newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["action", "qid", "old_stim", "new_stim", "reason"]
            )
            writer.writeheader()
            writer.writerows(ledger_rows)

    return {
        "relinked": len(remap),
        "retired_empty_stim": len(empty_orphans),
        "retired_null_stim": len(null_orphans),
        "total_changed": len(remap) + len(empty_orphans) + len(null_orphans),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report counts without modifying the DB.",
    )
    parser.add_argument(
        "--db", default=str(DB_PATH),
        help="SQLite path (default: data/gre_user.db).",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        result = apply_fixes(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    mode = "DRY RUN" if args.dry_run else "APPLIED"
    print(f"[{mode}] di-chart-integrity repair")
    for k, v in result.items():
        print(f"  {k}: {v}")
    if not args.dry_run:
        print(f"  ledger: {LEDGER_PATH}")


if __name__ == "__main__":
    main()
