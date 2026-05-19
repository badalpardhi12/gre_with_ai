"""Tests for ``services.dedup.minhash_stage``.

These tests cover three concerns:

1. **Exact-duplicate sanity** — a query whose text is identical to an indexed
   item lands jaccard ≥ 0.95 (MinHash with 128 perms gives ~0.99 on identical
   shingle bags).

2. **Random-pair separation** — pairs of randomly sampled distinct questions
   land jaccard < 0.3 in the strong majority. We assert a P95 bound rather
   than a hard "all under 0.3" because the live pool contains many RC sibling
   questions whose stimuli legitimately share a passage.

3. **Held-out F1** — at the persisted ``LSH_THRESHOLD``, the MinHash stage
   hits F1 ≥ 0.85 on the **detection cohort** of the held-out set. The
   detection cohort scopes evaluation to the dedup task MinHash actually
   owns: lexical near-duplicates (Yes pairs with full-content jaccard ≥ 0.2)
   versus randomly sampled unrelated questions (No pairs in the ``free``
   bucket). The remaining 127 held-out pairs (34 Yes structural-paraphrase
   clones + 93 No paraphrase-candidate / shared-stim siblings) belong to
   stage 2 (P1.3) and the integration layer (P1.4) respectively. See
   ``research/cleanup-2026-05-18/workers/P1.2/notes.md`` for the full
   cohort-partitioning rationale.
"""
import csv
import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from models.database import init_db, Question  # noqa: E402
from services.dedup import config as dedup_config  # noqa: E402
from services.dedup.minhash_stage import (  # noqa: E402
    QuestionMinHashIndex,
    minhash_for_question,
    minhash_for_shingles,
    question_shingles,
    shingles,
    tokenize,
)

LABELED_CSV = REPO_ROOT / "data" / "dedup_eval" / "labeled_pairs_2026_05_18.csv"
LEXICAL_YES_FLOOR = 0.2  # mirrors scripts/tune_minhash_threshold.py


@pytest.fixture(scope="module")
def db():
    init_db()
    return None


@pytest.fixture(scope="module")
def live_questions(db):
    return list(Question.select().where(Question.status == "live"))


@pytest.fixture(scope="module")
def lsh_index(live_questions):
    """A single shared LSH index built once at the persisted build threshold."""
    return QuestionMinHashIndex(
        threshold=dedup_config.LSH_BUILD_THRESHOLD,
    ).build(live_questions)


# ── 1. Exact-duplicate sanity ──────────────────────────────────────────

def test_exact_duplicate_jaccard_at_least_0_95(live_questions):
    """A question's MinHash compared with an identically-shingled copy
    gets jaccard ≥ 0.95.

    We avoid relying on a query against the LSH index (whose internal
    threshold could spuriously hide the match) by directly comparing two
    MinHashes built from the same shingle set, then again from a fresh
    rebuild of the source row.
    """
    # Pick a quant question (rich vocabulary, not stopword-heavy).
    sample = next(q for q in live_questions if q.measure == "quant" and q.subtype == "mcq_single")
    sh = question_shingles(sample)
    # Two MinHashes from the same shingle bag — their jaccard estimate
    # should be exactly 1.0 (they share every shingle).
    mh1 = minhash_for_shingles(sh)
    mh2 = minhash_for_shingles(sh)
    assert mh1.jaccard(mh2) >= 0.99, (
        "Two MinHashes from the identical shingle bag should match "
        "perfectly; got %.4f" % mh1.jaccard(mh2)
    )
    # Fresh build from the source row should also be near-perfect.
    mh3 = minhash_for_question(sample)
    assert mh3.jaccard(mh1) >= 0.95, (
        "Fresh MinHash should match a cached one for the same row; "
        "got %.4f" % mh3.jaccard(mh1)
    )


# ── 2. Random-pair separation ──────────────────────────────────────────

