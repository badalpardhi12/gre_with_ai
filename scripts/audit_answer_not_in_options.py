#!/usr/bin/env python3
"""Detect MCQ/DI items whose explanation concludes a numeric value that is
NOT among the answer options (and differs from the marked-correct option).

This is the failure class the user reported on qid 5420 and the inverse of
``audit_answer_key_drift.py``: drift catches "explanation picks a DIFFERENT
valid option"; this catches "explanation's computed answer is not an option
at all" — which the drift judge skips (it returns null when no option
matches). Deterministic + offline; output is hand-verified before any
migration acts on it (no auto-mutation).

Usage:
    venv/bin/python scripts/audit_answer_not_in_options.py [--db data/gre_mock.db]
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys


def numify(s):
    """Best-effort float from an option/explanation token; None if not numeric."""
    if s is None:
        return None
    s = str(s)
    s = re.sub(r'(?i)approximately|approx\.?|about|~|\$|,|percent|%|'
               r'billion|million|thousand|kwh|hours?|hrs?|minutes?|mins?|'
               r'miles?|km|kg|lbs?|°[cf]?|°|degrees?', '', s)
    s = s.replace('−', '-').strip()        # unicode minus
    m = re.fullmatch(r'-?\d+(?:\.\d+)?', s)
    return float(m.group()) if m else None


def near(a, b, rel=0.012, abs_=0.05):
    return abs(a - b) <= max(abs_, rel * max(abs(a), abs(b)))


def find(conn):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id,subtype,source,explanation FROM question "
        "WHERE status='live' AND subtype IN ('mcq_single','data_interp')"
    ).fetchall()
    opts = {}
    for o in conn.execute(
            "SELECT question_id,option_label,option_text,is_correct "
            "FROM questionoption"):
        opts.setdefault(o["question_id"], []).append(o)

    flagged = []
    for r in rows:
        oo = opts.get(r["id"], [])
        numeric_opts = [(o, numify(o["option_text"])) for o in oo]
        numeric_opts = [(o, v) for o, v in numeric_opts if v is not None]
        # Only items whose options are (almost) all numeric — otherwise the
        # "value not among options" comparison is meaningless.
        if not oo or len(numeric_opts) < max(3, 0.8 * len(oo)):
            continue
        corr = [o for o in oo if o["is_correct"]]
        if len(corr) != 1:
            continue
        corr_val = numify(corr[0]["option_text"])
        if corr_val is None:
            continue
        expl = r["explanation"] or ""
        if not expl.strip():
            continue
        # The explanation's concluded value: last "= X" / "≈ X" / "answer is X".
        concl = re.findall(
            r'(?:=|≈|≅|answer\s+is|equals?)\s*\$?'
            r'(-?\d[\d,]*(?:\.\d+)?)\s*%?', expl, re.I)
        if not concl:
            continue
        cval = numify(concl[-1])
        if cval is None:
            continue
        matches_any = any(near(cval, v) for _o, v in numeric_opts)
        if (not matches_any) and (not near(cval, corr_val)):
            flagged.append({
                "qid": r["id"], "source": r["source"],
                "marked_label": corr[0]["option_label"],
                "marked_value": corr_val, "concluded_value": cval,
                "options": [o["option_text"] for o in oo],
                "expl_tail": expl[-160:].replace("\n", " "),
            })
    return flagged


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/gre_mock.db")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    flagged = find(conn)
    conn.close()
    print(f"answer-not-in-options candidates: {len(flagged)}\n")
    for f in flagged:
        print(f"qid {f['qid']:<5} {f['source']:<16} key={f['marked_label']}"
              f"({f['marked_value']}) concluded={f['concluded_value']}")
        print(f"   options={f['options']}")
        print(f"   …{f['expl_tail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
