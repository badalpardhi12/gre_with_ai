#!/usr/bin/env python3
"""Tune embedding cosine + cross-encoder thresholds against the held-out set.

The held-out set is ``data/dedup_eval/labeled_pairs_2026_05_18.csv`` (244
rows; 42 Yes / 173 No / 29 Maybe; 60 of those Nos are
``bucket=paraphrase_candidate`` — high cosine + low jaccard).

Sweep:

    cosine ∈ [0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
    cross-encoder ∈ [0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]

The cross-encoder grid is on the **stsb-roberta-large [0,1]** sigmoid
scale (NOT the [0, 5] STS-B regression scale). The model card promises
[0, 5] but the sentence-transformers checkpoint applies a sigmoid
internally so we observe scores in [0, 1]. See worker P1.3's notes.

The two-stage decision rule under evaluation is:

    classify Yes  IFF  cos(a,b) >= cosine_threshold AND
                       cross_encoder(a,b) >= ce_threshold

(Or equivalently: stage-1 decides 'candidate', stage-2 decides 'paraphrase'.)

For each (cos, ce) cell we compute precision / recall / F1 against the
Yes-vs-No subset (Maybe is dropped — labeller best practice). We pick
the cell with the highest F1, **subject to the constraint** that >=90%
of the 60 ``paraphrase_candidate``-No pairs are correctly rejected.

Per CAVEAT FROM P1.1: zero confirmed paraphrase clones with j<0.4 exist
in this corpus, so the headline metric is the paraphrase_candidate
reject rate, not F1 on a synthetic paraphrase subset.

The chosen thresholds are written back into ``services/dedup/config.py``
(in-place text edit on the two assignments).

Python 3.9 compatible.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from models.database import Question, db, Stimulus  # noqa: E402
from services.dedup import embedding_stage  # noqa: E402
from services.dedup.config import (  # noqa: E402
    CROSS_ENCODER_MODEL_NAME,
    EMBEDDING_MODEL_NAME,
)


LABELED_CSV = REPO_ROOT / "data" / "dedup_eval" / "labeled_pairs_2026_05_18.csv"
DEFAULT_CACHE = REPO_ROOT / "data" / "dedup_eval" / "tune_cache_2026_05_18.json"
CONFIG_FILE = REPO_ROOT / "services" / "dedup" / "config.py"

COSINE_GRID = [0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
CE_GRID = [0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]


def load_labeled_pairs() -> List[Dict[str, str]]:
    """Return all rows from the labelled pair CSV as dicts."""
    with open(LABELED_CSV, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def fetch_question_text_map(qids: List[int]) -> Dict[int, str]:
    """Render each qid's full question (stim+stem+options) to one string.

    Mirrors :func:`services.dedup.embedding_stage.question_to_text` but
    operates on Peewee rows directly. We keep the two paths in sync by
    delegating: we hand-construct a tiny shim object the helper expects.
    """
    db.connect(reuse_if_open=True)
    out: Dict[int, str] = {}
    try:
        for q in Question.select().where(Question.id.in_(qids)):
            text = embedding_stage.question_to_text(q)
            out[int(q.id)] = text
    finally:
        db.close()
    missing = [q for q in qids if q not in out]
    if missing:
        # Treat missing as empty so the sweep can still run.
        for q in missing:
            out[q] = ""
    return out


def compute_pair_features(
    rows: List[Dict[str, str]],
    bi_encoder_name: str,
    ce_judge: embedding_stage.CrossEncoderJudge,
    cache_path: Path,
) -> List[Dict[str, object]]:
    """For each labelled row, compute ``cos`` and ``ce_score``.

    Caches per-pair_id features at ``cache_path`` so re-runs of the
    sweep don't re-encode.
    """
    cache: Dict[str, Dict[str, float]] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except (ValueError, OSError):
            cache = {}

    qids = sorted({int(r["qid_a"]) for r in rows} | {int(r["qid_b"]) for r in rows})
    text_map = fetch_question_text_map(qids)

    # Score every pair through the bi-encoder (cosine).
    print(
        f"[tune] {len(rows)} pairs, {len(qids)} unique qids — "
        f"loading bi-encoder {bi_encoder_name}",
        flush=True,
    )
    bi_t0 = time.time()
    model = embedding_stage._get_bi_encoder(bi_encoder_name)
    print(f"[tune] bi-encoder loaded in {time.time() - bi_t0:.1f}s", flush=True)

    # Encode each unique qid once, then look up.
    encode_t0 = time.time()
    qid_order = list(qids)
    text_order = [text_map[q] for q in qid_order]
    embeds = model.encode(
        text_order,
        batch_size=32,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    print(f"[tune] encoded {len(qid_order)} questions in {time.time() - encode_t0:.1f}s", flush=True)
    qid_to_idx = {q: i for i, q in enumerate(qid_order)}

    # Pre-compute cosines.
    rows_out: List[Dict[str, object]] = []
    pairs_to_judge: List[Tuple[str, str, str]] = []  # (pair_id, text_a, text_b)
    for r in rows:
        pid = r["pair_id"]
        qa = int(r["qid_a"])
        qb = int(r["qid_b"])
        ea = embeds[qid_to_idx[qa]]
        eb = embeds[qid_to_idx[qb]]
        cos = float(np.dot(ea, eb))
        cached = cache.get(pid, {})
        ce_score = cached.get("ce_score")
        rows_out.append(
            {
                "pair_id": pid,
                "qid_a": qa,
                "qid_b": qb,
                "final_label": r["final_label"],
                "bucket": r["bucket"],
                "sampling_strata": r["sampling_strata"],
                "cos": cos,
                "ce_score": ce_score,
            }
        )
        if ce_score is None:
            pairs_to_judge.append((pid, text_map[qa], text_map[qb]))

    # Run cross-encoder for any uncached rows.
    if pairs_to_judge:
        print(
            f"[tune] cross-encoder judging {len(pairs_to_judge)} new pairs "
            f"({CROSS_ENCODER_MODEL_NAME})…",
            flush=True,
        )
        ce_t0 = time.time()
        scores = ce_judge.judge_pairs_batch(
            [(t_a, t_b) for (_, t_a, t_b) in pairs_to_judge],
            batch_size=8,
        )
        print(
            f"[tune] cross-encoder done in {time.time() - ce_t0:.1f}s "
            f"({(time.time() - ce_t0) / max(len(pairs_to_judge), 1) * 1000:.0f} ms/pair)",
            flush=True,
        )
        score_by_pid = dict(zip([p for (p, _, _) in pairs_to_judge], scores))
        # Update both cache and rows_out.
        for r in rows_out:
            if r["ce_score"] is None and r["pair_id"] in score_by_pid:
                r["ce_score"] = float(score_by_pid[r["pair_id"]])
                cache[r["pair_id"]] = {
                    "cos": r["cos"],
                    "ce_score": r["ce_score"],
                }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2))
    return rows_out


def sweep(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Run the cosine × CE-threshold grid and return per-cell metrics."""
    yes_no = [r for r in rows if r["final_label"] in ("Yes", "No")]
    para_no = [r for r in yes_no if r["final_label"] == "No" and r["bucket"] == "paraphrase_candidate"]

    table: List[Dict[str, object]] = []
    for cos_t in COSINE_GRID:
        for ce_t in CE_GRID:
            tp = fp = fn = tn = 0
            for r in yes_no:
                pred = (r["cos"] >= cos_t) and (r["ce_score"] is not None and r["ce_score"] >= ce_t)
                truth = r["final_label"] == "Yes"
                if pred and truth:
                    tp += 1
                elif pred and not truth:
                    fp += 1
                elif (not pred) and truth:
                    fn += 1
                else:
                    tn += 1
            precision = tp / (tp + fp) if (tp + fp) else 0.0
            recall = tp / (tp + fn) if (tp + fn) else 0.0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
            # Reject rate over paraphrase_candidate-No.
            if para_no:
                para_rejects = sum(
                    1
                    for r in para_no
                    if not ((r["cos"] >= cos_t) and (r["ce_score"] is not None and r["ce_score"] >= ce_t))
                )
                reject_rate = para_rejects / len(para_no)
            else:
                reject_rate = 1.0
            table.append(
                {
                    "cos_threshold": cos_t,
                    "ce_threshold": ce_t,
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                    "tn": tn,
                    "precision": precision,
                    "recall": recall,
                    "f1": f1,
                    "para_no_total": len(para_no),
                    "para_no_rejects": int(reject_rate * len(para_no)) if para_no else 0,
                    "para_no_reject_rate": reject_rate,
                }
            )
    return table


