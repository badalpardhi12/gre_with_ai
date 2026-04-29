"""Deterministic backfill for two data-level anomalies:

1. rc_multi orphans: `rc_multi` Question rows with `stimulus_id IS NULL`.
   These all came from the `manhattan_5lb_2018` import; none has a recoverable
   passage in the DB. Per the audit they split into:
     - 1 empty-prompt row -> retire
     - 6 figure-referencing rows -> retire (cannot be answered without image)
     - 38 self-contained quant multi-select rows misclassified as verbal rc_multi
       -> retire (they could be reclassified to quant/mcq_multi in a follow-up)
   For the Kaplan worktree DB there is +1 orphan (a draft rc_single/rc_multi
   cluster about Orwell whose passage was never imported) -> left alone; draft
   status means session assembly won't touch it.

2. Data Interpretation cluster-size-1 items: 45 live `data_interp` questions,
   all from `ai_generated`. Each has its own unique synthesized chart stimulus,
   so they CANNOT be deterministically regrouped without inventing links --
   this is a generator-pipeline gap, not an extractor bug. One exception:
   stimuli 442 and 446 share identical chart imagery (only the caption
   differs). Questions 1852 and 1856 can be merged under a single stimulus
   to yield one legitimate 2-Q cluster. That is the only deterministic fix.

Usage:
    python backfill_clusters.py --db <path> --dry-run
    python backfill_clusters.py --db <path> --apply

The script is idempotent: running --apply twice is a no-op the second time.
"""
import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Orphan classification (mirrors audit_cluster_anomalies.py exactly)
# ---------------------------------------------------------------------------

FIG_MARKERS = [
    r'\babove\b', r'\bshown\b', r'\bas\s+shown\b',
    r'\baccording\s+to\s+the\s+graph\b',
    r'\baccording\s+to\s+the\s+chart\b',
    r'\baccording\s+to\s+the\s+table\b',
    r'\bbox-?and-?whisker\b',
    r'\bfrom\s+the\s+graph\b', r'\bfrom\s+the\s+chart\b',
    r'\bthe\s+graph\s+(on|below|above)\b',
    r'\bthe\s+chart\b',
    r'\bgraphical\s+representations\b',
    r'\bwhich\s+two\s+towns\b',
    r'\bwhich\s+.*\s+rosters\b',
    r'\bwhat\s+varsity\s+sports\s+rosters\b',
    r'\bon\s+what\s+varsity\b',
    r'\bin\s+the\s+figure\b', r'\bfigure\s+above\b',
]
FIG_RE = re.compile('|'.join(FIG_MARKERS), re.IGNORECASE)


def classify_orphan(prompt):
    """Return category for an rc_multi orphan."""
    if not prompt or not prompt.strip():
        return 'retire_empty'
    if FIG_RE.search(prompt):
        return 'retire_needs_figure'
    return 'retire_no_passage'


# ---------------------------------------------------------------------------
# DI dedupe detection (deterministic, single rule)
# ---------------------------------------------------------------------------

def find_di_dedup_groups(conn):
    """Find AI-generated DI stimuli that encode the same base64 image payload.

    Returns a list of groups (each group is a list of {qid, sid, title}).
    """
    import hashlib
    rows = conn.execute(
        """
        SELECT q.id, q.stimulus_id, s.title, s.content
        FROM question q JOIN stimulus s ON q.stimulus_id = s.id
        WHERE q.source = 'ai_generated'
          AND q.subtype = 'data_interp'
          AND q.status = 'live'
        """
    ).fetchall()
    buckets = {}
    for qid, sid, title, content in rows:
        m = re.search(r'base64,([A-Za-z0-9+/=]+)', content)
        # Use a long enough prefix (500 chars) for a safe deterministic key.
        key = m.group(1)[:500] if m else content[:500]
        h = hashlib.sha1(key.encode()).hexdigest()
        buckets.setdefault(h, []).append({'qid': qid, 'sid': sid, 'title': title})
    return [group for group in buckets.values() if len(group) > 1]


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------

def build_plan(conn):
    """Return a dict describing what would change (idempotent)."""
    # 1) rc_multi orphan classification (LIVE only -- we leave drafts alone)
    orphans = conn.execute(
        """
        SELECT id, source, status, prompt
        FROM question
        WHERE subtype='rc_multi' AND stimulus_id IS NULL AND status='live'
        ORDER BY id
        """
    ).fetchall()
    rc_retire = []
    for qid, source, status, prompt in orphans:
        cat = classify_orphan(prompt or '')
        rc_retire.append({
            'id': qid, 'source': source, 'status': status,
            'category': cat,
            'prompt_snippet': (prompt or '')[:100],
        })

    # 2) DI dedup groups (merge siblings into canonical stimulus = lowest id)
    di_merges = []
    for group in find_di_dedup_groups(conn):
        group_sorted = sorted(group, key=lambda g: g['sid'])
        canonical = group_sorted[0]
        for item in group_sorted[1:]:
            if item['sid'] == canonical['sid']:
                # Already merged on a prior run -- skip so the plan is idempotent.
                continue
            di_merges.append({
                'qid': item['qid'],
                'from_stimulus_id': item['sid'],
                'to_stimulus_id': canonical['sid'],
                'title': item['title'],
            })

    return {'rc_multi_retire': rc_retire, 'di_stimulus_merges': di_merges}


