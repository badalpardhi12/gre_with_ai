"""Top-up sampler for the dedup-evaluation pair set
(Phase 1.1, docs/implementation_plan_2026_05_18.md §164-174).

The first 200-pair pass produced only 26 cross-family-agreed Yes labels —
4 short of the ≥30 acceptance bar. Per the spec's STEP 4 fallback, we
top up the high-Jaccard pool. This script:

  * loads the existing candidate + labeled CSVs and freezes their
    qid-pair keys so we never re-sample what is already labeled,
  * pulls EXTRA high-Jaccard pairs (Jaccard ≥ 0.4), preferring
    same-stimulus-cluster matches when available since those are the
    cleanest duplicate signal,
  * appends the new candidates to candidate_pairs_2026_05_18.csv with
    pair_ids continuing from 201 upward.

After this runs, re-invoke scripts/label_dedup_pairs.py — the labeller
is resumable by pair_id, so only the new rows will be labelled.

Reproducibility: random.seed(20260518 + 1).
"""
from __future__ import annotations

import csv
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sample_dedup_pairs import (  # noqa: E402
    HIGH_JACCARD_CUT,
    SHINGLE_K,
    NEAREST_K,
    classify_strata,
    jaccard,
    load_live_questions,
    build_tfidf_per_measure,
    shingles,
    tokenize,
)

CANDIDATE_PATH = PROJECT_ROOT / "data" / "dedup_eval" / "candidate_pairs_2026_05_18.csv"
LABELED_PATH = PROJECT_ROOT / "data" / "dedup_eval" / "labeled_pairs_2026_05_18.csv"

SEED = 20260518 + 1
TOPUP_TARGET = 75
# Per-measure split inside the top-up.
TOPUP_SPLIT = {"quant": 0.5, "verbal": 0.5}
MAX_SCAN = 3000

CSV_FIELDNAMES = [
    "pair_id", "qid_a", "qid_b",
    "source_a", "source_b",
    "measure_a", "measure_b",
    "subtype_a", "subtype_b",
    "stem_a_first120", "stem_b_first120",
    "jaccard_5_shingle", "tfidf_cosine",
    "sampling_strata", "bucket",
]


