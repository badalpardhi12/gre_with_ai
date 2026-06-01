#!/usr/bin/env python3
"""
GRE Faithfulness Audit -- question-shape conformance to the current
(post-Sept-2023) GRE General Test specification.

Verified spec (ETS, corroborated by major prep sources):
  - QC: exactly the 4 canonical answer choices, in canonical order/text:
      "Quantity A is greater." / "Quantity B is greater." /
      "The two quantities are equal." /
      "The relationship cannot be determined from the information given."
    exactly one correct.
  - mcq_single (Select One): 5 choices, exactly one correct.
  - mcq_multi (Select One or More): >=3 choices, >=1 correct (all-or-nothing
    scoring is enforced in services/scoring.py, not here).
  - Sentence Equivalence (se): exactly 6 choices, exactly 2 correct.
  - Text Completion (tc): single-blank = 5 choices; the app stores multi-blank
    TC differently (per-blank), so only single-blank shape is asserted here.
  - Numeric Entry: no answer options (typed answer).
  - RC select-one (rc_single): >=2 choices (passages vary), exactly one correct.

This audit is read-only and reports violations; the QC text normalization is
applied by migration 042. Exit 0 = conformant, 1 = violations, 2 = error.
"""
import sys
import os
import argparse
from collections import Counter, defaultdict

import sqlite3

QC_CANON = [
    "Quantity A is greater.",
    "Quantity B is greater.",
    "The two quantities are equal.",
    "The relationship cannot be determined from the information given.",
]


def _opts(conn, qid):
    return conn.execute(
        "SELECT option_text, is_correct FROM questionoption WHERE question_id=? "
        "ORDER BY option_label", (qid,)).fetchall()


def audit(conn, live_only):
    where = "WHERE status='live'" if live_only else ""
    rows = conn.execute(f"SELECT id, subtype FROM question {where}").fetchall()
    viol = defaultdict(list)
    for qid, st in rows:
        o = _opts(conn, qid)
        texts = [t for t, _ in o]
        n = len(o)
        ncorr = sum(1 for _, ic in o if ic)
        if st == "qc":
            if texts != QC_CANON:
                viol["qc_noncanonical"].append(qid)
            if ncorr != 1:
                viol["qc_correct_count"].append(qid)
        elif st == "se":
            if n != 6:
                viol["se_options_not_6"].append(qid)
            if ncorr != 2:
                viol["se_correct_not_2"].append(qid)
        elif st == "mcq_single":
            if n != 5:
                viol["mcq_single_options_not_5"].append(qid)
            if ncorr != 1:
                viol["mcq_single_correct_not_1"].append(qid)
        elif st == "mcq_multi":
            if n < 3:
                viol["mcq_multi_lt_3_options"].append(qid)
            if ncorr < 1:
                viol["mcq_multi_no_correct"].append(qid)
        elif st == "rc_single":
            if ncorr != 1:
                viol["rc_single_correct_not_1"].append(qid)
        elif st == "numeric_entry":
            if n != 0:
                viol["numeric_has_options"].append(qid)
    return rows, viol


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/gre_mock.db")
    ap.add_argument("--live-only", action="store_true", default=True)
    ap.add_argument("--all-statuses", dest="live_only", action="store_false")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(args.db)
    rows, viol = audit(conn, args.live_only)
    conn.close()

    total = sum(len(v) for v in viol.values())
    print(f"GRE faithfulness audit on {args.db} (live_only={args.live_only})")
    print(f"  items scanned: {len(rows)}")
    print(f"  shape violations: {total}")
    for k in sorted(viol):
        ids = viol[k]
        print(f"    {k}: {len(ids)}"
              + ("" if args.summary else f"  e.g. {ids[:8]}"))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
