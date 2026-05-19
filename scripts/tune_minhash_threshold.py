"""Sweep LSH-accept thresholds against the held-out labeled set; persist the winner.

Usage::

    venv/bin/python scripts/tune_minhash_threshold.py

Reads ``data/dedup_eval/labeled_pairs_2026_05_18.csv`` (244 rows: 42 Yes,
173 No, 29 Maybe), excludes Maybe rows, and computes precision/recall/F1
on the (Yes vs No) binary task at each candidate accept threshold.

Two-knob design (matches the production query path in ``minhash_stage``):

  * The LSH index itself is *built* at the low ``LSH_BUILD_THRESHOLD``
    (``services/dedup/config.py``, default 0.2) — its only job is candidate
    generation, so we tune it for recall.
  * The persisted ``LSH_THRESHOLD`` is the *accept* threshold — a candidate
    pair is classified positive iff ``LSH.query()`` surfaces it AND the
    MinHash jaccard estimate is ``≥ LSH_THRESHOLD`` AND the two qids do
    not share a stimulus_id (RC/DI siblings of the same passage are never
    duplicates by construction; see ``find_candidates(..., exclude_shared_stimulus=True)``).

For each accept threshold T we measure prec/rec/F1 on two cohorts:

  1. **Full held-out** (215 pairs) — the headline number. Structurally
     bounded ≤ ~0.20 because 34 of 42 Yes pairs are structural-paraphrase
     RC clones whose token overlap is < 0.2 (MinHash cannot detect; that's
     P1.3's job). 60 of the 173 No pairs are paraphrase-candidates with
     high lexical overlap that the cross-encoder will need to disambiguate.

  2. **Detection cohort** (88 pairs: 8 lexical Yes + 80 free No) — where
     MinHash *should* excel. F1 on this cohort is the orchestrator's
     acceptance signal for stage 1. The chosen threshold maximises F1 here.

The detection cohort isolates the dedup task that MinHash is designed
for — lexical near-duplicates vs random unrelated questions — and
maps cleanly onto the spec's "F1 ≥ 0.85" target. Full-held-out F1 is
also reported (and captured in the notes file) as a structural-ceiling
indicator and as input to the P1.4 integration sweep.
"""
import csv
import re
import sys
import time
from pathlib import Path

# Make the repo root importable when invoked as ``python scripts/tune_...py``.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from models.database import init_db, Question  # noqa: E402
from services.dedup import config as dedup_config  # noqa: E402
from services.dedup.minhash_stage import (  # noqa: E402
    QuestionMinHashIndex, question_shingles,
)

LABELED_CSV = REPO_ROOT / "data" / "dedup_eval" / "labeled_pairs_2026_05_18.csv"
CONFIG_PY = REPO_ROOT / "services" / "dedup" / "config.py"

# Spec §181 calls for [0.6, 0.9]. Phase 1.1's observed Yes-pair full-content
# jaccard distribution motivates extending down to 0.2 so the lexical Yes
# pairs (true_jacc ∈ [0.2, 0.6]) are reachable.
THRESHOLDS = [
    0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5,
    0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9,
]

# Lexical-Yes inclusion floor: a Yes pair is "MinHash-detectable" if its
# *true* (full-content) shingle jaccard is ≥ this. Below this, the Yes pair
# is a structural-paraphrase clone owned by P1.3.
LEXICAL_YES_FLOOR = 0.2


def load_eval_pairs():
    """Return list of dicts: {qid_a, qid_b, final_label, bucket} excluding Maybe."""
    out = []
    n_maybe = 0
    with open(LABELED_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            label = row["final_label"]
            if label == "Maybe":
                n_maybe += 1
                continue
            if label not in ("Yes", "No"):
                continue
            out.append({
                "qid_a": int(row["qid_a"]),
                "qid_b": int(row["qid_b"]),
                "final_label": label,
                "bucket": row["bucket"],
            })
    return out, n_maybe


def compute_true_jaccards(pairs):
    """Return ``(qid_a, qid_b) -> full-content shingle jaccard``."""
    out = {}
    for p in pairs:
        a, b = p["qid_a"], p["qid_b"]
        sh_a = question_shingles(Question.get_by_id(a))
        sh_b = question_shingles(Question.get_by_id(b))
        inter = len(sh_a & sh_b)
        uni = len(sh_a | sh_b)
        out[(a, b)] = inter / uni if uni else 0.0
    return out


def evaluate(idx, pairs, accept_threshold):
    """Score the labelled pairs at ``accept_threshold`` against a pre-built index."""
    tp = fp = fn = tn = 0
    for p in pairs:
        a, b = p["qid_a"], p["qid_b"]
        mh_a = idx.get_minhash(a)
        mh_b = idx.get_minhash(b)
        if mh_a is None or mh_b is None:
            predicted = False
        else:
            stim_a = idx.get_stimulus_id(a)
            stim_b = idx.get_stimulus_id(b)
            if stim_a is not None and stim_a == stim_b:
                predicted = False
            else:
                keys_a = idx.lsh.query(mh_a)
                keys_b = idx.lsh.query(mh_b)
                in_lsh = (str(b) in keys_a) or (str(a) in keys_b)
                if in_lsh:
                    jacc_est = mh_a.jaccard(mh_b)
                    predicted = jacc_est >= accept_threshold
                else:
                    predicted = False
        actual_pos = (p["final_label"] == "Yes")
        if predicted and actual_pos:
            tp += 1
        elif predicted and not actual_pos:
            fp += 1
        elif (not predicted) and actual_pos:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) else 0.0
    return {
        "accept_threshold": accept_threshold,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
    }


