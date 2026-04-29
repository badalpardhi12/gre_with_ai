"""Backfill `question.difficulty_target` for sources that were persisted
with a hardcoded default of 3.

Background
----------
`scripts/persist_princeton.py` and the legacy manhattan/ai_generated
persist paths wrote every row with ``difficulty_target=3`` because the
source material doesn't ship per-item difficulty labels. That uniformity
breaks ``services.question_bank``: an "easy" section filters on
``difficulty_target <= 2`` (finds nothing) and "hard" filters on
``difficulty_target >= 4`` (finds nothing) — so adaptive routing
collapses into a single pool.

Strategy (deterministic, no LLM)
--------------------------------
Per ``(source, subtype)`` bucket, split rows into five quintiles by
prompt length + stimulus length, then map quintiles to difficulty
1..5. Long prompts / heavy stimuli lean harder. Ties are broken by
``question.id`` so the mapping is stable across runs.

Targets a realistic GRE bell shape: quintile cut points map to
difficulties [2, 3, 3, 4, 4] for most subtypes (skewed toward the
middle) — see ``_DIFFICULTY_CURVE``. QC and numeric_entry get a slightly
harder curve; rc_single (short) gets a slightly easier curve.

Scope
-----
Only rewrites rows where ``difficulty_target == 3`` AND the source is
in the affected set (Princeton, Manhattan, legacy ai_generated). Rows
that already have a non-3 value (ai_synthetic, kaplan_2024, or anything
a later pass rated) are left alone.

Idempotent: re-running produces the same assignments.

Usage
-----
    venv/bin/python scripts/backfill_difficulty_target.py [--db PATH] [--dry-run]

Defaults to updating both ``data/gre_user.db`` and ``data/gre_mock.db``.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Sources whose rows still sit at the hardcoded default 3.
AFFECTED_SOURCES = ("princeton_2012", "manhattan_5lb_2018", "ai_generated")

# Default difficulty curve for five-quintile mapping.
# Index 0 -> easiest quintile, index 4 -> hardest.
_DIFFICULTY_CURVE_DEFAULT = (2, 3, 3, 3, 4)
_DIFFICULTY_CURVE_BY_SUBTYPE = {
    # QC tends to hide traps even in short stems -> push tails harder.
    "qc": (2, 3, 3, 4, 4),
    # Numeric entry often has multi-step quant work -> push harder.
    "numeric_entry": (2, 3, 3, 4, 5),
    # Data interp is dense; even the "short" ones lean medium-hard.
    "data_interp": (3, 3, 4, 4, 5),
    # RC singles ride the passage's difficulty; keep spread tighter.
    "rc_single": (1, 2, 3, 3, 4),
    "rc_multi": (2, 3, 3, 4, 4),
    "rc_select_passage": (2, 3, 3, 4, 4),
    # TC and SE: vocabulary-heavy; long, ornate stems tend to trap.
    "tc": (1, 2, 3, 4, 5),
    "se": (2, 3, 3, 4, 5),
    "mcq_single": (2, 3, 3, 4, 4),
    "mcq_multi": (2, 3, 3, 4, 5),
}


def _stimulus_length(conn: sqlite3.Connection, stim_id) -> int:
    if stim_id is None:
        return 0
    row = conn.execute(
        "SELECT COALESCE(LENGTH(content), 0) FROM stimulus WHERE id=?",
        (stim_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def _complexity_score(conn: sqlite3.Connection, row) -> int:
    """Stable integer score: prompt length + stimulus length.

    We only need ordering within a ``(source, subtype)`` bucket, so
    plain character counts work. Stimulus adds realism for RC/DI items
    whose prompts alone are uniformly short.
    """
    stim_len = _stimulus_length(conn, row["stimulus_id"])
    return int(row["prompt_len"]) + stim_len


def _assign_quintile(i: int, n: int) -> int:
    """Map rank i/n to a quintile 0..4 (5 near-equal buckets)."""
    if n <= 1:
        return 2
    # Guard: if rank == n (shouldn't happen since we use 0-based i), clamp.
    q = (5 * i) // n
    if q > 4:
        q = 4
    return q


def compute_plan(
    conn: sqlite3.Connection,
    sources: Tuple[str, ...] = AFFECTED_SOURCES,
) -> Dict[int, int]:
    """Return ``{question_id: new_difficulty}`` for affected rows."""
    rows = conn.execute(
        "SELECT id, source, subtype, stimulus_id, "
        "       COALESCE(LENGTH(prompt), 0) AS prompt_len, "
        "       difficulty_target "
        f"FROM question WHERE difficulty_target = 3 AND source IN "
        f"({','.join('?' * len(sources))})",
        sources,
    ).fetchall()

    # Bucket by (source, subtype). Compute complexity within each
    # bucket and rank.
    buckets: Dict[Tuple[str, str], List[Tuple[int, int, int]]] = defaultdict(list)
    for r in rows:
        complexity = _complexity_score(conn, r)
        buckets[(r["source"], r["subtype"])].append(
            (complexity, int(r["id"]), int(r["id"]))
        )

    plan: Dict[int, int] = {}
    for (source, subtype), items in buckets.items():
        # Sort: complexity asc, then id asc for deterministic ties.
        items.sort()
        n = len(items)
        curve = _DIFFICULTY_CURVE_BY_SUBTYPE.get(
            subtype, _DIFFICULTY_CURVE_DEFAULT
        )
        for i, (_, qid, _) in enumerate(items):
            q = _assign_quintile(i, n)
            plan[qid] = curve[q]
    return plan


def apply_plan(
    conn: sqlite3.Connection, plan: Dict[int, int], dry_run: bool = False
) -> Dict[int, int]:
    """Apply the plan; return pre-fix and post-fix distribution dicts."""
    pre = {d: 0 for d in range(1, 6)}
    post = {d: 0 for d in range(1, 6)}
    for qid, new_d in plan.items():
        pre[3] += 1
        post[new_d] += 1
    if dry_run:
        return pre, post
    cur = conn.cursor()
    for qid, new_d in plan.items():
        cur.execute(
            "UPDATE question SET difficulty_target=? WHERE id=?",
            (new_d, qid),
        )
    conn.commit()
    return pre, post


def _report_distribution(conn: sqlite3.Connection, sources: Tuple[str, ...]) -> None:
    rows = conn.execute(
        "SELECT source, difficulty_target, COUNT(*) FROM question "
        f"WHERE source IN ({','.join('?' * len(sources))}) "
        "GROUP BY source, difficulty_target ORDER BY source, difficulty_target",
        sources,
    ).fetchall()
    by_source: Dict[str, Dict[int, int]] = defaultdict(dict)
    for src, d, c in rows:
        by_source[src][d] = c
    for src in sorted(by_source):
        dist = by_source[src]
        total = sum(dist.values())
        line = ", ".join(
            f"{d}:{dist.get(d, 0)}" for d in sorted(dist)
        )
        print(f"  {src:<24} total={total:<5} [{line}]")


def backfill_db(db_path: Path, dry_run: bool = False) -> None:
    if not db_path.exists():
        print(f"[skip] {db_path} does not exist")
        return
    print(f"\n=== {db_path} ===")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        print("  Before:")
        _report_distribution(conn, AFFECTED_SOURCES)
        plan = compute_plan(conn)
        print(f"  Computed plan: {len(plan)} rows to re-assign")
        pre, post = apply_plan(conn, plan, dry_run=dry_run)
        print(f"  Pre  (all were 3): {dict(pre)}")
        print(f"  Post distribution: {dict(post)}")
        if dry_run:
            print("  (dry-run; no changes written)")
        else:
            print("  After:")
            _report_distribution(conn, AFFECTED_SOURCES)
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        action="append",
        help="Path to DB to update (repeatable). Defaults to gre_user.db "
        "and gre_mock.db under data/.",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.db:
        dbs = [Path(p) for p in args.db]
    else:
        dbs = [
            PROJECT_ROOT / "data" / "gre_user.db",
            PROJECT_ROOT / "data" / "gre_mock.db",
        ]
    for db in dbs:
        backfill_db(db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
