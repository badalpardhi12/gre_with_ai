#!/usr/bin/env python3
"""
GRE Mock Database Audit -- Option-Graft Detection
=================================================

Detects the "option-graft" corruption class that the existing
``audit_answer_key_drift.py`` is structurally blind to: cases where the
``questionoption`` rows attached to a question actually belong to a
*different* question (grafted during a two-pass seed-mirror). Because the
drift audit reasons about *which stored option the explanation concludes*,
it silently accepts grafted options as ground truth. This audit instead
asks the orthogonal question: **do these options even belong to this stem?**

Two HIGH-PRECISION signals (an earlier draft used "any shared option-set" and
"option tokens absent from explanation"; both proved hopelessly noisy --
distinct arithmetic questions legitimately share generic integer option-sets
like {2,3,4,5,6}, and a well-formed MCQ's explanation normally names only the
*correct* value, not the four distractors, so ~80% token-absence is the norm,
not corruption). The refined signals are:

  (1) PROVENANCE_DIVERGENCE (high confidence) -- for AI-generated items that
      carry a ``provenance_json.judge_result`` (recorded at generation, never
      rewritten), the numeric tokens the judge rationales discuss are the
      item's ORIGINAL options. If those barely intersect the currently-stored
      option tokens, the options were grafted in after generation. This is the
      exact fingerprint of the two-pass seed-mirror graft and directly drives
      WS-A repair.

  (2) DISTINCTIVE_SHARED_SET (high confidence) -- two or more distinct
      questions share an identical full option-set AND that set is
      *distinctive* (contains fractions, ratios, LaTeX, %/currency, or
      text/multi-word choices -- NOT a plain integer or arithmetic-progression
      set). Distinct authored questions do share {2,3,4,5,6}; they do not share
      {5/11, 35/66, 1/2, 5/12, 7/22, 35/132}. A small allow-list handles
      genuinely shared scenarios (e.g. a menu reused across two items).

QC options are canonical fixed text and are excluded from both signals.

Usage:
    python scripts/audit_option_graft.py                 # full report
    python scripts/audit_option_graft.py --summary       # counts only
    python scripts/audit_option_graft.py --export        # write JSON manifest
    python scripts/audit_option_graft.py --db data/gre_user.db
    python scripts/audit_option_graft.py --live-only     # restrict to status=live

Exit codes:
    0 = clean
    1 = graft suspects found
    2 = error

The exported manifest (data/audits/option_graft_manifest_<date>.json) is the
canonical worklist consumed by the WS-A repair migration.
"""

import sys
import os
import re
import json
import argparse
import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3

# QC options are fixed canonical text shared by EVERY qc item by design.
SKIP_SUBTYPES_FOR_TOKENS = {"qc"}

# Verbal subtypes share text option-sets when an item is duplicated across two
# source ingestions (e.g. legacy 'imported' vs 'princeton_2012'). That is a
# near-duplicate concern handled by WS-C dedup, NOT an option graft, so the
# shared-set graft signal excludes them.
VERBAL_SUBTYPES = {"rc_single", "rc_multi", "se", "tc", "rc_select_passage"}

# Genuinely-shared option scenarios that are NOT corruption (reviewed).
# Keyed by the normalized option-set signature; empty until a real one is
# confirmed during review (kept explicit so nothing is silently excused).
SHARED_OPTION_SET_ALLOWLIST = set()

# Items the provenance-divergence signal flags but which are VERIFIED correct
# (the options match the prompt + explanation). q5394's ratio options ("1:2"…)
# parse into small integers that don't appear in its count-based judge prose,
# so the numeric-overlap heuristic mis-fires; it is the native owner of those
# ratio options (q5374 was the graft victim, repaired by migration 039).
VERIFIED_NATIVE_QIDS = {5394}

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:/\d+)?|−?\d+")


def _norm_text(s):
    if s is None:
        return ""
    # unify unicode minus, strip whitespace, lowercase for comparison
    return re.sub(r"\s+", " ", s.replace("−", "-")).strip().lower()


