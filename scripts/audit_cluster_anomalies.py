"""Forensic classification of rc_multi orphans and DI cluster-size-1 items across all 4 DBs."""
import sqlite3
import re
import json
from pathlib import Path

DBS = {
    'main':      '/Users/chiku/Documents/side_projects/gre_with_ai/data/gre_mock.db',
    'princeton': '/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-a9405213/data/gre_mock.db',
    'kaplan':    '/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-a9aaa962/data/gre_mock.db',
    'synthetic': '/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-a5d145f1/data/gre_mock.db',
}

FIG_MARKERS = [
    r'\babove\b', r'\bshown\b', r'\bas\s+shown\b', r'\baccording\s+to\s+the\s+graph\b',
    r'\baccording\s+to\s+the\s+chart\b', r'\baccording\s+to\s+the\s+table\b',
    r'\bbox-?and-?whisker\b', r'\bfrom\s+the\s+graph\b', r'\bfrom\s+the\s+chart\b',
    r'\bthe\s+graph\s+(on|below|above)', r'\bthe\s+chart\b', r'\bgraphical\s+representations\b',
    r'\bwhich\s+two\s+towns\b', r'\bwhich\s+.*\s+rosters\b',
    r'\bwhat\s+varsity\s+sports\s+rosters\b', r'\bon\s+what\s+varsity\b',
    r'\bin\s+the\s+figure\b', r'\bfigure\s+above\b',
]
FIG_RE = re.compile('|'.join(FIG_MARKERS), re.IGNORECASE)

def classify_orphan(prompt: str) -> str:
    """Return 'retire_empty', 'retire_needs_figure', or 'self_contained'."""
    if not prompt or not prompt.strip():
        return 'retire_empty'
    if FIG_RE.search(prompt):
        return 'retire_needs_figure'
    return 'self_contained'

def audit_db(path):
    c = sqlite3.connect(path)
    out = {'path': path}
    # Totals
    out['total_questions'] = c.execute('SELECT COUNT(*) FROM question').fetchone()[0]
    out['total_stimulus'] = c.execute('SELECT COUNT(*) FROM stimulus').fetchone()[0]

    # DI cluster size histogram, live
    rows = c.execute("""
        SELECT source, cnt, COUNT(*)
        FROM (SELECT source, stimulus_id, COUNT(*) AS cnt FROM question
              WHERE subtype='data_interp' AND status='live' AND stimulus_id IS NOT NULL
              GROUP BY source, stimulus_id)
        GROUP BY source, cnt ORDER BY source, cnt""").fetchall()
    out['di_live_histogram'] = [{'source': r[0], 'cluster_size': r[1], 'num_clusters': r[2]} for r in rows]

    # DI questions with null stimulus
    out['di_live_null_stimulus'] = c.execute(
        "SELECT COUNT(*) FROM question WHERE subtype='data_interp' AND status='live' AND stimulus_id IS NULL"
    ).fetchone()[0]

    # rc_multi orphan classification
    orphans = c.execute("""
        SELECT id, source, status, prompt FROM question
        WHERE subtype='rc_multi' AND stimulus_id IS NULL
        ORDER BY id""").fetchall()
    classified = {'retire_empty': [], 'retire_needs_figure': [], 'self_contained': []}
    for qid, source, status, prompt in orphans:
        classified[classify_orphan(prompt or '')].append({
            'id': qid, 'source': source, 'status': status,
            'prompt_snippet': (prompt or '')[:120]
        })
    out['rc_multi_orphans'] = {
        k: {'count': len(v), 'items': v} for k, v in classified.items()
    }
    out['rc_multi_orphan_total'] = sum(len(v) for v in classified.values())

    # rc_multi total (live + draft)
    out['rc_multi_totals'] = dict(c.execute(
        "SELECT status, COUNT(*) FROM question WHERE subtype='rc_multi' GROUP BY status"
    ).fetchall())

    # Potential DI dedupe candidates (stimuli with identical first-200 base64 chars)
    import hashlib
    di_stim = c.execute("""
        SELECT q.id, q.stimulus_id, s.title, s.content
        FROM question q JOIN stimulus s ON q.stimulus_id=s.id
        WHERE q.subtype='data_interp' AND q.status='live'""").fetchall()
    by_hash = {}
    for qid, sid, title, content in di_stim:
        m = re.search(r'base64,([A-Za-z0-9+/=]+)', content)
        key = m.group(1)[:500] if m else content[:500]
        h = hashlib.sha1(key.encode()).hexdigest()[:12]
        by_hash.setdefault(h, []).append({'qid': qid, 'sid': sid, 'title': title})
    dupe_groups = [v for v in by_hash.values() if len(v) > 1]
    out['di_dedup_candidates'] = dupe_groups
    c.close()
    return out

def main():
    result = {k: audit_db(v) for k, v in DBS.items()}
    out_path = Path('/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-afcf77e0/data/audits/audit_raw.json')
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"Wrote {out_path}")
    for db, data in result.items():
        print(f"\n=== {db} ({data['path']}) ===")
        print(f"  total_q={data['total_questions']}, total_stim={data['total_stimulus']}")
        print(f"  DI live histogram: {data['di_live_histogram']}")
        print(f"  DI live null-stim: {data['di_live_null_stimulus']}")
        print(f"  rc_multi orphans: total={data['rc_multi_orphan_total']}")
        for k, v in data['rc_multi_orphans'].items():
            print(f"    {k}: {v['count']}")
        print(f"  DI dedup candidates: {len(data['di_dedup_candidates'])} groups")
        for g in data['di_dedup_candidates']:
            print(f"    {g}")

if __name__ == '__main__':
    main()
