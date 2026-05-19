"""Sample 200 question-pairs for the held-out dedup evaluation set
(Phase 1.1, docs/implementation_plan_2026_05_18.md §164-174).

The sampler draws from three pools:

  * pool A — "high-Jaccard" pairs (shingle Jaccard >= 0.4): exercises stage-1
    (MinHash) dedup. Target 60 pairs.
  * pool B — "paraphrase candidates" (low Jaccard, high TF-IDF cosine):
    exercises stage-2 (embedding + cross-encoder) dedup. Target 60 pairs.
  * pool C — uniform random "free" pairs: keeps the negative class realistic.
    Target 80 pairs.

Within each pool the measure split is enforced (≈100 Quant / ≈100 Verbal
across the full 200), and the source-pairing column is set post hoc to one
of {cluster, same_source, cross_source} so downstream consumers can still
slice by source-pairing without us forcing a hard quota that the data may
not support (the live bank, as of 2026-05-18, has 0 quant cluster pairs).

The output CSV at data/dedup_eval/candidate_pairs_2026_05_18.csv is
overwritten on each run. Reproducibility: random.seed(20260518).
"""
from __future__ import annotations

import csv
import math
import random
import re
import sys
from collections import defaultdict
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models.database import db, Question, QuestionOption, Stimulus  # noqa: E402

OUT_PATH = PROJECT_ROOT / "data" / "dedup_eval" / "candidate_pairs_2026_05_18.csv"

SEED = 20260518
TOTAL_PAIRS = 200

# Pool quotas (must sum to TOTAL_PAIRS).
TARGET_HIGH_JACCARD = 60       # 30%
TARGET_PARAPHRASE = 60         # 30%
TARGET_FREE = 80               # 40%

# Per-measure split inside each pool (best-effort).
MEASURE_SPLIT = {"quant": 0.5, "verbal": 0.5}

# Bucket cutoffs.
HIGH_JACCARD_CUT = 0.40
LOW_JACCARD_CUT = 0.15
HIGH_COSINE_CUT = 0.55   # tuned against TF-IDF on stem+stim+choices

SHINGLE_K = 5

# Knobs for the candidate-search loops.
NEAREST_K = 25                  # TF-IDF top-K neighbors to scan per query
MAX_HIGH_JACCARD_SCAN = 1500    # query-rows to evaluate before stopping
MAX_PARAPHRASE_SCAN = 1500
MAX_FREE_DRAWS = 50_000


# ── Tokenization helpers ────────────────────────────────────────────


_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_TAG_RE = re.compile(r"<[^>]+>")
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+|\\\(|\\\)|\\\[|\\\]")
_WS_RE = re.compile(r"\s+")


def normalize_text(text):  # type: (str) -> str
    """Lowercase, strip HTML + LaTeX commands, collapse whitespace."""
    if not text:
        return ""
    s = unescape(text)
    s = _TAG_RE.sub(" ", s)
    s = _LATEX_CMD_RE.sub(" ", s)
    s = s.lower()
    s = _WS_RE.sub(" ", s).strip()
    return s


def tokenize(text):  # type: (str) -> List[str]
    return _WORD_RE.findall(normalize_text(text))


def shingles(tokens, k=SHINGLE_K):  # type: (List[str], int) -> Set[str]
    if len(tokens) < k:
        return set(tokens)
    return set(" ".join(tokens[i:i + k]) for i in range(len(tokens) - k + 1))


def jaccard(a, b):  # type: (Set[str], Set[str]) -> float
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# ── DB load ─────────────────────────────────────────────────────────


def load_live_questions():
    """Return list of dicts, one per live Question, with stem + meta."""
    db.connect(reuse_if_open=True)
    try:
        stim_map = {s.id: s.content for s in
                    Stimulus.select(Stimulus.id, Stimulus.content)}
        questions = list(
            Question
            .select(
                Question.id, Question.measure, Question.subtype, Question.source,
                Question.stimulus, Question.prompt,
            )
            .where(Question.status == "live")
        )
        opts_by_qid = defaultdict(list)
        for opt in QuestionOption.select():
            opts_by_qid[opt.question_id].append(opt.option_text)
    finally:
        db.close()

    out = []
    for q in questions:
        prompt_text = q.prompt or ""
        stim_text = stim_map.get(q.stimulus_id, "") if q.stimulus_id else ""
        opts = opts_by_qid.get(q.id, [])
        full_text = " ".join([stim_text, prompt_text] + opts)
        out.append({
            "qid": q.id,
            "measure": q.measure,
            "subtype": q.subtype,
            "source": q.source,
            "stimulus_id": q.stimulus_id,
            "prompt": prompt_text,
            "stem_norm": normalize_text(prompt_text),
            "full_text_norm": normalize_text(full_text),
        })
    return out


