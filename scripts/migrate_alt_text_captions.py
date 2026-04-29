"""
Migration: strip vision-generated alt-text captions from Stimulus.content.

Context
-------
Some build-time generators (see scripts/_fix_di_charts.py and
scripts/_extract_manhattan.py) appended a vision-generated descriptive
caption after each image:

    <img src="data:image/png;base64,…" /><p style="text-align:center;
        font-style:italic; color:#a0a0a0; margin-top:6px;">A cross-shaped
        figure composed of 5 equal squares arranged in a plus sign
        pattern…</p>

That caption is alt-text metadata — it's useful for screen-reader fallback
but not for a sighted test-taker, who sees the image itself. Rendering the
caption inline makes the passage read like it includes a redundant
description of its own diagram.

What this migration does
------------------------
1. Walks every row in `stimulus` table.
2. For each `<p>` caption styled with the marker color / italic that
   reads like a figure description (see
   services.figure_captions.is_alt_text_caption), wrap the whole
   element inside an HTML comment `<!--alt-text:…-->`. The
   `widgets.html_sanitizer.safe_html` bleach call already uses
   `strip_comments=True`, so the wrapped text disappears at render time
   but the string survives in the DB — if the heuristic ever
   misclassifies a legit caption we can recover it.
3. Leaves unit / axis labels ("Sales in thousands of dollars") alone —
   they carry semantic data the question solver needs.

Idempotent: rerunning the migration finds no matching plain captions the
second time around (they've all been converted to comments).

Usage
-----
    venv/bin/python scripts/migrate_alt_text_captions.py [--dry-run] [--db PATH]

Defaults to writing against `config.DB_PATH` (data/gre_mock.db).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make the project root importable when this script is run directly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.figure_captions import strip_alt_text_captions


def _connect(db_path: str):
    import sqlite3
    return sqlite3.connect(db_path)


def run(db_path: str, dry_run: bool = False) -> dict:
    """Apply (or preview) the caption rewrite. Returns a summary dict."""
    conn = _connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, content FROM stimulus")
    rows = cur.fetchall()

    affected = 0
    total_stripped = 0
    samples: list[dict] = []

    for sid, content in rows:
        if not content:
            continue
        new_content, stripped = strip_alt_text_captions(content)
        if not stripped:
            continue
        affected += 1
        total_stripped += len(stripped)
        if len(samples) < 5:
            samples.append({
                "stimulus_id": sid,
                "stripped_count": len(stripped),
                "first_caption": stripped[0][:160],
            })
        if not dry_run:
            cur.execute(
                "UPDATE stimulus SET content = ? WHERE id = ?",
                (new_content, sid),
            )

    if not dry_run:
        conn.commit()
    conn.close()

    return {
        "total_stimuli": len(rows),
        "affected_stimuli": affected,
        "captions_stripped": total_stripped,
        "samples": samples,
        "dry_run": dry_run,
    }


def _cli():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change but don't write.")
    ap.add_argument("--db", default=None, help="Override DB path.")
    args = ap.parse_args()

    if args.db:
        db_path = args.db
    else:
        # Import lazily so test runners that don't have config configured
        # can still import this module.
        from config import DB_PATH
        db_path = str(DB_PATH)

    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}")
        sys.exit(2)

    summary = run(db_path, dry_run=args.dry_run)
    mode = "DRY RUN" if args.dry_run else "APPLIED"
    print(f"[{mode}] alt-text caption cleanup on {db_path}")
    print(f"  Stimuli scanned: {summary['total_stimuli']}")
    print(f"  Stimuli affected: {summary['affected_stimuli']}")
    print(f"  Captions stripped: {summary['captions_stripped']}")
    if summary["samples"]:
        print("  Samples:")
        for s in summary["samples"]:
            print(f"    stim {s['stimulus_id']}: {s['first_caption']}")


if __name__ == "__main__":
    _cli()
