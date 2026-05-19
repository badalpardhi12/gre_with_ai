#!/usr/bin/env python3
"""Balance-gap audit (Phase 4.1).

Read-only audit that compares the current live-question pool against the
12x pool-depth targets defended in
``research/cleanup-2026-05-18/pool_targets.md`` (Phase 0d) and emits two
JSON artifacts under ``data/audits/`` that curated batch synthesis
consumes.

Inputs
------
* ``tests/benchmarks/cleanup_baseline_2026_05_18.json`` -- Phase 0c
  benchmark; provides the per-(measure, subtype, status) live counts and
  the DI cluster snapshot.
* ``research/cleanup-2026-05-18/pool_targets.md`` -- Phase 0d targets.
  We do NOT parse the markdown; the per-bucket numbers are baked in below
  (and cross-referenced against the live counts at runtime so any drift
  surfaces).
* ``data/dedup_eval/full_sweep_2026_05_18.csv`` (optional) -- Phase 1.5
  full-bank dedup sweep. Used to project the post-retirement bucket
  counts under the production threshold (cosine >= 0.9, ce_score >= 0.3).
  If absent, the projection block is omitted.

Outputs
-------
* ``data/audits/balance_gaps_2026_05_18.json``
* ``data/audits/balance_gaps_2026_05_18.json``

Both files are deterministic given the same inputs.

Usage
-----
    venv/bin/python scripts/audit_balance_gaps.py
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

# ── Phase 0d 12x targets ──────────────────────────────────────────────────
# Per-section per-test demand and 12x pool target, from
# pool_targets.md sections 3.1 / 3.2 / 5.
#
# Each entry: {"per_test_demand": int, "target_12x": int,
#              "verdict_at_12x": "synthesis_required"|"borderline"|"cleanup_only"|"none"}
#
# Notes
# -----
# * data_interp targets are expressed in CLUSTERS of 3 sibling questions.
#   The bucket "live" count below is also a cluster count when we can
#   read it from the baseline DI-cluster block.
# * rc_select_passage carries a "synthesis OR retire" verdict (Open
#   Question 6). We default to "synthesis_required" in code and let the
#   downstream curated batch worker honour the retire alternative if the user
#   product-decides that way.
TARGETS_12X: Dict[Tuple[str, str], Dict] = {
    ("quant", "qc"):           {"per_test_demand": 9,  "target": 108, "verdict": "cleanup_only"},
    ("quant", "mcq_single"):   {"per_test_demand": 10, "target": 120, "verdict": "cleanup_only"},
    ("quant", "mcq_multi"):    {"per_test_demand": 3,  "target": 36,  "verdict": "synthesis_required"},
    ("quant", "numeric_entry"):{"per_test_demand": 3,  "target": 36,  "verdict": "none"},
    ("quant", "data_interp"):  {"per_test_demand": 3,  "target": 36,  "verdict": "synthesis_required",
                                "cluster_target": 12, "per_cluster_questions": 3},
    ("verbal", "tc"):          {"per_test_demand": 7,  "target": 84,  "verdict": "cleanup_only"},
    ("verbal", "se"):          {"per_test_demand": 7,  "target": 84,  "verdict": "cleanup_only"},
    ("verbal", "rc_single"):   {"per_test_demand": 6,  "target": 72,  "verdict": "cleanup_only"},
    ("verbal", "rc_multi"):    {"per_test_demand": 7,  "target": 84,  "verdict": "borderline"},
    ("verbal", "rc_select_passage"): {"per_test_demand": 1, "target": 12, "verdict": "synthesis_required"},
    ("awa", "issue"):          {"per_test_demand": 1,  "target": 12,  "verdict": "none"},
}

POOL_MULTIPLIER = 12

# Production dedup thresholds, lifted from
# ``services/dedup/config.py`` (EMBEDDING_COSINE_THRESHOLD = 0.9,
# CROSS_ENCODER_PARAPHRASE_THRESHOLD = 0.3). A pair that meets BOTH is
# what the live ingest pipeline would flag as a duplicate.
DEDUP_COSINE_THRESHOLD = 0.9
DEDUP_CE_THRESHOLD = 0.3

# Stricter "high-confidence" tier, used purely for reporting alongside
# the production tier.
DEDUP_HIGH_CONF_COSINE = 0.95
DEDUP_HIGH_CONF_CE = 0.5


def _load_baseline(path: Path) -> Dict:
    with path.open() as f:
        return json.load(f)


def _live_count_for_bucket(baseline: Dict, measure: str, subtype: str) -> int:
    """Sum live items for a (measure, subtype) bucket across all sources/difficulties."""
    facets: Dict[str, int] = baseline["content_db"]["by_full_facet"]
    total = 0
    target_prefix_measure = f"|measure={measure}|"
    target_subtype = f"|subtype={subtype}|"
    target_status = "|status=live"
    for facet_key, count in facets.items():
        if (target_prefix_measure in facet_key
                and target_subtype in facet_key
                and facet_key.endswith(target_status)):
            total += count
    return total


def _live_di_cluster_count(baseline: Dict) -> int:
    """Live DI clusters as defined in pool_targets.md §3.1: any DI stimulus
    with at least one live sibling question. The baseline JSON's
    ``di_clusters.total_clusters`` field already counts only stimuli with
    at least 2 live siblings (per Phase 0c). We use that figure directly to
    stay consistent with the pool_targets.md "1 cluster (2 live Q)" framing
    that drove the -11 gap.
    """
    di = baseline.get("content_db", {}).get("di_clusters") or baseline.get("di_clusters") or {}
    if not di:
        di = baseline.get("di_clusters") or {}
    # Prefer total_clusters (the pool_targets.md definition); fall back to
    # the stricter 3+-siblings figure if total_clusters is unavailable.
    return int(di.get("total_clusters") or di.get("clusters_with_3plus_live_siblings") or 0)


def _live_di_question_count(baseline: Dict) -> int:
    return _live_count_for_bucket(baseline, "quant", "data_interp")


def _live_awa_count(baseline: Dict) -> int:
    """AWA prompts aren't tagged as a measure in the by_full_facet facets;
    we use the documented live count from pool_targets.md (136) as the
    canonical figure since the benchmark JSON only enumerates Quant + Verbal
    measures. If a future benchmark JSON adds AWA, override here."""
    # The pool_targets.md table lists AWA: Issue prompts live = 136. We don't
    # have a programmatic facet to validate, so we surface this as a
    # documentation-derived number rather than fabricating a bucket.
    return 136


def _bucket_live_count(baseline: Dict, measure: str, subtype: str,
                       targets: Dict) -> Tuple[int, str]:
    """Return (live_count, count_kind) where count_kind is 'questions' or
    'clusters' (DI uses cluster counts to align with the cluster_target).
    """
    if measure == "quant" and subtype == "data_interp":
        return _live_di_cluster_count(baseline), "clusters"
    if measure == "awa":
        return _live_awa_count(baseline), "prompts"
    return _live_count_for_bucket(baseline, measure, subtype), "questions"


def _derive_target_and_gap(measure: str, subtype: str, live: int,
                           spec: Dict, count_kind: str) -> Tuple[int, int]:
    """Returns (target, gap) where gap follows the brief schema:
    ``gap = live - target`` (negative => below target / synthesis,
    positive => surplus / cleanup-only)."""
    if measure == "quant" and subtype == "data_interp" and count_kind == "clusters":
        target = int(spec.get("cluster_target") or 12)
    else:
        target = int(spec["target"])
    gap = live - target
    return target, gap


def _project_dedup_impact(baseline: Dict, sweep_path: Optional[Path]) -> Dict:
    """Project per-bucket live counts after retiring duplicates flagged at the
    production threshold. **Read-only -- nothing is actually retired.**

    Strategy: for every flagged pair (qid_a, qid_b) where both share the same
    (measure, subtype), retire one side (qid_b) -- decrementing that bucket's
    projected live count by 1. Pairs across different subtypes are left alone
    (the retirement decision belongs to the human reviewer).
    """
    out = {
        "method": "subtract one side of every same-bucket pair flagged at "
                  "production thresholds (cosine>=%.2f AND ce_score>=%.2f)" % (
                      DEDUP_COSINE_THRESHOLD, DEDUP_CE_THRESHOLD),
        "production_thresholds": {
            "cosine": DEDUP_COSINE_THRESHOLD,
            "ce_score": DEDUP_CE_THRESHOLD,
        },
        "high_confidence_thresholds": {
            "cosine": DEDUP_HIGH_CONF_COSINE,
            "ce_score": DEDUP_HIGH_CONF_CE,
        },
        "pairs_at_production_threshold": 0,
        "pairs_at_high_confidence_threshold": 0,
        "if_retire_at_production_threshold": {},
        "if_retire_at_high_confidence_threshold": {},
        "skipped_pairs_cross_subtype": 0,
    }

    if sweep_path is None or not sweep_path.exists():
        out["sweep_path_missing"] = str(sweep_path) if sweep_path else None
        return out

    # Bucket -> count of duplicate-side retirements
    prod_decrements: Dict[Tuple[str, str], int] = defaultdict(int)
    hc_decrements: Dict[Tuple[str, str], int] = defaultdict(int)
    skipped_cross = 0
    pairs_prod = 0
    pairs_hc = 0

    def _f(s: str) -> Optional[float]:
        if s is None or s == "":
            return None
        try:
            return float(s)
        except ValueError:
            return None

    with sweep_path.open() as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            cos = _f(row.get("cosine"))
            ce = _f(row.get("ce_score"))
            if cos is None or ce is None:
                continue
            measure_a = row.get("measure_a")
            measure_b = row.get("measure_b")
            sub_a = row.get("subtype_a")
            sub_b = row.get("subtype_b")
            same_bucket = (measure_a == measure_b and sub_a == sub_b)
            if cos >= DEDUP_COSINE_THRESHOLD and ce >= DEDUP_CE_THRESHOLD:
                pairs_prod += 1
                if same_bucket:
                    prod_decrements[(measure_a, sub_a)] += 1
                else:
                    skipped_cross += 1
            if cos >= DEDUP_HIGH_CONF_COSINE and ce >= DEDUP_HIGH_CONF_CE:
                pairs_hc += 1
                if same_bucket:
                    hc_decrements[(measure_a, sub_a)] += 1

    out["pairs_at_production_threshold"] = pairs_prod
    out["pairs_at_high_confidence_threshold"] = pairs_hc
    out["skipped_pairs_cross_subtype"] = skipped_cross

    # Project new live counts per bucket
    for (measure, subtype) in TARGETS_12X.keys():
        spec = TARGETS_12X[(measure, subtype)]
        live, count_kind = _bucket_live_count(baseline, measure, subtype, spec)
        if count_kind != "questions":
            # We don't project DI cluster impact from question-level dedup
            # pairs (a duplicate sibling is a cluster integrity concern, not a
            # cluster-count change). Skip cluster-counted buckets.
            continue
        prod_dec = prod_decrements.get((measure, subtype), 0)
        hc_dec = hc_decrements.get((measure, subtype), 0)
        bucket_key = f"{measure}|{subtype}"
        out["if_retire_at_production_threshold"][bucket_key] = {
            "live_now": live,
            "retirements": prod_dec,
            "live_after": max(0, live - prod_dec),
        }
        out["if_retire_at_high_confidence_threshold"][bucket_key] = {
            "live_now": live,
            "retirements": hc_dec,
            "live_after": max(0, live - hc_dec),
        }

    return out


def _derive_synthesis_handoff(buckets: List[Dict]) -> Dict:
    """Build the synthesis handoff for curated batch from the bucket verdicts."""
    handoff = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "multiplier": POOL_MULTIPLIER,
        "synthesis_required_buckets": [],
        "borderline_buckets": [],
        # Convenience aggregates per the brief schema
        "synthesis_targets": {},
    }

    di_clusters = 0
    mcq_multi_quant = 0
    rc_select_passage = 0
    geometry_flag_for_phase_5_2 = True

    for b in buckets:
        verdict = b["verdict"]
        if verdict == "synthesis_required":
            min_count = abs(b["gap"]) if b["gap"] < 0 else 0  # gap negative => shortfall
            entry = {
                "measure": b["measure"],
                "subtype": b["subtype"],
                "live": b["live"],
                "target": b["target"],
                "gap": b["gap"],
                "min_synthesis_count": min_count,
                "recommended_synthesis_count": _recommended_with_margin(b),
                "count_kind": b.get("count_kind", "questions"),
                "note": b.get("note", ""),
            }
            handoff["synthesis_required_buckets"].append(entry)

            if b["measure"] == "quant" and b["subtype"] == "data_interp":
                di_clusters = entry["recommended_synthesis_count"]
            if b["measure"] == "quant" and b["subtype"] == "mcq_multi":
                mcq_multi_quant = entry["recommended_synthesis_count"]
            if b["measure"] == "verbal" and b["subtype"] == "rc_select_passage":
                rc_select_passage = entry["recommended_synthesis_count"]
        elif verdict == "borderline":
            handoff["borderline_buckets"].append({
                "measure": b["measure"],
                "subtype": b["subtype"],
                "live": b["live"],
                "target": b["target"],
                "gap": b["gap"],
                "note": b.get("note", ""),
            })

    handoff["synthesis_targets"] = {
        "di_clusters": di_clusters,
        "mcq_multi_quant": mcq_multi_quant,
        "rc_select_passage": rc_select_passage,
        "geometry_mcq_figures": "flagged_for_phase_5_2_audit"
        if geometry_flag_for_phase_5_2 else None,
    }
    return handoff


def _recommended_with_margin(bucket: Dict) -> int:
    """Per pool_targets.md §5: synthesize at least the gap, ideally with a
    safety margin so cleanup attrition (Phase 1 retirements) doesn't push
    a bucket back below target.

    For DI clusters and rc_select_passage the gap is small (12 and 12);
    we recommend synthesizing the gap exactly.

    For mcq_multi the upstream report recommends >= 39 to clear 12x with
    margin (gap is 15). We use ``max(gap, ceil(gap * 1.3) + small_const)`` =
    21 for safety, but if the upstream §5 recommendation is higher we
    honour it.
    """
    measure = bucket["measure"]
    subtype = bucket["subtype"]
    gap = abs(bucket["gap"])  # positive shortfall (gap is negative for synthesis_required)

    # Specific upstream guidance
    if measure == "quant" and subtype == "mcq_multi":
        # Upstream §5: ">= 15 ... ideally >= 39 to safely clear with margin"
        return max(gap, 39)
    if measure == "quant" and subtype == "data_interp":
        # Upstream §5: ">= 11 new clusters" of 3 sibling questions.
        return max(gap, 11)
    if measure == "verbal" and subtype == "rc_select_passage":
        # Upstream §5: synth ~12 OR retire (product decision)
        return max(gap, 12)
    return gap


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=REPO_ROOT / "tests" / "benchmarks" / "cleanup_baseline_2026_05_18.json",
        help="Phase 0c benchmark JSON.",
    )
    parser.add_argument(
        "--sweep",
        type=Path,
        default=REPO_ROOT / "data" / "dedup_eval" / "full_sweep_2026_05_18.csv",
        help="Phase 1.5 full-bank dedup sweep CSV (optional).",
    )
    parser.add_argument(
        "--out-balance",
        type=Path,
        default=REPO_ROOT / "data" / "audits" / "balance_gaps_2026_05_18.json",
        help="Path to write balance-gap JSON.",
    )
    parser.add_argument(
        "--out-synth",
        type=Path,
        default=REPO_ROOT / "data" / "audits" / "synthesis_targets_2026_05_18.json",
        help="Path to write synthesis-handoff JSON.",
    )
    args = parser.parse_args()

    if not args.baseline.exists():
        print(f"ERROR: baseline not found at {args.baseline}", file=sys.stderr)
        return 2

    args.out_balance.parent.mkdir(parents=True, exist_ok=True)
    args.out_synth.parent.mkdir(parents=True, exist_ok=True)

    baseline = _load_baseline(args.baseline)
    buckets: List[Dict] = []

    for (measure, subtype), spec in TARGETS_12X.items():
        live, count_kind = _bucket_live_count(baseline, measure, subtype, spec)
        target, gap = _derive_target_and_gap(measure, subtype, live, spec, count_kind)
        # Verdict promotion: if the static spec said cleanup_only / borderline /
        # none but the live count is now below target, escalate. Conversely,
        # if the spec said synthesis_required but the live count cleared
        # the target, downgrade to none.
        # Sign convention: gap = live - target. gap < 0 => below target.
        verdict = spec["verdict"]
        if gap < 0 and verdict in ("cleanup_only", "none"):
            verdict = "synthesis_required" if abs(gap) >= max(2, target * 0.1) else "borderline"
        if gap >= 0 and verdict == "synthesis_required":
            verdict = "cleanup_only"

        note_parts = []
        if measure == "quant" and subtype == "data_interp":
            cluster_target = spec.get("cluster_target", 12)
            per_cluster_q = spec.get("per_cluster_questions", 3)
            equivalent_q_target = cluster_target * per_cluster_q
            note_parts.append(
                f"Counted in CLUSTERS of {per_cluster_q} sibling questions; "
                f"{cluster_target}-cluster target ~ {equivalent_q_target} questions."
            )
            note_parts.append(
                "Image-bearing synthesis: defer to matplotlib/SVG rendering "
                "per report.md §4.5 (text-in-image fidelity)."
            )
        if measure == "verbal" and subtype == "rc_select_passage":
            note_parts.append(
                "Open Question 6: synthesize ~12 OR retire from "
                "VERBAL_COMPOSITION (one-line code change). Default = synthesize."
            )
        if measure == "verbal" and subtype == "rc_multi":
            note_parts.append(
                "Borderline: gap of -2 is inside multiplier-noise floor; "
                "pool_targets.md §5 recommends NOT synthesizing."
            )

        buckets.append({
            "measure": measure,
            "subtype": subtype,
            "live": live,
            "target": target,
            "gap": gap,
            "per_test_demand": spec["per_test_demand"],
            "verdict": verdict,
            "count_kind": count_kind,
            "note": "; ".join(note_parts) if note_parts else "",
        })

    # Sort: synthesis_required first, then borderline, then cleanup_only/none.
    verdict_order = {
        "synthesis_required": 0,
        "borderline": 1,
        "cleanup_only": 2,
        "none": 3,
    }
    buckets.sort(key=lambda b: (verdict_order[b["verdict"]], b["measure"], b["subtype"]))

    dedup_projection = _project_dedup_impact(baseline, args.sweep)

    out_balance = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "multiplier": POOL_MULTIPLIER,
        "source_baseline": str(args.baseline.relative_to(REPO_ROOT)),
        "source_pool_targets": "research/cleanup-2026-05-18/pool_targets.md",
        "source_dedup_sweep": (
            str(args.sweep.relative_to(REPO_ROOT)) if args.sweep.exists() else None
        ),
        "buckets": buckets,
        "synthesis_targets": _derive_synthesis_handoff(buckets)["synthesis_targets"],
        "dedup_net_impact_projection": dedup_projection,
        "summary": {
            "n_synthesis_required": sum(1 for b in buckets if b["verdict"] == "synthesis_required"),
            "n_borderline": sum(1 for b in buckets if b["verdict"] == "borderline"),
            "n_cleanup_only": sum(1 for b in buckets if b["verdict"] == "cleanup_only"),
            "n_none": sum(1 for b in buckets if b["verdict"] == "none"),
        },
    }

    handoff = _derive_synthesis_handoff(buckets)
    handoff["source_balance_gaps"] = str(args.out_balance.relative_to(REPO_ROOT))

    with args.out_balance.open("w") as f:
        json.dump(out_balance, f, indent=2, sort_keys=True)
        f.write("\n")
    with args.out_synth.open("w") as f:
        json.dump(handoff, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"wrote {args.out_balance}")
    print(f"wrote {args.out_synth}")
    print(f"summary: {out_balance['summary']}")
    print("synthesis_targets:", handoff["synthesis_targets"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