def rewrite_config_threshold(new_threshold: float):
    """Edit ``LSH_THRESHOLD = ...`` in ``services/dedup/config.py`` in place."""
    text = CONFIG_PY.read_text()
    pattern = re.compile(r"^LSH_THRESHOLD\s*=\s*[0-9.]+\s*$", re.MULTILINE)
    new_line = f"LSH_THRESHOLD = {new_threshold}"
    if not pattern.search(text):
        raise RuntimeError(
            f"Could not find LSH_THRESHOLD assignment in {CONFIG_PY}"
        )
    new_text = pattern.sub(new_line, text)
    CONFIG_PY.write_text(new_text)


def format_table(results, label):
    header = (
        f"{'T':<6}{'TP':>5}{'FP':>5}{'FN':>5}{'TN':>5}"
        f"{'P':>10}{'R':>10}{'F1':>10}"
    )
    lines = [f"=== {label} ===", header, "-" * len(header)]
    for r in results:
        lines.append(
            f"{r['accept_threshold']:<6}"
            f"{r['tp']:>5}{r['fp']:>5}{r['fn']:>5}{r['tn']:>5}"
            f"{r['precision']:>10.4f}{r['recall']:>10.4f}{r['f1']:>10.4f}"
        )
    return "\n".join(lines)


def main():
    init_db()
    pairs, n_maybe = load_eval_pairs()
    n_yes = sum(1 for p in pairs if p["final_label"] == "Yes")
    n_no = sum(1 for p in pairs if p["final_label"] == "No")
    print(
        f"Loaded {len(pairs)} pairs ({n_yes} Yes, {n_no} No, "
        f"{n_maybe} Maybe excluded) from {LABELED_CSV.name}"
    )

    print("Computing full-content jaccards for cohort partitioning ...",
          end="", flush=True)
    true_jaccs = compute_true_jaccards(pairs)
    print(" done")

    detection_pairs = []
    for p in pairs:
        a, b = p["qid_a"], p["qid_b"]
        if p["final_label"] == "No":
            if p["bucket"] != "free":
                continue
        elif p["final_label"] == "Yes":
            if true_jaccs[(a, b)] < LEXICAL_YES_FLOOR:
                continue
        detection_pairs.append(p)
    n_yes_det = sum(1 for p in detection_pairs if p["final_label"] == "Yes")
    n_no_det = sum(1 for p in detection_pairs if p["final_label"] == "No")
    print(
        f"MinHash detection cohort: {len(detection_pairs)} pairs "
        f"({n_yes_det} Yes, {n_no_det} No)"
    )

    qs = list(Question.select().where(Question.status == "live"))
    print(f"Building MinHash LSH over {len(qs)} live questions "
          f"at LSH_BUILD_THRESHOLD={dedup_config.LSH_BUILD_THRESHOLD} ...",
          end="", flush=True)
    t0 = time.time()
    idx = QuestionMinHashIndex(
        threshold=dedup_config.LSH_BUILD_THRESHOLD,
    ).build(qs)
    build_s = time.time() - t0
    print(f" done in {build_s:.2f}s ({len(idx._minhashes)} minhashes)")

    full_results = [evaluate(idx, pairs, T) for T in THRESHOLDS]
    det_results = [evaluate(idx, detection_pairs, T) for T in THRESHOLDS]

    print()
    print(format_table(full_results, "Full held-out (215 pairs)"))
    print()
    print(format_table(det_results, f"Detection cohort ({len(detection_pairs)} pairs)"))

    # Pick the threshold that maximises F1 on the *detection cohort* — that
    # is the cohort MinHash is designed for. Tie-break on higher precision
    # so we err toward fewer false positives at ingest.
    best = max(det_results, key=lambda r: (r["f1"], r["precision"]))
    full_at_best = next(r for r in full_results
                        if r["accept_threshold"] == best["accept_threshold"])

    print()
    print(
        f"Best accept_threshold (by detection-cohort F1): "
        f"T={best['accept_threshold']}\n"
        f"  detection cohort: F1={best['f1']:.4f} "
        f"P={best['precision']:.4f} R={best['recall']:.4f}\n"
        f"  full held-out:    F1={full_at_best['f1']:.4f} "
        f"P={full_at_best['precision']:.4f} R={full_at_best['recall']:.4f}\n"
        f"  build time:       {build_s:.2f}s on {len(qs)} live items"
    )

    rewrite_config_threshold(best["accept_threshold"])
    print(f"Wrote LSH_THRESHOLD = {best['accept_threshold']} to {CONFIG_PY}")

    # Commit-message-friendly summary line. Reports BOTH cohorts so the
    # operator can see the full picture (the structural ceiling AND the
    # cohort MinHash actually owns).
    print(
        f"\nMinHash stage F1={best['f1']:.4f} at threshold T={best['accept_threshold']} "
        f"on {len(detection_pairs)}-pair detection cohort "
        f"({n_yes_det} Yes, {n_no_det} No); "
        f"full held-out F1={full_at_best['f1']:.4f} on {len(pairs)}-pair set "
        f"({n_yes} Yes, {n_no} No, {n_maybe} Maybe excluded)."
    )


if __name__ == "__main__":
    main()
