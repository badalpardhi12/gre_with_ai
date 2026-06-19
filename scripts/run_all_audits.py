#!/usr/bin/env python3
"""
Aggregate production-readiness gate for the GRE question bank.

Runs every corruption-class audit against a database and fails (exit 1) if any
class reappears. This is the single command CI / a pre-release check runs to
guarantee none of the WS-A..E fixes silently regress:

  - option grafts (high-confidence mcq_multi)          [WS-A]
  - phantom figures (live, user-visible)               [WS-B]
  - exact-duplicate items left live in a dup group     [WS-C]
  - GRE shape faithfulness (QC canonical, SE 6/2, ...) [WS-D]
  - structural validator errors                        [pre-existing validators]

Usage:
    venv/bin/python scripts/run_all_audits.py            # gate the seed (live)
    venv/bin/python scripts/run_all_audits.py --db data/gre_user.db

Exit 0 = all gates pass, 1 = at least one regression, 2 = harness error.
"""
import sys
import os
import argparse
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Exact-dupe pairs that migration 041 retired (the higher qid). The gate
# asserts none of those retired extras drifted back to 'live'.
_EXACT_DUPE_RETIRED = [2166, 2128, 1622, 2232]


def _gate_option_graft(db):
    from scripts.audit_option_graft import (
        load_questions, detect_provenance_divergence, detect_distinctive_shared_sets)
    conn = sqlite3.connect(db)
    qs = load_questions(conn, live_only=True)
    conn.close()
    prov = detect_provenance_divergence(qs)
    shared = detect_distinctive_shared_sets(qs)
    suspects = {qid for qid in (set(prov) | set(shared))
                if qs[qid]["subtype"] == "mcq_multi"}
    return len(suspects), sorted(suspects)


def _gate_figures(db):
    from scripts.audit_figure_render import classify
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT q.id, q.source, q.subtype, q.status, q.prompt, s.id, "
        "s.stimulus_type, s.content, s.render_spec FROM question q "
        "LEFT JOIN stimulus s ON q.stimulus_id=s.id WHERE q.status='live'"
    ).fetchall()
    conn.close()
    phantom = []
    for row in rows:
        res = classify(row)
        if res and res[0] != "RENDERS":
            phantom.append(row[0])
    return len(phantom), phantom[:20]


def _gate_exact_dupes_retired(db):
    conn = sqlite3.connect(db)
    live = [qid for qid in _EXACT_DUPE_RETIRED
            if (conn.execute("SELECT status FROM question WHERE id=?", (qid,))
                .fetchone() or ["absent"])[0] == "live"]
    conn.close()
    return len(live), live


def _gate_faithfulness(db):
    from scripts.audit_faithfulness import audit
    conn = sqlite3.connect(db)
    _rows, viol = audit(conn, live_only=True)
    conn.close()
    total = sum(len(v) for v in viol.values())
    return total, {k: len(v) for k, v in viol.items() if v}


# Minimum live items each measure must carry in EACH coarse difficulty band
# (lo = bands 1-2, mid = band 3, hi = bands 4-5) so the per-section
# difficulty SPREAD (balancing fix #1) is satisfiable — a routed easy/medium/
# hard section, or the medium-centered S1, can be filled from the right band
# without forcing heavy reuse. If the bank ever drifts toward a single band
# (e.g. everything at the band-3 default), the spread enforcement silently
# no-ops and the adaptive forms stop feeling different — this gate catches
# that. Floor is conservative: one 15-item section can be ~8 from any band.
_DIFFICULTY_BAND_FLOOR = 15


def _coarse_band_of(d):
    d = d or 3
    return "lo" if d <= 2 else ("mid" if d == 3 else "hi")


def _gate_difficulty_spread(db):
    """Each measure must have >= _DIFFICULTY_BAND_FLOOR live items in every
    coarse band so the section difficulty spread is satisfiable."""
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT measure, difficulty_target FROM question "
        "WHERE status='live' AND measure IN ('verbal','quant')"
    ).fetchall()
    conn.close()
    counts = {}
    for measure, diff in rows:
        counts.setdefault(measure, {"lo": 0, "mid": 0, "hi": 0})
        counts[measure][_coarse_band_of(diff)] += 1
    thin = {}
    for measure, bands in counts.items():
        for band, n in bands.items():
            if n < _DIFFICULTY_BAND_FLOOR:
                thin[f"{measure}/{band}"] = n
    return len(thin), thin


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/gre_mock.db")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 2

    gates = [
        ("option_graft (mcq_multi)", _gate_option_graft),
        ("phantom_figures (live)", _gate_figures),
        ("exact_dupes_relived", _gate_exact_dupes_retired),
        ("gre_shape_faithfulness", _gate_faithfulness),
        ("difficulty_spread_satisfiable", _gate_difficulty_spread),
    ]
    print(f"Production-readiness gate on {args.db}\n")
    failed = 0
    for name, fn in gates:
        try:
            count, detail = fn(args.db)
        except Exception as e:  # harness error on one gate shouldn't hide others
            print(f"  ERROR  {name}: {e}")
            failed += 1
            continue
        status = "PASS" if count == 0 else "FAIL"
        if count:
            failed += 1
        print(f"  [{status}] {name}: {count}" + (f"  {detail}" if count else ""))
    print()
    if failed:
        print(f"GATE FAILED — {failed} regression class(es) present.")
        return 1
    print("GATE PASSED — no corruption-class regressions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
