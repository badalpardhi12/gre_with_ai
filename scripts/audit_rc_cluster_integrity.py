"""Forensic audit for RC cluster data integrity.

Checks for:
  1. Stimuli whose content contains cluster markers ("Questions N-M are based on...")
  2. Exact-text duplicate stimuli (the same passage present in >1 stimulus row)
  3. RC question orphans (stimulus_id IS NULL but subtype is rc_*)
  4. Specific case: the user's "Max Planck" passage

Read-only: produces a markdown audit to data/audits/.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime
from typing import List, Tuple


CLUSTER_LIKE = (
    "s.content LIKE '%Questions %based on the passage%' "
    "OR s.content LIKE '%Questions %based on the following passage%' "
    "OR s.content LIKE '%refer to the following passage%' "
    "OR s.content LIKE '%following passage.%'"
)


def _q(conn: sqlite3.Connection, sql: str, params: Tuple = ()) -> List[Tuple]:
    cur = conn.execute(sql, params)
    return cur.fetchall()


def audit_stimuli_with_cluster_markers(conn: sqlite3.Connection) -> List[Tuple]:
    sql = f"""
    SELECT s.id, s.stimulus_type, substr(s.content, 1, 140) AS snippet, COUNT(q.id) AS qcount
    FROM stimulus s LEFT JOIN question q ON q.stimulus_id = s.id
    WHERE s.stimulus_type = 'passage'
      AND ({CLUSTER_LIKE})
    GROUP BY s.id
    ORDER BY qcount, s.id
    """
    return _q(conn, sql)


def audit_duplicate_stimuli(conn: sqlite3.Connection) -> List[Tuple]:
    sql = """
    SELECT COUNT(*) AS dup_count, substr(content, 1, 100) AS snippet, GROUP_CONCAT(id) AS ids
    FROM stimulus
    WHERE stimulus_type = 'passage'
    GROUP BY content HAVING dup_count > 1
    ORDER BY dup_count DESC, snippet
    """
    return _q(conn, sql)


def audit_rc_orphans(conn: sqlite3.Connection) -> List[Tuple]:
    sql = """
    SELECT subtype, COUNT(*) AS n
    FROM question
    WHERE subtype IN ('rc_single', 'rc_multi', 'rc_select_passage')
      AND stimulus_id IS NULL
      AND status = 'live'
    GROUP BY subtype
    """
    return _q(conn, sql)


def audit_max_planck(conn: sqlite3.Connection) -> List[Tuple]:
    sql = """
    SELECT q.id, q.subtype, q.status, substr(q.prompt, 1, 140) AS prompt_snippet,
           q.stimulus_id, substr(COALESCE(s.content, ''), 1, 140) AS stim_snippet
    FROM question q LEFT JOIN stimulus s ON s.id = q.stimulus_id
    WHERE q.prompt LIKE '%Max Planck%' OR s.content LIKE '%Max Planck%'
    ORDER BY q.id
    """
    return _q(conn, sql)


def cluster_histogram(conn: sqlite3.Connection) -> List[Tuple]:
    """How many stimuli have k questions linked? (passage stimuli only)."""
    sql = """
    SELECT qcount, COUNT(*) AS n_stimuli FROM (
      SELECT s.id, COUNT(q.id) AS qcount
      FROM stimulus s LEFT JOIN question q ON q.stimulus_id = s.id AND q.status = 'live'
      WHERE s.stimulus_type = 'passage'
      GROUP BY s.id
    ) GROUP BY qcount ORDER BY qcount
    """
    return _q(conn, sql)


def write_audit(db_path: str, out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        dup = audit_duplicate_stimuli(conn)
        cluster_markers = audit_stimuli_with_cluster_markers(conn)
        orphans = audit_rc_orphans(conn)
        planck = audit_max_planck(conn)
        hist = cluster_histogram(conn)
    finally:
        conn.close()

    lines = []
    lines.append(f"# RC cluster integrity audit — {datetime.utcnow().isoformat()}Z")
    lines.append(f"DB: `{db_path}`\n")

    lines.append("## 1. Duplicate stimuli (same passage text in multiple rows)\n")
    if not dup:
        lines.append("_None._\n")
    else:
        lines.append(f"**Total duplicate-content groups: {len(dup)}**\n")
        lines.append("| copies | stimulus ids | snippet |")
        lines.append("|-------:|--------------|---------|")
        for n, snippet, ids in dup:
            snippet_clean = (snippet or "").replace("|", "\\|").replace("\n", " ")[:80]
            lines.append(f"| {n} | {ids} | {snippet_clean} |")
        lines.append("")

    lines.append("## 2. Stimuli containing cluster markers (\"Questions N-M are based on...\")\n")
    lines.append(f"**Total: {len(cluster_markers)}**\n")
    # Split by question count
    by_qcount = {}
    for sid, stype, snip, qc in cluster_markers:
        by_qcount.setdefault(qc, []).append((sid, snip))
    for qc in sorted(by_qcount):
        lines.append(f"### qcount={qc} ({len(by_qcount[qc])} stimuli)")
        for sid, snip in by_qcount[qc][:20]:
            s = (snip or "").replace("\n", " ")[:100]
            lines.append(f"- stim {sid}: {s}")
        if len(by_qcount[qc]) > 20:
            lines.append(f"- ... +{len(by_qcount[qc]) - 20} more")
        lines.append("")

    lines.append("## 3. RC orphan questions (stimulus_id IS NULL, status=live)\n")
    if not orphans:
        lines.append("_None._\n")
    else:
        lines.append("| subtype | count |")
        lines.append("|---------|------:|")
        for subtype, n in orphans:
            lines.append(f"| {subtype} | {n} |")
        lines.append("")

    lines.append("## 4. Max Planck passage (user's reported case)\n")
    if not planck:
        lines.append("_No matches._\n")
    else:
        lines.append("| qid | subtype | status | stim_id | prompt snippet | stim snippet |")
        lines.append("|----:|---------|--------|--------:|----------------|--------------|")
        for qid, subtype, status, prompt_snip, stim_id, stim_snip in planck:
            ps = (prompt_snip or "").replace("|", "\\|").replace("\n", " ")[:70]
            ss = (stim_snip or "").replace("|", "\\|").replace("\n", " ")[:70]
            lines.append(f"| {qid} | {subtype} | {status} | {stim_id} | {ps} | {ss} |")
        lines.append("")

    lines.append("## 5. Cluster size histogram (questions per passage stimulus, live)\n")
    lines.append("| qcount | #stimuli |")
    lines.append("|-------:|---------:|")
    for qc, n in hist:
        lines.append(f"| {qc} | {n} |")
    lines.append("")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"Wrote audit: {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default="/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-a6ab107e/data/gre_user.db",
    )
    ap.add_argument(
        "--out",
        default="/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-a6ab107e/data/audits/rc_cluster_integrity_2026_04_28.md",
    )
    args = ap.parse_args()
    write_audit(args.db, args.out)


if __name__ == "__main__":
    main()
