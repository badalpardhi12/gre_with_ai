#!/usr/bin/env python3
"""Phase 1.5 — Full-bank dedup dry-run sweep.

Runs the integrated two-stage dedup pipeline against every live question
pair, outputs the candidate-duplicate report at
``data/dedup_eval/full_sweep_2026_05_18.csv``.

NO deletions. NO status changes. Output is a flat CSV the user reviews
before any retirement decisions in Phase 1.6 (HUMAN GATE).

Acceptance criteria (per plan §229):
    - Sweep completes in <15 min on the live bank (~2,599 items).
    - Report is human-readable.

Output schema:
    qid_a, qid_b, source_a, source_b, measure_a, measure_b,
    subtype_a, subtype_b, jaccard_estimate, cosine, ce_score,
    flagged_by ∈ {minhash, embedding, both}, stem_a_first120, stem_b_first120
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from peewee import JOIN

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from models.database import Question, Stimulus, QuestionOption  # noqa: E402
from services.dedup import config as dedup_config  # noqa: E402
from services.dedup.embedding_stage import (  # noqa: E402
    CrossEncoderJudge,
    _get_bi_encoder,
    load_embeddings,
    question_to_text,
)
from services.dedup.minhash_stage import (  # noqa: E402
    QuestionMinHashIndex,
    minhash_for_shingles,
    shingles,
    tokenize,
)


OUTPUT_CSV = REPO_ROOT / "data" / "dedup_eval" / "full_sweep_2026_05_18.csv"


def _load_live_questions() -> List[Question]:
    """Pull every live question with its stimulus joined."""
    return list(
        Question.select(Question, Stimulus)
        .join(Stimulus, JOIN.LEFT_OUTER)
        .where(Question.status == "live")
        .order_by(Question.id)
    )


def _stem_first_120(q: Question) -> str:
    txt = (q.prompt or "").strip().replace("\n", " ")
    return txt[:120]


def _q_options(q: Question) -> List[str]:
    return [o.option_text or "" for o in q.options]


def _measure_of(q: Question) -> str:
    """Return 'quant'|'verbal'|'awa'|'unknown' from topic/subtype."""
    topic = (q.topic or "").lower()
    if topic.startswith("quant") or topic in {
        "arithmetic", "algebra", "geometry", "data_interp", "data_analysis",
    }:
        return "quant"
    if topic.startswith("verbal") or topic in {
        "rc", "tc", "se", "reading_comprehension",
    }:
        return "verbal"
    if topic.startswith("awa") or topic == "issue":
        return "awa"
    sub = (q.subtype or "").lower()
    if sub in {"qc", "mcq_single", "mcq_multi", "numeric_entry", "data_interp"}:
        return "quant"
    if sub in {"tc", "se", "rc_single", "rc_multi", "rc_select_passage"}:
        return "verbal"
    return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-pairs", type=int, default=0,
                        help="Cap candidate pairs (debugging). 0 = unbounded.")
    parser.add_argument("--skip-cross-encoder", action="store_true",
                        help="Skip CE re-rank (faster, less precise).")
    args = parser.parse_args()

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    print(f"[full_sweep] loading live questions ...", flush=True)
    t0 = time.time()
    questions = _load_live_questions()
    print(f"[full_sweep] loaded {len(questions)} live questions in {time.time()-t0:.1f}s",
          flush=True)

    qid_by_pos = {q.id: i for i, q in enumerate(questions)}
    questions_by_id = {q.id: q for q in questions}

    # ── Stage 1: MinHash ────────────────────────────────────────────────
    print(f"[full_sweep] building MinHash LSH index "
          f"(num_perm={dedup_config.LSH_NUM_PERM}, T={dedup_config.LSH_BUILD_THRESHOLD}) ...",
          flush=True)
    t1 = time.time()
    index = QuestionMinHashIndex(
        threshold=dedup_config.LSH_BUILD_THRESHOLD,
        num_perm=dedup_config.LSH_NUM_PERM,
        k=dedup_config.SHINGLE_SIZE,
    )
    index.build(questions)
    print(f"[full_sweep] MinHash index built in {time.time()-t1:.1f}s", flush=True)

    print(f"[full_sweep] running MinHash candidate scan ...", flush=True)
    t2 = time.time()
    minhash_pairs: Dict[Tuple[int, int], float] = {}
    for q in questions:
        cands = index.find_candidates(q, exclude_shared_stimulus=True)
        for other_qid, jaccard in cands:
            if jaccard < dedup_config.LSH_THRESHOLD:
                continue
            a, b = sorted([q.id, other_qid])
            if a == b:
                continue
            key = (a, b)
            if key not in minhash_pairs or jaccard > minhash_pairs[key]:
                minhash_pairs[key] = jaccard
    print(f"[full_sweep] MinHash pairs flagged: {len(minhash_pairs)} "
          f"in {time.time()-t2:.1f}s", flush=True)

    # ── Stage 2: Embedding ──────────────────────────────────────────────
    print(f"[full_sweep] loading persisted embeddings ...", flush=True)
    t3 = time.time()
    try:
        all_emb, qid_list = load_embeddings(REPO_ROOT / "data" / "dedup_eval" / "embeddings_2026_05_18.npy")
    except FileNotFoundError as e:
        print(f"[full_sweep] FATAL: embeddings npy missing: {e}", flush=True)
        return 2
    print(f"[full_sweep] embeddings loaded: shape={all_emb.shape} "
          f"in {time.time()-t3:.1f}s", flush=True)

    # Subset embeddings to questions that are still live (some persisted
    # qids may have been retired since the embedding run).
    live_set = {q.id for q in questions}
    keep_idx = [i for i, qid in enumerate(qid_list) if qid in live_set]
    emb_live = all_emb[keep_idx]
    qid_live = [qid_list[i] for i in keep_idx]
    qid_to_emb_idx = {qid: i for i, qid in enumerate(qid_live)}
    print(f"[full_sweep] embeddings subset to {len(qid_live)} live items",
          flush=True)

    # Cosine matrix (vectorized). For 2,599 items @ 768 dim this is
    # ~25 MB and a 1-2s GEMM.
    import numpy as np
    print(f"[full_sweep] computing cosine similarity matrix ...", flush=True)
    t4 = time.time()
    norms = np.linalg.norm(emb_live, axis=1, keepdims=True)
    emb_normed = emb_live / np.clip(norms, 1e-9, None)
    cos_mat = emb_normed @ emb_normed.T
    print(f"[full_sweep] cos matrix shape={cos_mat.shape} "
          f"in {time.time()-t4:.1f}s", flush=True)

    # Pairs with cosine >= threshold (excluding diagonal, lower-triangle only).
    cos_thresh = dedup_config.EMBEDDING_COSINE_THRESHOLD
    embedding_pairs: Dict[Tuple[int, int], float] = {}
    n = len(qid_live)
    for i in range(n):
        # Compare i to j > i only (upper triangle).
        cos_row = cos_mat[i, i + 1:]
        hits = np.where(cos_row >= cos_thresh)[0]
        for offset in hits:
            j = i + 1 + int(offset)
            qa, qb = qid_live[i], qid_live[j]
            a, b = sorted([qa, qb])
            embedding_pairs[(a, b)] = float(cos_row[offset])

    print(f"[full_sweep] embedding pairs flagged (cos≥{cos_thresh}): "
          f"{len(embedding_pairs)}", flush=True)

    # ── Stage 2 re-rank: cross-encoder ──────────────────────────────────
    ce_scores: Dict[Tuple[int, int], float] = {}
    if args.skip_cross_encoder or not embedding_pairs:
        print(f"[full_sweep] skipping cross-encoder re-rank "
              f"({'requested' if args.skip_cross_encoder else 'no pairs'})",
              flush=True)
    else:
        print(f"[full_sweep] running cross-encoder re-rank "
              f"on {len(embedding_pairs)} pairs ...", flush=True)
        t5 = time.time()
        ce = CrossEncoderJudge()
        pairs_list = sorted(embedding_pairs.keys())
        text_pairs: List[Tuple[str, str]] = []
        for a, b in pairs_list:
            qa = questions_by_id.get(a)
            qb = questions_by_id.get(b)
            if qa is None or qb is None:
                text_pairs.append(("", ""))
                continue
            text_pairs.append((question_to_text(qa), question_to_text(qb)))
        scores = ce.judge_pairs_batch(text_pairs)
        for (a, b), s in zip(pairs_list, scores):
            ce_scores[(a, b)] = float(s)
        print(f"[full_sweep] CE re-rank done in {time.time()-t5:.1f}s",
              flush=True)

    # ── Merge + emit ────────────────────────────────────────────────────
    all_pairs: Set[Tuple[int, int]] = set(minhash_pairs.keys()) | set(embedding_pairs.keys())
    print(f"[full_sweep] writing {len(all_pairs)} candidate pairs to {OUTPUT_CSV}",
          flush=True)

    rows_out = 0
    with OUTPUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "qid_a", "qid_b",
            "source_a", "source_b",
            "measure_a", "measure_b",
            "subtype_a", "subtype_b",
            "jaccard_estimate", "cosine", "ce_score",
            "flagged_by",
            "stem_a_first120", "stem_b_first120",
        ])
        w.writeheader()

        ce_thresh = dedup_config.CROSS_ENCODER_PARAPHRASE_THRESHOLD
        for a, b in sorted(all_pairs):
            if args.max_pairs and rows_out >= args.max_pairs:
                break
            qa = questions_by_id.get(a)
            qb = questions_by_id.get(b)
            if qa is None or qb is None:
                continue

            jac = minhash_pairs.get((a, b))
            cos = embedding_pairs.get((a, b))
            ce_s = ce_scores.get((a, b))

            in_minhash = jac is not None
            # Embedding "flag" requires CE confirmation when CE was run.
            in_embedding = (
                cos is not None
                and (
                    ce_s is None  # CE skipped → use cos alone
                    or ce_s >= ce_thresh
                )
            )

            if not (in_minhash or in_embedding):
                continue

            if in_minhash and in_embedding:
                flagged = "both"
            elif in_minhash:
                flagged = "minhash"
            else:
                flagged = "embedding"

            w.writerow({
                "qid_a": a, "qid_b": b,
                "source_a": qa.provenance or "", "source_b": qb.provenance or "",
                "measure_a": _measure_of(qa), "measure_b": _measure_of(qb),
                "subtype_a": qa.subtype or "", "subtype_b": qb.subtype or "",
                "jaccard_estimate": f"{jac:.4f}" if jac is not None else "",
                "cosine": f"{cos:.4f}" if cos is not None else "",
                "ce_score": f"{ce_s:.4f}" if ce_s is not None else "",
                "flagged_by": flagged,
                "stem_a_first120": _stem_first_120(qa),
                "stem_b_first120": _stem_first_120(qb),
            })
            rows_out += 1

    elapsed = time.time() - t0
    print(f"[full_sweep] DONE — wrote {rows_out} pairs in {elapsed:.1f}s "
          f"({elapsed/60:.1f} min) to {OUTPUT_CSV}",
          flush=True)
    print(f"[full_sweep] thresholds used: "
          f"jaccard≥{dedup_config.LSH_THRESHOLD}, "
          f"cosine≥{dedup_config.EMBEDDING_COSINE_THRESHOLD}, "
          f"CE≥{dedup_config.CROSS_ENCODER_PARAPHRASE_THRESHOLD}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