def _option_signature(option_texts):
    """A stable signature for a full option-set: sorted multiset of
    normalized option texts. Order-independent (label assignment varies)."""
    return "||".join(sorted(_norm_text(t) for t in option_texts))


def _nums(s):
    """Numeric tokens (incl. fractions / negatives / decimals), normalized."""
    s = (s or "").replace("−", "-")
    return set(m.group(0).replace("−", "-") for m in _NUM_RE.finditer(s))


def _is_distinctive(option_texts):
    """A set is graft-distinctive if distinct authored questions would not
    plausibly land on it by coincidence. Plain integer / arithmetic-progression
    / simple-currency sets are NOT distinctive (commonly shared); fractions,
    ratios, LaTeX, and text/multi-word choices ARE."""
    joined = " ".join(t or "" for t in option_texts)
    if "\\" in joined:                       # LaTeX
        return True
    if re.search(r"\d+\s*:\s*\d+", joined):   # ratios a:b
        return True
    if re.search(r"\b\d+\s*/\s*\d+\b", joined):  # fractions a/b
        return True
    # any option carrying two or more alphabetic words -> text choices
    for t in option_texts:
        if len(re.findall(r"[A-Za-z]{2,}", t or "")) >= 2:
            return True
    return False


def load_questions(conn, live_only):
    where = "WHERE q.subtype IS NOT NULL"
    if live_only:
        where += " AND q.status='live'"
    rows = conn.execute(
        f"""
        SELECT q.id, q.source, q.subtype, q.status, q.prompt, q.explanation,
               q.provenance_json
        FROM question q
        {where}
        """
    ).fetchall()
    out = {}
    for qid, source, subtype, status, prompt, expl, pj in rows:
        opts = conn.execute(
            "SELECT option_label, option_text, is_correct FROM questionoption "
            "WHERE question_id=? ORDER BY option_label",
            (qid,),
        ).fetchall()
        out[qid] = dict(
            id=qid, source=source, subtype=subtype, status=status,
            prompt=prompt or "", explanation=expl or "",
            provenance_json=pj or "", options=opts,
        )
    return out


def detect_distinctive_shared_sets(questions):
    """Signal (2): identical full option-sets across distinct questions, but
    only when the set is distinctive enough that coincidental sharing is
    implausible."""
    by_sig = defaultdict(list)
    sig_opts = {}
    for q in questions.values():
        if not q["options"] or len(q["options"]) < 2:
            continue
        if q["subtype"] in SKIP_SUBTYPES_FOR_TOKENS:
            continue
        if q["subtype"] in VERBAL_SUBTYPES:
            continue  # verbal text-option dupes are a WS-C dedup concern
        if q["id"] in VERIFIED_NATIVE_QIDS:
            continue
        texts = [t for _, t, _ in q["options"]]
        sig = _option_signature(texts)
        if sig in SHARED_OPTION_SET_ALLOWLIST:
            continue
        by_sig[sig].append(q["id"])
        sig_opts[sig] = texts
    findings = {}
    for sig, qids in by_sig.items():
        if len(qids) > 1 and _is_distinctive(sig_opts[sig]):
            for qid in qids:
                findings[qid] = sorted(qids)
    return findings