# ── TF-IDF + nearest-neighbor index ─────────────────────────────────


def build_tfidf_per_measure(rows):
    """Per-measure TF-IDF + a top-K neighbor list per query.

    Returns dict keyed by measure: {
      'rows': List[row], 'matrix': csr, 'qid_to_idx': dict, 'topk': dict[idx -> List[(idx, cos)]],
    }
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        return None

    out = {}
    for measure in ("quant", "verbal"):
        m_rows = [r for r in rows if r["measure"] == measure]
        if not m_rows:
            continue
        docs = [r["full_text_norm"] or " " for r in m_rows]
        vec = TfidfVectorizer(
            analyzer="word",
            ngram_range=(1, 2),
            min_df=2,
            max_df=0.95,
            sublinear_tf=True,
        )
        matrix = vec.fit_transform(docs)
        # Compute top-K neighbors via batched sparse @ sparse.T
        sims = (matrix @ matrix.T).toarray()
        # Zero the diagonal so a question isn't its own nearest neighbor
        for i in range(sims.shape[0]):
            sims[i, i] = 0.0
        topk = {}
        for i in range(sims.shape[0]):
            row = sims[i]
            # argpartition then sort the top-K
            if row.size <= NEAREST_K:
                idxs = list(range(row.size))
            else:
                idxs = list(row.argpartition(-NEAREST_K)[-NEAREST_K:])
            scored = sorted(((j, float(row[j])) for j in idxs), key=lambda x: -x[1])
            topk[i] = scored
        qid_to_idx = {r["qid"]: i for i, r in enumerate(m_rows)}
        out[measure] = {
            "rows": m_rows, "matrix": matrix, "qid_to_idx": qid_to_idx,
            "topk": topk,
        }
    return out


# ── Pair sampling ───────────────────────────────────────────────────


def classify_strata(qa, qb):
    """Source-pairing label: cluster | same_source | cross_source."""
    if qa["stimulus_id"] and qa["stimulus_id"] == qb["stimulus_id"]:
        return "cluster"
    if qa["source"] == qb["source"]:
        return "same_source"
    return "cross_source"


def classify_bucket(jaccard_val, cosine_val):
    if jaccard_val >= HIGH_JACCARD_CUT:
        return "high_jaccard"
    if jaccard_val < LOW_JACCARD_CUT and cosine_val >= HIGH_COSINE_CUT:
        return "paraphrase_candidate"
    return "free"


def sample_high_jaccard(rng, per_measure, shingle_cache, target_per_measure):
    """Walk TF-IDF top-K neighbors looking for shingle-Jaccard >= 0.4."""
    pairs = []
    seen = set()  # frozenset of (qa_qid, qb_qid)
    for measure, want in target_per_measure.items():
        bundle = per_measure.get(measure)
        if bundle is None or want == 0:
            continue
        m_rows = bundle["rows"]
        topk = bundle["topk"]
        order = list(range(len(m_rows)))
        rng.shuffle(order)
        order = order[:MAX_HIGH_JACCARD_SCAN]
        local = []
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
    return pairs, seen


def sample_paraphrase(rng, per_measure, shingle_cache, target_per_measure, already_seen):
    """Walk TF-IDF top-K neighbors looking for low-Jaccard / high-cosine pairs."""
    pairs = []
    seen = set(already_seen)
    for measure, want in target_per_measure.items():
        bundle = per_measure.get(measure)
        if bundle is None or want == 0:
            continue
        m_rows = bundle["rows"]
        topk = bundle["topk"]
        order = list(range(len(m_rows)))
        rng.shuffle(order)
        order = order[:MAX_PARAPHRASE_SCAN]
        local = []
        for i in order:
            if len(local) >= want:
                break
            qa = m_rows[i]
            ja_shingles = shingle_cache[qa["qid"]]
            for j, cos in topk.get(i, []):
                if j == i:
                    continue
                if cos < HIGH_COSINE_CUT:
                    break  # neighbors are cosine-sorted, give up early
                qb = m_rows[j]
                key = frozenset((qa["qid"], qb["qid"]))
                if key in seen:
                    continue
                jb_shingles = shingle_cache[qb["qid"]]
                ja = jaccard(ja_shingles, jb_shingles)
                if ja < LOW_JACCARD_CUT:
                    seen.add(key)
                    local.append({
                        "qa": qa, "qb": qb, "jaccard": ja, "cosine": float(cos),
                        "bucket": "paraphrase_candidate",
                    })
                    if len(local) >= want:
                        break
        pairs.extend(local)
    return pairs, seen


def sample_free(rng, rows, per_measure, shingle_cache, target_per_measure, already_seen):
    """Uniform random pairs (within measure)."""
    pairs = []
    seen = set(already_seen)
    by_measure = {
        "quant": [r for r in rows if r["measure"] == "quant"],
        "verbal": [r for r in rows if r["measure"] == "verbal"],
    }
    for measure, want in target_per_measure.items():
        pool = by_measure.get(measure, [])
        if not pool or want == 0:
            continue
        local = []
        bundle = per_measure.get(measure)
        attempts = 0
        while len(local) < want and attempts < MAX_FREE_DRAWS:
            attempts += 1
            qa, qb = rng.sample(pool, 2)
            key = frozenset((qa["qid"], qb["qid"]))
            if key in seen:
                continue
            ja = jaccard(shingle_cache[qa["qid"]], shingle_cache[qb["qid"]])
            cos = 0.0
            if bundle is not None:
                ia = bundle["qid_to_idx"][qa["qid"]]
                ib = bundle["qid_to_idx"][qb["qid"]]
                cos = float((bundle["matrix"][ia] @ bundle["matrix"][ib].T).toarray()[0, 0])
            seen.add(key)
            local.append({
                "qa": qa, "qb": qb, "jaccard": ja, "cosine": cos,
                "bucket": classify_bucket(ja, cos),
            })
        pairs.extend(local)
    return pairs, seen


# ── Output ──────────────────────────────────────────────────────────


def write_csv(pairs, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "pair_id", "qid_a", "qid_b",
        "source_a", "source_b",
        "measure_a", "measure_b",
        "subtype_a", "subtype_b",
        "stem_a_first120", "stem_b_first120",
        "jaccard_5_shingle", "tfidf_cosine",
        "sampling_strata", "bucket",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, p in enumerate(pairs, 1):
            qa = p["qa"]
            qb = p["qb"]
            writer.writerow({
                "pair_id": i,
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


# ── Main ────────────────────────────────────────────────────────────


def main():
    rng = random.Random(SEED)
    print("[sample_dedup_pairs] loading live questions ...")
    rows = load_live_questions()
    print("[sample_dedup_pairs] %d live questions loaded "
          "(quant=%d, verbal=%d)" % (
              len(rows),
              sum(1 for r in rows if r["measure"] == "quant"),
              sum(1 for r in rows if r["measure"] == "verbal"),
          ))

    print("[sample_dedup_pairs] computing per-measure TF-IDF + top-K "
          "neighbors ...")
    per_measure = build_tfidf_per_measure(rows)
    if per_measure is None:
        print("[sample_dedup_pairs] FATAL: sklearn missing — aborting")
        sys.exit(2)

    print("[sample_dedup_pairs] caching shingle sets ...")
    shingle_cache = {r["qid"]: shingles(tokenize(r["stem_norm"])) for r in rows}

    # Per-measure quotas inside each pool (best-effort).
    def split(target):
        quant = int(round(target * MEASURE_SPLIT["quant"]))
        verbal = target - quant
        return {"quant": quant, "verbal": verbal}

    print("[sample_dedup_pairs] pool A: hunting high-Jaccard pairs ...")
    high_pairs, seen = sample_high_jaccard(
        rng, per_measure, shingle_cache, split(TARGET_HIGH_JACCARD))
    print("[sample_dedup_pairs]   high-Jaccard found: %d" % len(high_pairs))

    print("[sample_dedup_pairs] pool B: hunting paraphrase candidates ...")
    par_pairs, seen = sample_paraphrase(
        rng, per_measure, shingle_cache, split(TARGET_PARAPHRASE), seen)
    print("[sample_dedup_pairs]   paraphrase candidates found: %d" % len(par_pairs))

    print("[sample_dedup_pairs] pool C: drawing free random pairs ...")
    free_pairs, seen = sample_free(
        rng, rows, per_measure, shingle_cache, split(TARGET_FREE), seen)
    print("[sample_dedup_pairs]   free pairs: %d" % len(free_pairs))

    pairs = high_pairs + par_pairs + free_pairs

    if len(pairs) < TOTAL_PAIRS:
        print("[sample_dedup_pairs] WARNING: only sampled %d / %d pairs "
              "— some pools exhausted candidate space" % (len(pairs), TOTAL_PAIRS))

    bucket_counts = defaultdict(int)
    measure_counts = defaultdict(int)
    strata_counts = defaultdict(int)
    for p in pairs:
        bucket_counts[p["bucket"]] += 1
        measure_counts[p["qa"]["measure"]] += 1
        strata_counts[classify_strata(p["qa"], p["qb"])] += 1

    print("[sample_dedup_pairs] bucket mix: %s" % dict(bucket_counts))
    print("[sample_dedup_pairs] measure mix: %s" % dict(measure_counts))
    print("[sample_dedup_pairs] strata mix: %s" % dict(strata_counts))

    write_csv(pairs, OUT_PATH)
    print("[sample_dedup_pairs] wrote %d pairs to %s" % (len(pairs), OUT_PATH))


if __name__ == "__main__":
    main()