def summarize_plan(plan, db_path):
    lines = []
    lines.append(f"DB: {db_path}")
    lines.append(f"Step 1 -- retire {len(plan['rc_multi_retire'])} rc_multi live orphans")
    cats = {}
    for item in plan['rc_multi_retire']:
        cats[item['category']] = cats.get(item['category'], 0) + 1
    for cat, n in sorted(cats.items()):
        lines.append(f"    {cat}: {n}")
    lines.append(f"Step 2 -- merge {len(plan['di_stimulus_merges'])} DI questions onto canonical stimuli (dedupe)")
    for m in plan['di_stimulus_merges']:
        lines.append(f"    q{m['qid']}: stim {m['from_stimulus_id']} -> {m['to_stimulus_id']} ({m['title']})")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_plan(conn, plan):
    """Mutate the DB idempotently. Returns (rc_retired, di_merged, di_stim_deleted)."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()

    rc_retired = 0
    for item in plan['rc_multi_retire']:
        # Idempotent: only update if still live & still orphan
        res = cur.execute(
            """
            UPDATE question
            SET status='retired', updated_at=?
            WHERE id=? AND subtype='rc_multi' AND stimulus_id IS NULL AND status='live'
            """,
            (now, item['id']),
        )
        rc_retired += res.rowcount

    di_merged = 0
    freed_stim_ids = set()
    for m in plan['di_stimulus_merges']:
        res = cur.execute(
            """
            UPDATE question
            SET stimulus_id=?, updated_at=?
            WHERE id=? AND stimulus_id=?
            """,
            (m['to_stimulus_id'], now, m['qid'], m['from_stimulus_id']),
        )
        if res.rowcount:
            di_merged += 1
            freed_stim_ids.add(m['from_stimulus_id'])

    # Clean up orphan stimuli left over from the DI merge (safety: only delete
    # if nothing else still references them).
    di_stim_deleted = 0
    for sid in freed_stim_ids:
        ref = cur.execute(
            "SELECT COUNT(*) FROM question WHERE stimulus_id=?", (sid,)
        ).fetchone()[0]
        if ref == 0:
            cur.execute("DELETE FROM stimulus WHERE id=?", (sid,))
            di_stim_deleted += 1

    conn.commit()
    return rc_retired, di_merged, di_stim_deleted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def snapshot_counts(conn):
    return {
        'questions_live': conn.execute("SELECT COUNT(*) FROM question WHERE status='live'").fetchone()[0],
        'questions_retired': conn.execute("SELECT COUNT(*) FROM question WHERE status='retired'").fetchone()[0],
        'rc_multi_live_orphans': conn.execute(
            "SELECT COUNT(*) FROM question WHERE subtype='rc_multi' AND stimulus_id IS NULL AND status='live'"
        ).fetchone()[0],
        'di_live_stimuli': conn.execute(
            "SELECT COUNT(DISTINCT stimulus_id) FROM question WHERE subtype='data_interp' AND status='live' AND stimulus_id IS NOT NULL"
        ).fetchone()[0],
        'stimulus_count': conn.execute("SELECT COUNT(*) FROM stimulus").fetchone()[0],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--db', required=True, help='Path to gre_mock.db')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', default=None,
                        help='When --apply, copy DB here before mutating.')
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 2

    if args.apply:
        backup_dir = Path(args.backup_dir) if args.backup_dir else db_path.parent
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup_path = backup_dir / f"{db_path.name}.{ts}.bak"
        shutil.copy2(db_path, backup_path)
        print(f"[backup] {db_path} -> {backup_path}")

    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA foreign_keys = ON')
    before = snapshot_counts(conn)
    plan = build_plan(conn)

    print('-' * 72)
    print(summarize_plan(plan, str(db_path)))
    print('-' * 72)
    print(f"Before: {before}")

    if args.dry_run:
        print("(dry run -- no changes written)")
        return 0

    rc_retired, di_merged, di_stim_deleted = apply_plan(conn, plan)
    after = snapshot_counts(conn)
    print(f"Applied: rc_multi_retired={rc_retired}, di_questions_merged={di_merged}, di_stimuli_deleted={di_stim_deleted}")
    print(f"After:  {after}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