def pick_best(table: List[Dict[str, object]], min_reject_rate: float = 0.90) -> Optional[Dict[str, object]]:
    """Pick the (cos, ce) cell with the highest F1 such that the
    paraphrase_candidate-No reject rate is at least ``min_reject_rate``.

    Tie-breaks (in order):
        1. Higher reject rate wins (more conservative on paraphrase Nos).
        2. Lower CE threshold wins (still meets safety, but catches more true-Yes).
        3. Lower cos threshold wins (same reasoning).
    """
    eligible = [c for c in table if c["para_no_reject_rate"] >= min_reject_rate]
    if not eligible:
        return None
    eligible.sort(
        key=lambda c: (
            -c["f1"],
            -c["para_no_reject_rate"],
            c["ce_threshold"],
            c["cos_threshold"],
        )
    )
    return eligible[0]


def write_thresholds_to_config(cos_t: float, ce_t: float) -> None:
    """In-place edit the two ``=`` assignments + the version tuple in config.py.

    Idempotent: a no-op edit (e.g. when the chosen thresholds match the
    persisted ones already) is silently OK; only a failed regex match
    raises.
    """
    text = CONFIG_FILE.read_text()

    cos_pattern = r"(EMBEDDING_COSINE_THRESHOLD: float = )[\d.]+"
    ce_pattern = r"(CROSS_ENCODER_PARAPHRASE_THRESHOLD: float = )[\d.]+"

    if not re.search(cos_pattern, text):
        raise RuntimeError(
            "Could not find EMBEDDING_COSINE_THRESHOLD assignment in config.py."
        )
    if not re.search(ce_pattern, text):
        raise RuntimeError(
            "Could not find CROSS_ENCODER_PARAPHRASE_THRESHOLD assignment in config.py."
        )

    new_text = re.sub(cos_pattern, f"\\g<1>{cos_t}", text)
    new_text = re.sub(ce_pattern, f"\\g<1>{ce_t}", new_text)
    if new_text != text:
        CONFIG_FILE.write_text(new_text)


