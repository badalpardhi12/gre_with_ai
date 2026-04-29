"""Backfill Princeton Question.figure_refs from the consolidator JSON.

For each JSON item flagged `needs_vision=True` with an `image_ref`, locate
its matching Question row in the worktree's gre_user.db and set
`Question.figure_refs` to the JSON-serialized [image_ref].

Matching strategy
-----------------
The JSON addresses items by (drill, question_num) which doesn't map cleanly
to the DB's auto-assigned source_anchor (QST###). We instead normalize the
stem text to a fingerprint and look for an exact 1:1 match among Princeton
rows:

    fingerprint = strip_punct(lowercase(collapse_whitespace(stem)))[:80]

Ties (multiple DB rows with the same fingerprint) and misses (no DB row) are
both logged and skipped — we never guess.

Usage
-----
    venv/bin/python scripts/backfill_princeton_figure_refs.py
    venv/bin/python scripts/backfill_princeton_figure_refs.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

WT = Path("/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-ac007118")
MAIN = Path("/Users/chiku/Documents/side_projects/gre_with_ai")

os.chdir(str(WT))
sys.path.insert(0, str(WT))

PRINCETON_JSON = (
    MAIN / "data" / "extracted" / "princeton" / "princeton_extracted.json"
)


def fingerprint(text):
    """Normalize a stem to a stable 80-char key.

    The JSON stems sometimes insert spaces around punctuation the DB does
    not (e.g. JSON "Finnegan ’ s" vs DB "Finnegan’s"), so we strip ALL
    whitespace in addition to punctuation. Truncation at 80 chars keeps
    the key stable across minor mid-stem edits (rare on raw imports).
    """
    if not text:
        return ""
    t = text.lower()
    # strip all non-alphanumeric (incl. spaces + punctuation + smart quotes)
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t[:80]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from models.database import Question  # noqa: E402

    with open(PRINCETON_JSON) as f:
        data = json.load(f)
    items = [q for q in data["questions"] if q.get("image_ref")]
    print(f"JSON items with image_ref: {len(items)}")

    # Build DB fingerprint index over Princeton rows only.
    fp_to_ids = defaultdict(list)
    princeton_rows = Question.select(Question.id, Question.prompt).where(
        Question.source == "princeton_2012"
    )
    for r in princeton_rows:
        fp_to_ids[fingerprint(r.prompt)].append(r.id)
    print(f"Princeton rows indexed: {sum(len(v) for v in fp_to_ids.values())}")
    print(f"Unique fingerprints: {len(fp_to_ids)}")

    matched = 0
    ambiguous = 0
    missing = 0
    ambiguous_log = []
    missing_log = []

    for item in items:
        fp = fingerprint(item["prompt"])
        ids = fp_to_ids.get(fp, [])
        if len(ids) == 1:
            qid = ids[0]
            if not args.dry_run:
                Question.update(
                    figure_refs=json.dumps([item["image_ref"]])
                ).where(Question.id == qid).execute()
            matched += 1
        elif len(ids) > 1:
            ambiguous += 1
            ambiguous_log.append(
                (item["drill"], item["question_num"], item["image_ref"], ids)
            )
        else:
            missing += 1
            missing_log.append(
                (item["drill"], item["question_num"], item["image_ref"],
                 item["prompt"][:80])
            )

    print()
    print(f"Matched:   {matched}")
    print(f"Ambiguous: {ambiguous}")
    print(f"Missing:   {missing}")

    if ambiguous_log:
        print("\n--- ambiguous (showing up to 5) ---")
        for row in ambiguous_log[:5]:
            print(row)
    if missing_log:
        print("\n--- missing (showing up to 10) ---")
        for row in missing_log[:10]:
            print(row)

    # Double-check: how many Princeton rows now have figure_refs non-empty?
    total_with_refs = Question.select().where(
        (Question.source == "princeton_2012")
        & (Question.figure_refs != "[]")
        & (Question.figure_refs.is_null(False))
    ).count()
    print(f"\nPrinceton rows with figure_refs set: {total_with_refs}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