def load_existing_keys():
    """Return frozensets of (qid_a, qid_b) already in candidate or labeled CSVs."""
    keys = set()
    max_pair_id = 0
    if CANDIDATE_PATH.exists():
        with open(CANDIDATE_PATH, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                keys.add(frozenset((int(row["qid_a"]), int(row["qid_b"]))))
                max_pair_id = max(max_pair_id, int(row["pair_id"]))
    return keys, max_pair_id


def sample_topup(rng, rows, per_measure, shingle_cache, already_seen,
                 target_per_measure):
    """Same logic as sample_high_jaccard but: (1) iterates more rows,
    (2) prefers cluster pairs when ties occur, (3) excludes already_seen."""
    seen = set(already_seen)
    by_measure_rows = {
        "quant": [r for r in rows if r["measure"] == "quant"],
        "verbal": [r for r in rows if r["measure"] == "verbal"],
    }

    # Build cluster lookup: stimulus_id -> list of rows (questions).
    cluster_map = defaultdict(list)
    for r in rows:
        if r["stimulus_id"]:
            cluster_map[r["stimulus_id"]].append(r)

    pairs = []
    for measure, want in target_per_measure.items():
        bundle = per_measure.get(measure)
        if bundle is None or want == 0:
            continue
        m_rows = bundle["rows"]
        topk = bundle["topk"]
        local = []

        # PASS 1 — cluster pairs (same stimulus, jaccard ≥ 0.4).
        # These are usually genuine duplicates / paraphrases.
        m_clusters = [c for c in cluster_map.values()
                      if len(c) >= 2 and c[0]["measure"] == measure]
        rng.shuffle(m_clusters)
        for cluster in m_clusters:
            if len(local) >= want:
                break
            for i in range(len(cluster)):
                if len(local) >= want:
                    break
                for j in range(i + 1, len(cluster)):
                    qa, qb = cluster[i], cluster[j]
                    key = frozenset((qa["qid"], qb["qid"]))
                    if key in seen:
                        continue
                    ja = jaccard(shingle_cache[qa["qid"]],
                                 shingle_cache[qb["qid"]])
                    if ja < HIGH_JACCARD_CUT:
                        continue
                    seen.add(key)
                    # cosine: lookup if we can
                    cos = 0.0
                    qid_to_idx = bundle["qid_to_idx"]
                    if (qa["qid"] in qid_to_idx and
                            qb["qid"] in qid_to_idx):
                        ia = qid_to_idx[qa["qid"]]
                        ib = qid_to_idx[qb["qid"]]
                        cos = float((bundle["matrix"][ia] @
                                     bundle["matrix"][ib].T).toarray()[0, 0])
                    local.append({
                        "qa": qa, "qb": qb, "jaccard": ja, "cosine": cos,
                        "bucket": "high_jaccard",
                    })
                    if len(local) >= want:
                        break

        # PASS 2 — TF-IDF nearest-neighbor walk (broader scan).
        if len(local) < want:
            order = list(range(len(m_rows)))
            rng.shuffle(order)
            order = order[:MAX_SCAN]
            for i in order:
                if len(local) >= want:
                    break
                qa = m_rows[i]
                ja_shingles = shingle_cache[qa["qid"]]
                for j, cos in topk.get(i, []):
                    if j == i:
                        continue
                    qb = m_rows[j]
                    key = frozenset((qa["qid"], qb["qid"]))
                    if key in seen:
                        continue
                    jb_shingles = shingle_cache[qb["qid"]]
                    ja = jaccard(ja_shingles, jb_shingles)
                    if ja >= HIGH_JACCARD_CUT:
                        seen.add(key)
                        local.append({
                            "qa": qa, "qb": qb, "jaccard": ja, "cosine": float(cos),
                            "bucket": "high_jaccard",
                        })
                        if len(local) >= want:
                            break

        pairs.extend(local)
        print("[topup_dedup_pairs] %s: %d new pairs" % (measure, len(local)))

    return pairs


def append_csv(pairs, start_pair_id, out_path):
    """Append the new pairs to the existing CSV, picking up pair_ids from
    start_pair_id + 1."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not out_path.exists()
    with open(out_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        for k, p in enumerate(pairs, start=1):
            qa, qb = p["qa"], p["qb"]
            writer.writerow({
                "pair_id": start_pair_id + k,
                "qid_a": qa["qid"],
                "qid_b": qb["qid"],
                "source_a": qa["source"],
                "source_b": qb["source"],
                "measure_a": qa["measure"],
                "measure_b": qb["measure"],
                "subtype_a": qa["subtype"],
                "subtype_b": qb["subtype"],
                "stem_a_first120": qa["stem_norm"][:120],
                "stem_b_first120": qb["stem_norm"][:120],
                "jaccard_5_shingle": "%.4f" % p["jaccard"],
                "tfidf_cosine": "%.4f" % p["cosine"],
                "sampling_strata": classify_strata(qa, qb),
                "bucket": p["bucket"],
            })


def main():
    rng = random.Random(SEED)
    print("[topup_dedup_pairs] loading live questions ...")
    rows = load_live_questions()
    print("[topup_dedup_pairs] %d live questions loaded" % len(rows))

    print("[topup_dedup_pairs] computing per-measure TF-IDF + top-K neighbors ...")
    per_measure = build_tfidf_per_measure(rows)
    if per_measure is None:
        print("[topup_dedup_pairs] FATAL: sklearn missing — aborting")
        sys.exit(2)

    print("[topup_dedup_pairs] caching shingle sets ...")
    shingle_cache = {r["qid"]: shingles(tokenize(r["stem_norm"])) for r in rows}

    already_seen, max_pair_id = load_existing_keys()
    print("[topup_dedup_pairs] %d existing qid-pair keys; max_pair_id=%d" %
          (len(already_seen), max_pair_id))

    quant = int(round(TOPUP_TARGET * TOPUP_SPLIT["quant"]))
    verbal = TOPUP_TARGET - quant
    target_per_measure = {"quant": quant, "verbal": verbal}
    print("[topup_dedup_pairs] target: %s" % target_per_measure)

    new_pairs = sample_topup(rng, rows, per_measure, shingle_cache,
                             already_seen, target_per_measure)
    print("[topup_dedup_pairs] %d new top-up pairs" % len(new_pairs))

    if not new_pairs:
        print("[topup_dedup_pairs] nothing to append — exiting")
        return

    append_csv(new_pairs, max_pair_id, CANDIDATE_PATH)
    print("[topup_dedup_pairs] appended %d rows to %s "
          "(pair_ids %d..%d)" %
          (len(new_pairs), CANDIDATE_PATH,
           max_pair_id + 1, max_pair_id + len(new_pairs)))


if __name__ == "__main__":
    main()