def print_table(table: List[Dict[str, object]]) -> None:
    """Pretty-print the sweep table to stdout."""
    header = (
        "  cos    ce  | TP  FP  FN  TN |  P     R    F1  | paraNo reject"
    )
    print(header)
    print("-" * len(header))
    for c in table:
        print(
            f"  {c['cos_threshold']:.2f}  {c['ce_threshold']:.1f} | "
            f"{c['tp']:>2}  {c['fp']:>2}  {c['fn']:>2}  {c['tn']:>2} | "
            f"{c['precision']:.2f}  {c['recall']:.2f}  {c['f1']:.2f}  | "
            f"{c['para_no_rejects']}/{c['para_no_total']} = {c['para_no_reject_rate']:.2%}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                        help=f"Per-pair score cache (default: {DEFAULT_CACHE})")
    parser.add_argument("--bi-encoder", type=str, default=EMBEDDING_MODEL_NAME)
    parser.add_argument("--cross-encoder", type=str, default=CROSS_ENCODER_MODEL_NAME)
    parser.add_argument("--no-write", action="store_true",
                        help="Don't update services/dedup/config.py with the chosen thresholds.")
    parser.add_argument("--min-reject-rate", type=float, default=0.90)
    args = parser.parse_args()

    rows = load_labeled_pairs()
    print(f"[tune] loaded {len(rows)} labelled pairs from {LABELED_CSV}")

    judge = embedding_stage.CrossEncoderJudge(args.cross_encoder)
    scored = compute_pair_features(rows, args.bi_encoder, judge, args.cache)

    # Persist a flat CSV next to the cache for downstream debugging.
    pair_csv = args.cache.with_suffix(".pairs.csv")
    with open(pair_csv, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "pair_id", "qid_a", "qid_b", "final_label",
                "bucket", "sampling_strata", "cos", "ce_score",
            ],
        )
        writer.writeheader()
        for r in scored:
            writer.writerow(r)
    print(f"[tune] per-pair features → {pair_csv}")

    table = sweep(scored)
    print_table(table)

    best = pick_best(table, min_reject_rate=args.min_reject_rate)
    if best is None:
        print(
            f"[tune] NO cell meets paraphrase_candidate-No reject rate >= "
            f"{args.min_reject_rate:.0%}. Falling back to highest reject rate.",
            file=sys.stderr,
        )
        # Fall back to whichever cell has the strongest reject rate, then F1.
        table.sort(
            key=lambda c: (-c["para_no_reject_rate"], -c["f1"], c["ce_threshold"], c["cos_threshold"])
        )
        best = table[0]

    print()
    print(
        "[tune] CHOSEN cell: "
        f"cos={best['cos_threshold']:.2f}  ce={best['ce_threshold']:.1f}  "
        f"F1={best['f1']:.3f}  P={best['precision']:.3f}  R={best['recall']:.3f}  "
        f"para_no_reject={best['para_no_reject_rate']:.2%}  "
        f"({best['para_no_rejects']}/{best['para_no_total']})"
    )

    if not args.no_write:
        write_thresholds_to_config(best["cos_threshold"], best["ce_threshold"])
        print(f"[tune] wrote thresholds to {CONFIG_FILE}")

    yes_no_count = sum(1 for r in scored if r["final_label"] in ("Yes", "No"))
    print(
        f"Embedding+CE stage F1={best['f1']:.3f} at "
        f"cosine={best['cos_threshold']:.2f}, CE_threshold={best['ce_threshold']:.1f} "
        f"on {yes_no_count}-pair held-out set; paraphrase_candidate-No reject "
        f"rate={best['para_no_reject_rate']:.1%}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