def detect_provenance_divergence(questions, min_overlap=0.30):
    """Signal (1): the numeric tokens the immutable judge_result rationales
    discuss (the ORIGINAL options) barely intersect the currently-stored
    option tokens -> options grafted after generation.

    Returns {qid: overlap_ratio} for items below ``min_overlap``.

    Restricted to ``mcq_multi``: there the judge emits separate verdicts for
    "the marked correct options are..." AND "the marked-wrong options are...",
    so the rationale enumerates the FULL original option set and the overlap is
    meaningful. For single-answer items the rationale typically names only the
    correct value (and Data-Interpretation answers are derived ratios/percents
    that never echo the raw table numbers), so the signal is unreliable there.
    """
    findings = {}
    for q in questions.values():
        if q["subtype"] != "mcq_multi":
            continue
        if q["id"] in VERIFIED_NATIVE_QIDS:
            continue
        opts = q["options"]
        if not opts or len(opts) < 2:
            continue
        opt_nums = set()
        for _, text, _ in opts:
            opt_nums |= _nums(text)
        if len(opt_nums) < 2:
            continue  # prose options: out of scope for a numeric signal
        pj = q["provenance_json"]
        if not pj or "judge_result" not in pj:
            continue
        try:
            judge = json.loads(pj).get("judge_result", {})
        except Exception:
            continue
        # gather rationale prose from the judge verdicts + summary
        prose = [judge.get("rationale", "")]
        for v in judge.get("verdicts", []):
            prose.append(v.get("rationale", ""))
        prov_nums = _nums(" ".join(prose))
        if not prov_nums:
            continue
        present = sum(1 for n in opt_nums if n in prov_nums)
        overlap = present / max(1, len(opt_nums))
        if overlap < min_overlap:
            findings[q["id"]] = round(overlap, 3)
    return findings


def build_manifest(questions, shared, prov):
    suspects = sorted(set(shared) | set(prov))
    entries = []
    for qid in suspects:
        q = questions[qid]
        reasons = []
        if qid in prov:
            reasons.append("provenance_divergence")
        if qid in shared:
            reasons.append("distinctive_shared_set")
        # mcq_multi grafts are high-confidence (provenance enumerates the full
        # original option set); other shared-set hits are flagged for review
        # rather than blind repair.
        confidence = "high" if q["subtype"] == "mcq_multi" else "review"
        entries.append(dict(
            qid=qid, source=q["source"], subtype=q["subtype"],
            status=q["status"], reasons=reasons, confidence=confidence,
            shares_options_with=[x for x in shared.get(qid, []) if x != qid],
            provenance_overlap=prov.get(qid),
        ))
    return entries


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/gre_mock.db")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--live-only", action="store_true")
    ap.add_argument("--min-overlap", type=float, default=0.30)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    questions = load_questions(conn, args.live_only)
    shared = detect_distinctive_shared_sets(questions)
    prov = detect_provenance_divergence(questions, args.min_overlap)
    entries = build_manifest(questions, shared, prov)
    conn.close()

    # Breakdown by source/subtype
    by_source = defaultdict(int)
    by_subtype = defaultdict(int)
    n_high = sum(1 for e in entries if e["confidence"] == "high")
    for e in entries:
        by_source[e["source"]] += 1
        by_subtype[e["subtype"]] += 1

    print(f"Option-graft audit on {args.db} (live_only={args.live_only})")
    print(f"  questions scanned: {len(questions)}")
    print(f"  graft suspects:    {len(entries)}  (high-confidence mcq_multi: {n_high}; review: {len(entries)-n_high})")
    print(f"    provenance_divergence: {len(prov)}   distinctive_shared_set: {len(shared)}")
    print(f"  by source:  {dict(by_source)}")
    print(f"  by subtype: {dict(by_subtype)}")

    if not args.summary:
        for e in entries:
            print(f"  - q{e['qid']} [{e['source']}/{e['subtype']}/{e['status']}] "
                  f"{','.join(e['reasons'])}"
                  + (f" shares={e['shares_options_with']}" if e['shares_options_with'] else "")
                  + (f" prov_overlap={e['provenance_overlap']}" if e['provenance_overlap'] is not None else ""))

    if args.export:
        os.makedirs("data/audits", exist_ok=True)
        stamp = datetime.date.today().isoformat()
        path = f"data/audits/option_graft_manifest_{stamp}.json"
        with open(path, "w") as f:
            json.dump(dict(
                generated=datetime.datetime.now().isoformat(timespec="seconds"),
                db=args.db, live_only=args.live_only,
                scanned=len(questions), suspects=len(entries),
                entries=entries,
            ), f, indent=2)
        print(f"  -> manifest written: {path}")

    return 1 if entries else 0


if __name__ == "__main__":
    sys.exit(main())
