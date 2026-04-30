"""
Retire all verbal questions that didn't come from the Manhattan 5lb
import or our own LLM-generated batch.

Why: the existing verbal bank had legacy contamination from earlier
imports (`source='imported'` covers Kaplan, Princeton, and the
seed-data CSV — all of which had answer-key, option-text, or formatting
defects we've been chasing). Now that the Manhattan v3 extraction
gives us a high-quality verbal foundation, we want only:

  - source = 'manhattan_5lb_2018' (the new authoritative bank)
  - source = 'ai_generated'        (the rc_multi / rc_select_passage
                                    fillers from scripts/generate_questions
                                    and scripts/fill_rc_balance_gaps)

…to remain `status='live'` on the verbal side. Everything else gets
flipped to `status='retired'` so the assembler stops serving them
(see services.question_bank.select_questions_composed which filters
on status='live'). Quant questions are untouched.

This is RETIREMENT, not deletion — the rows stay in the DB so:
  1. Past Response/Session FK references aren't broken.
  2. We can audit what was retired (status='retired' filter).
  3. We can un-retire if we change our mind (just flip status back).

Usage:
    venv/bin/python scripts/retire_verbal_contamination.py            # dry-run
    venv/bin/python scripts/retire_verbal_contamination.py --apply    # write
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import db, Question, init_db
from services.log import get_logger

logger = get_logger("retire_verbal_contamination")

KEEP_SOURCES = ("manhattan_5lb_2018", "ai_generated")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually write status='retired' to the DB "
                         "(default: dry-run)")
    ap.add_argument("--measure", default="verbal",
                    help="restrict the cleanup to one measure "
                         "(default: verbal). Pass 'all' to apply across "
                         "verbal+quant, but that's almost never what you want.")
    args = ap.parse_args()

    init_db()
    db.connect(reuse_if_open=True)

    base = Question.select().where(Question.measure == args.measure) \
        if args.measure != "all" else Question.select()
    rows = list(base)
    print(f"Inspecting {len(rows)} {args.measure} questions…")

    # Bucket the live rows by source
    live_by_source = Counter()
    retired_by_source = Counter()
    for r in rows:
        if r.status == "live":
            live_by_source[r.source or "(none)"] += 1
        else:
            retired_by_source[r.source or "(none)"] += 1

    print(f"\nCurrent live ({sum(live_by_source.values())}):")
    for s, n in live_by_source.most_common():
        keep = "✓ keep" if s in KEEP_SOURCES else "✗ RETIRE"
        print(f"  {s:<25} {n:>5}  {keep}")
    print(f"\nAlready retired ({sum(retired_by_source.values())}):")
    for s, n in retired_by_source.most_common():
        print(f"  {s:<25} {n:>5}")

    targets = [r for r in rows
               if r.status == "live" and (r.source or "") not in KEEP_SOURCES]
    if not targets:
        print(f"\nNothing to retire — every live {args.measure} question "
              f"is already from one of: {KEEP_SOURCES}.")
        return 0

    target_ids = [r.id for r in targets]
    print(f"\n{'PLAN' if not args.apply else 'APPLYING'}: retire "
          f"{len(targets)} {args.measure} questions whose source is not in "
          f"{KEEP_SOURCES}.")
    by_source = Counter((r.source or "(none)") for r in targets)
    for s, n in by_source.most_common():
        print(f"  source={s!r}: {n} rows")

    if not args.apply:
        print("\nDry-run; no changes written. Re-run with --apply to retire.")
        return 0

    with db.atomic():
        n = (Question
             .update(status="retired")
             .where(Question.id.in_(target_ids))
             .execute())
    logger.info("retired %d %s questions (contamination cleanup)",
                n, args.measure)
    print(f"\nDone. Retired {n} rows. Verbal bank now serves only "
          f"manhattan_5lb_2018 + ai_generated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