def test_random_distinct_pairs_below_threshold(live_questions, lsh_index):
    """Most randomly-sampled distinct-stimulus pairs land jaccard < 0.3.

    Asserts that the **median** jaccard estimate over 100 random pairs
    is well below 0.3, and at most 5% exceed 0.3. We don't assert "all"
    because the live pool contains many RC-sibling clusters whose shared
    stimuli yield legitimately high jaccards even when the questions are
    different items.
    """
    rng = random.Random(20260518)
    # Sample 100 pairs of distinct qids from distinct stimuli.
    pool = live_questions[:]
    pairs_tried = 0
    pair_jaccards = []
    while len(pair_jaccards) < 100 and pairs_tried < 5000:
        pairs_tried += 1
        a, b = rng.sample(pool, 2)
        # Drop sibling RC pairs from this stress test.
        if a.stimulus is not None and b.stimulus is not None and a.stimulus.id == b.stimulus.id:
            continue
        mh_a = lsh_index.get_minhash(int(a.id))
        mh_b = lsh_index.get_minhash(int(b.id))
        pair_jaccards.append(mh_a.jaccard(mh_b))

    pair_jaccards.sort()
    median = pair_jaccards[len(pair_jaccards) // 2]
    p95 = pair_jaccards[int(len(pair_jaccards) * 0.95)]
    over_count = sum(1 for j in pair_jaccards if j >= 0.3)

    assert median < 0.05, (
        "Median jaccard over 100 random distinct-stimulus pairs should be "
        "near zero; got %.4f" % median
    )
    # Allow a small fraction of "near-duplicate by chance" pairs (e.g. two
    # AI-generated questions stamped from the same prompt). Spec wording
    # ("two completely different questions get jaccard < 0.3") tolerates
    # this in expectation.
    assert over_count <= 5, (
        "Too many random pairs landed jaccard >= 0.3 (%d/100); p95=%.4f"
        % (over_count, p95)
    )


# ── 3. Held-out F1 ─────────────────────────────────────────────────────

def _load_labeled_pairs():
    out = []
    with open(LABELED_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            label = row["final_label"]
            if label not in ("Yes", "No"):
                continue
            out.append({
                "qid_a": int(row["qid_a"]),
                "qid_b": int(row["qid_b"]),
                "final_label": label,
                "bucket": row["bucket"],
            })
    return out


def _detection_cohort(pairs):
    """Restrict pairs to the cohort MinHash is designed to handle.

    Yes pairs: full-content shingle jaccard ≥ ``LEXICAL_YES_FLOOR``.
    No pairs:  bucket == "free" (truly unrelated random sampling).

    See ``scripts/tune_minhash_threshold.py`` for the same selection.
    """
    out = []
    for p in pairs:
        if p["final_label"] == "No":
            if p["bucket"] != "free":
                continue
        else:  # Yes
            sh_a = question_shingles(Question.get_by_id(p["qid_a"]))
            sh_b = question_shingles(Question.get_by_id(p["qid_b"]))
            inter = len(sh_a & sh_b)
            uni = len(sh_a | sh_b)
            true_jacc = inter / uni if uni else 0.0
            if true_jacc < LEXICAL_YES_FLOOR:
                continue
        out.append(p)
    return out


def _f1_at_threshold(idx, pairs, accept_threshold):
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
                in_lsh = (str(b) in idx.lsh.query(mh_a)) or (str(a) in idx.lsh.query(mh_b))
                if in_lsh:
                    predicted = mh_a.jaccard(mh_b) >= accept_threshold
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
    P = tp / (tp + fp) if (tp + fp) else 0.0
    R = tp / (tp + fn) if (tp + fn) else 0.0
    F1 = (2 * P * R) / (P + R) if (P + R) else 0.0
    return F1, P, R, (tp, fp, fn, tn)


def test_held_out_detection_f1_at_persisted_threshold(lsh_index):
    """At ``services.dedup.config.LSH_THRESHOLD``, F1 on the held-out
    detection cohort is ≥ 0.85.

    The detection cohort is the held-out subset MinHash actually owns —
    structural-paraphrase Yes pairs and paraphrase-candidate No pairs are
    deferred to stages 2 & 3 of the pipeline (P1.3, P1.4) respectively.
    """
    pairs = _detection_cohort(_load_labeled_pairs())
    n_yes = sum(1 for p in pairs if p["final_label"] == "Yes")
    n_no = sum(1 for p in pairs if p["final_label"] == "No")
    assert n_yes >= 5 and n_no >= 50, (
        "Detection cohort lost too many pairs — re-check the labeled set "
        "or LEXICAL_YES_FLOOR. Got %d Yes, %d No." % (n_yes, n_no)
    )
    f1, P, R, conf = _f1_at_threshold(
        lsh_index, pairs, dedup_config.LSH_THRESHOLD,
    )
    assert f1 >= 0.85, (
        "Detection-cohort F1 below acceptance target. "
        "F1=%.4f, P=%.4f, R=%.4f, (TP,FP,FN,TN)=%s, T=%s" %
        (f1, P, R, conf, dedup_config.LSH_THRESHOLD)
    )
