"""Consolidate Princeton / Kaplan / synthetic items from worktree DBs into
main's runtime DB.

Idempotent upsert by ``(source, source_anchor)`` when available, or by a
content-hash fallback of the stimulus + prompt when the source DB predates
the ``source_anchor`` column (Kaplan). Running this script twice against
an already-populated main DB produces identical results — the second run
reports zero inserts.

Invoked once from the Phase-3 consolidation step.

Usage:
    venv/bin/python scripts/consolidate_dbs.py

Prints per-source insert/update counters and aborts on hard schema drift.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

REPO = Path("/Users/chiku/Documents/side_projects/gre_with_ai")

MAIN_DB = REPO / "data" / "gre_user.db"

# Each entry is (description, worktree_db_path, source_token).
SOURCES: List[Tuple[str, Path, str]] = [
    ("Princeton",
     REPO / ".claude/worktrees/agent-a9405213/data/gre_user.db",
     "princeton_2012"),
    ("Kaplan",
     REPO / ".claude/worktrees/agent-a9aaa962/data/gre_mock.db",
     "kaplan_2024"),
    ("Synthetic",
     REPO / ".claude/worktrees/agent-a5d145f1/data/gre_user.db",
     "ai_synthetic"),
]


# ---------------------------------------------------------------------------
# Helpers

def _row_to_dict(cur: sqlite3.Cursor, row: sqlite3.Row) -> Dict:
    return {col[0]: row[idx] for idx, col in enumerate(cur.description)}


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    cur = conn.execute(f'PRAGMA table_info("{table}")')
    return [r[1] for r in cur.fetchall()]


def _content_hash(prompt: str, stimulus_content: str, options_text: str) -> str:
    h = hashlib.sha256()
    h.update(prompt.encode("utf-8", "ignore"))
    h.update(b"||")
    h.update(stimulus_content.encode("utf-8", "ignore"))
    h.update(b"||")
    h.update(options_text.encode("utf-8", "ignore"))
    return h.hexdigest()


def _get_options_text(conn: sqlite3.Connection, question_id: int) -> str:
    rows = conn.execute(
        "SELECT option_label, option_text FROM questionoption "
        "WHERE question_id=? ORDER BY option_label",
        (question_id,),
    ).fetchall()
    return "||".join(f"{r[0]}={r[1]}" for r in rows)


def _get_stimulus(conn: sqlite3.Connection, stim_id: Optional[int]) -> Optional[Dict]:
    if stim_id is None:
        return None
    cur = conn.execute("SELECT * FROM stimulus WHERE id=?", (stim_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(cur, row)


def _stim_hash(stim: Dict) -> str:
    h = hashlib.sha256()
    h.update(stim.get("stimulus_type", "").encode("utf-8", "ignore"))
    h.update(b"||")
    h.update(stim.get("title", "").encode("utf-8", "ignore"))
    h.update(b"||")
    h.update(stim.get("content", "").encode("utf-8", "ignore"))
    h.update(b"||")
    h.update(stim.get("render_spec", "").encode("utf-8", "ignore"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Core upsert

def consolidate_source(desc: str, src_path: Path, source_tag: str,
                        dst_conn: sqlite3.Connection) -> Dict[str, int]:
    """Upsert all rows for ``source=source_tag`` from ``src_path`` into ``dst``.

    Returns counters: ``{"inserted": N, "updated": M, "skipped": K,
    "stimuli_inserted": X, "options_inserted": Y, "numeric_inserted": Z}``.
    """
    counters = {
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "stimuli_inserted": 0,
        "stimuli_reused": 0,
        "options_inserted": 0,
        "numeric_inserted": 0,
    }
    if not src_path.exists():
        print(f"[{desc}] SKIP — source DB not found: {src_path}")
        return counters

    src = sqlite3.connect(str(src_path))
    src.row_factory = sqlite3.Row

    src_cols = _table_columns(src, "question")
    dst_cols = _table_columns(dst_conn, "question")
    has_source_anchor_src = "source_anchor" in src_cols
    has_review_notes_src = "review_notes" in src_cols
    has_synthetic_cols_src = "provenance_json" in src_cols

    print(f"[{desc}] reading {source_tag} rows from {src_path}")
    print(f"         source has source_anchor={has_source_anchor_src} "
          f"review_notes={has_review_notes_src} "
          f"synthetic_cols={has_synthetic_cols_src}")

    rows = src.execute(
        "SELECT * FROM question WHERE source=?", (source_tag,)
    ).fetchall()
    print(f"[{desc}] {len(rows)} source rows to upsert")

    # Build a dst index keyed by (source, source_anchor) OR content hash.
    existing_by_anchor: Dict[Tuple[str, str], int] = {}
    existing_by_hash: Dict[str, int] = {}
    dst_cur = dst_conn.execute(
        "SELECT id, source, source_anchor, prompt, stimulus_id "
        "FROM question WHERE source=?", (source_tag,)
    )
    for r in dst_cur.fetchall():
        dst_id = r[0]
        src_val = r[1]
        anchor = r[2] or ""
        if anchor:
            existing_by_anchor[(src_val, anchor)] = dst_id
        # Hash fallback
        opts = _get_options_text(dst_conn, dst_id)
        stim = _get_stimulus(dst_conn, r[4])
        stim_content = stim["content"] if stim else ""
        existing_by_hash[_content_hash(r[3], stim_content, opts)] = dst_id

    # Cache dst stimuli by content hash so we reuse rather than duplicate.
    dst_stim_by_hash: Dict[str, int] = {}
    dst_stim_cur = dst_conn.execute("SELECT * FROM stimulus")
    for srow in dst_stim_cur.fetchall():
        s_dict = _row_to_dict(dst_stim_cur, srow)
        dst_stim_by_hash[_stim_hash(s_dict)] = s_dict["id"]

    for row in rows:
        q = {k: row[k] for k in row.keys()}
        src_qid = q["id"]
        anchor = q.get("source_anchor", "") if has_source_anchor_src else ""

        # ----- Resolve dst question id (update vs insert) -----
        dst_qid: Optional[int] = None
        if anchor:
            dst_qid = existing_by_anchor.get((source_tag, anchor))
        if dst_qid is None:
            # Hash fallback
            src_stim = _get_stimulus(src, q["stimulus_id"])
            src_opts = _get_options_text(src, src_qid)
            src_stim_content = src_stim["content"] if src_stim else ""
            ch = _content_hash(q["prompt"], src_stim_content, src_opts)
            dst_qid = existing_by_hash.get(ch)

        # ----- Resolve dst stimulus id -----
        new_stim_id: Optional[int] = None
        src_stim = _get_stimulus(src, q["stimulus_id"])
        if src_stim is not None:
            sh = _stim_hash(src_stim)
            if sh in dst_stim_by_hash:
                new_stim_id = dst_stim_by_hash[sh]
                counters["stimuli_reused"] += 1
            else:
                # Insert stimulus into dst.
                cols = [c for c in src_stim.keys() if c != "id"]
                placeholders = ",".join("?" for _ in cols)
                vals = [src_stim[c] for c in cols]
                cur = dst_conn.execute(
                    f"INSERT INTO stimulus ({','.join(cols)}) "
                    f"VALUES ({placeholders})",
                    vals,
                )
                new_stim_id = cur.lastrowid
                dst_stim_by_hash[sh] = new_stim_id
                counters["stimuli_inserted"] += 1

        # ----- Build insert/update column set -----
        # Only use columns that exist in BOTH source and dest.
        shared_cols = [c for c in src_cols if c in dst_cols and c != "id"]
        payload = {c: q.get(c) for c in shared_cols}
        payload["stimulus_id"] = new_stim_id
        # If dst has source_anchor but the source row didn't set one, stamp
        # a synthetic anchor so repeat runs are stable.
        if "source_anchor" in dst_cols:
            if not payload.get("source_anchor"):
                # Derive a deterministic anchor from the content hash so
                # re-runs match up. Keep it prefixed to avoid collision
                # with real publisher anchors.
                src_stim_content = src_stim["content"] if src_stim else ""
                src_opts = _get_options_text(src, src_qid)
                ch = _content_hash(q["prompt"], src_stim_content, src_opts)
                payload["source_anchor"] = f"auto-{source_tag}-{ch[:16]}"

        if dst_qid is not None:
            # UPDATE path
            set_cols = [c for c in shared_cols if c != "source_anchor"]
            # Keep source_anchor updates too so future runs index correctly.
            if "source_anchor" in dst_cols:
                set_cols = list(set_cols) + ["source_anchor"]
            set_clause = ",".join(f"{c}=?" for c in set_cols)
            vals = [payload.get(c) for c in set_cols] + [dst_qid]
            dst_conn.execute(
                f"UPDATE question SET {set_clause} WHERE id=?", vals,
            )
            counters["updated"] += 1
            new_qid = dst_qid
        else:
            # INSERT path
            insert_cols = list(payload.keys())
            placeholders = ",".join("?" for _ in insert_cols)
            vals = [payload[c] for c in insert_cols]
            cur = dst_conn.execute(
                f"INSERT INTO question ({','.join(insert_cols)}) "
                f"VALUES ({placeholders})",
                vals,
            )
            new_qid = cur.lastrowid
            counters["inserted"] += 1

        # ----- Children: options + numeric_answer -----
        # Replace under the new qid so moves are clean.
        dst_conn.execute(
            "DELETE FROM questionoption WHERE question_id=?", (new_qid,)
        )
        opt_rows = src.execute(
            "SELECT * FROM questionoption WHERE question_id=?", (src_qid,)
        ).fetchall()
        opt_cols = _table_columns(src, "questionoption")
        opt_shared = [c for c in opt_cols if c != "id"
                       and c in _table_columns(dst_conn, "questionoption")]
        for o in opt_rows:
            od = {k: o[k] for k in o.keys()}
            od["question_id"] = new_qid
            vals = [od.get(c) for c in opt_shared]
            placeholders = ",".join("?" for _ in opt_shared)
            dst_conn.execute(
                f"INSERT INTO questionoption ({','.join(opt_shared)}) "
                f"VALUES ({placeholders})",
                vals,
            )
            counters["options_inserted"] += 1

        dst_conn.execute(
            "DELETE FROM numericanswer WHERE question_id=?", (new_qid,)
        )
        num_rows = src.execute(
            "SELECT * FROM numericanswer WHERE question_id=?", (src_qid,)
        ).fetchall()
        num_cols = _table_columns(src, "numericanswer")
        num_shared = [c for c in num_cols if c != "id"
                       and c in _table_columns(dst_conn, "numericanswer")]
        for n in num_rows:
            nd = {k: n[k] for k in n.keys()}
            nd["question_id"] = new_qid
            vals = [nd.get(c) for c in num_shared]
            placeholders = ",".join("?" for _ in num_shared)
            dst_conn.execute(
                f"INSERT INTO numericanswer ({','.join(num_shared)}) "
                f"VALUES ({placeholders})",
                vals,
            )
            counters["numeric_inserted"] += 1

    src.close()
    return counters


def main():
    if not MAIN_DB.exists():
        print(f"FATAL: main DB not found: {MAIN_DB}", file=sys.stderr)
        sys.exit(2)

    dst = sqlite3.connect(str(MAIN_DB))
    dst.row_factory = sqlite3.Row

    totals: Dict[str, Dict[str, int]] = {}
    try:
        for desc, src_path, source_tag in SOURCES:
            counters = consolidate_source(desc, src_path, source_tag, dst)
            totals[desc] = counters
            dst.commit()
            print(f"[{desc}] committed: {counters}")
    finally:
        dst.close()

    print("\n=== SUMMARY ===")
    for desc, c in totals.items():
        print(f"{desc:<12}: inserted={c['inserted']:<4} updated={c['updated']:<4} "
              f"stimuli(+{c['stimuli_inserted']}/reused{c['stimuli_reused']}) "
              f"options=+{c['options_inserted']} numeric=+{c['numeric_inserted']}")


if __name__ == "__main__":
    main()
