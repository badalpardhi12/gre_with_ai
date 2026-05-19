"""Persisted configuration for the two-stage dedup pipeline.

Per W5 risk A4 in docs/implementation_plan_2026_05_18.md, every persisted
dedup decision must be tagged with the (model_version, threshold) tuple so
a future re-run with different settings doesn't silently contradict shipped
retirements.

Stage 1 (MinHash/LSH, P1.2): cheap lexical near-dup catch.
Stage 2 (embedding + cross-encoder, P1.3): semantic paraphrase catch.

The tuning scripts auto-rewrite their respective thresholds in this file
after sweeping the held-out labelled set.
"""
from __future__ import annotations

# ── Stage-1: MinHash / LSH ─────────────────────────────────────────────

# Algorithm-version tuple used to tag dedup decisions in storage.
# Bumping this string invalidates prior persisted candidate lists.
EMBEDDING_OR_HASH_MODEL = "minhash-shingle5-stopword-mixed-2026-05-18"

# Stage marker used by the integration layer (P1.4) to attribute a candidate
# pair back to the stage that produced it.
STAGE = "stage1_minhash"

# MinHash permutation count. 128 is the datasketch default and gives ~8% std
# error on jaccard estimates — adequate for a *candidate generator*; the
# embedding+cross-encoder stage (P1.3) handles precision.
LSH_NUM_PERM = 128

# k-shingle size. 5 captures phrase-level co-occurrence without over-fitting
# to single-token swaps.
SHINGLE_SIZE = 5

# LSH-internal threshold used to *build* the index. Kept low so the
# candidate-generation step has high recall — datasketch's internal
# band-thresholding is calibrated at the 50% probability point, so a low
# build threshold compensates for MinHash estimation noise. The actual
# accept/reject decision is post-filtered by ``LSH_THRESHOLD`` below.
# Tuned to 0.2 by ``scripts/tune_minhash_threshold.py``: lower values
# add no recall on the held-out set; higher values miss lexical Yes
# pairs whose true jaccard sits in [0.2, 0.3].
LSH_BUILD_THRESHOLD = 0.2

# Tuned LSH band-threshold (jaccard estimate accept threshold for a
# candidate to be classified as a near-duplicate). Auto-rewritten by
# ``scripts/tune_minhash_threshold.py`` after sweeping the held-out set.
LSH_THRESHOLD = 0.2

# ── Stage-2: Embedding + cross-encoder ─────────────────────────────────

# Bi-encoder for fast cosine retrieval over all live questions.
# all-mpnet-base-v2: 768-dim, ~420 MB on disk, mean ~80 ms/item on CPU,
# strong on STS / paraphrase tasks (the v1 choice per plan §199).
EMBEDDING_MODEL_NAME = "sentence-transformers/all-mpnet-base-v2"

# Cross-encoder re-ranker for cosine-survivor pairs. The
# stsb-roberta-large checkpoint applies a sigmoid before returning,
# so scores observed in practice are in [0, 1] — NOT the [0, 5]
# STS-B regression scale the model card describes. (See worker P1.3
# notes for the diagnosis.)
CROSS_ENCODER_MODEL_NAME = "cross-encoder/stsb-roberta-large"

# Cosine-similarity threshold for *candidate* pairs (stage-2A).
# Tuned 2026-05-18 against the 215-pair labelled set (Yes/No only).
# Pairs with cos < threshold never reach the cross-encoder.
EMBEDDING_COSINE_THRESHOLD: float = 0.9

# Cross-encoder regression threshold (stage-2B). Pairs with score >=
# threshold are flagged as paraphrase-duplicates. Observed scale is
# [0, 1] (sigmoid output). Tuned by ``scripts/tune_embedding_thresholds.py``.
CROSS_ENCODER_PARAPHRASE_THRESHOLD: float = 0.3

# Reproducibility tuple — see W5 risk A4. The trailing date string is a
# manual stamp bumped whenever a tuning sweep promotes new thresholds.
EMBEDDING_VERSION_TUPLE = (
    EMBEDDING_MODEL_NAME,
    CROSS_ENCODER_MODEL_NAME,
    EMBEDDING_COSINE_THRESHOLD,
    CROSS_ENCODER_PARAPHRASE_THRESHOLD,
    "2026-05-18",
)

# ── Encoding budget ────────────────────────────────────────────────────

# Cap input length for the bi-encoder. mpnet handles 384 tokens natively;
# we crop input strings to ~2,000 chars (longer than any real GRE stem +
# stimulus head + options) before feeding to the tokenizer so the call
# never silently truncates a passage that *should* have been split.
EMBEDDING_INPUT_CHAR_CAP = 2000

# Default batch size for bi-encoder encoding on CPU.
EMBEDDING_BATCH_SIZE = 32

# Default top-k for nearest-neighbour cosine retrieval per query.
EMBEDDING_TOP_K = 20
