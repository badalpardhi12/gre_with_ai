"""Stage-2 of the two-stage dedup pipeline: bi-encoder + cross-encoder.

Pipeline:

    Question rows ──► ``embed_questions`` (bi-encoder, all-mpnet-base-v2)
                  ──► ``find_paraphrase_candidates`` (cosine NN, cos>=tau1)
                  ──► ``CrossEncoderJudge.judge_pairs_batch`` (re-rank, score>=tau2)
                  ──► duplicate-pair list

Both models are loaded LAZILY on first use so unit tests that don't load
weights run in milliseconds. Thresholds and model names live in
:mod:`services.dedup.config` so an A/B sweep doesn't have to grep code.

Python 3.9 compatible: no ``X | Y`` unions, no ``match``.

Network discipline
------------------
On first model load, ``sentence-transformers`` does an HTTPS HEAD against
huggingface.co to confirm the cached snapshot is still current. Inside
the Apple proxy that HEAD redirects to a Xet endpoint that occasionally
hangs forever — see worker P1.3's notes for the diagnosis. We force
``HF_HUB_OFFLINE=1`` + ``TRANSFORMERS_OFFLINE=1`` *iff the user hasn't
already set them* so the cached files are used directly. The cache is
populated by the orchestrator's environment-bootstrap step (or by a
manual curl-based pre-populate, if the upstream Xet flow stalls).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np

# Default to offline mode for reproducibility + Xet-proxy avoidance.
# Callers that need to fetch fresh weights can unset these before
# importing ``services.dedup.embedding_stage``.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# We only import the heavy sentence_transformers classes lazily — see
# ``_get_bi_encoder`` and ``CrossEncoderJudge.judge_pair`` — so importing
# this module costs nothing if the caller only wants e.g.
# ``find_paraphrase_candidates`` over a pre-computed array.

from services.dedup.config import (
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_COSINE_THRESHOLD,
    EMBEDDING_INPUT_CHAR_CAP,
    EMBEDDING_MODEL_NAME,
    EMBEDDING_TOP_K,
    CROSS_ENCODER_MODEL_NAME,
)


# ── Lazy model loaders ─────────────────────────────────────────────────

_BI_ENCODER = None  # type: Optional[object]


def _get_bi_encoder(model_name: str = EMBEDDING_MODEL_NAME):
    """Return a cached SentenceTransformer instance for ``model_name``.

    The model is downloaded on first call (huggingface.co is allowlisted
    in this venv) and reused for the lifetime of the process.
    """
    global _BI_ENCODER
    if _BI_ENCODER is None or getattr(_BI_ENCODER, "_dedup_model_name", None) != model_name:
        from sentence_transformers import SentenceTransformer  # local import (slow)
        model = SentenceTransformer(model_name)
        # Tag the instance so a sweep that switches model names re-loads.
        try:
            model._dedup_model_name = model_name
        except AttributeError:
            pass
        _BI_ENCODER = model
    return _BI_ENCODER


# ── Question → text canonicalisation ───────────────────────────────────

def question_to_text(question, max_chars: int = EMBEDDING_INPUT_CHAR_CAP) -> str:
    """Render a Question row as a single string for embedding.

    Field ordering MATTERS for paraphrase detection. Two RC questions
    sharing the same passage have IDENTICAL stimulus content; if the
    stimulus dominates the encoder input, the bi-encoder collapses the
    two distinct items to cosine~1.0 — exactly the
    ``paraphrase_candidate``-No failure mode P1.1 flagged. We therefore:

    1. Put the question ``prompt`` (the discriminating signal) FIRST.
    2. Append each option label + text (also discriminating).
    3. Append a SHORT stimulus head (capped at ``stim_head_chars``)
       last, so it provides topical context without drowning out the
       question-specific signal.

    Accepts either a Peewee ``Question`` instance or an object with
    duck-typed ``prompt``/``options``/``stimulus`` attributes — the
    tests use a tiny dataclass to avoid spinning up a real DB.
    """
    parts: List[str] = []

    prompt = getattr(question, "prompt", "") or ""
    if prompt:
        parts.append(prompt.strip())

    options_iter = getattr(question, "options", None)
    if options_iter is not None:
        try:
            opts = list(options_iter)
        except TypeError:
            opts = []
        for opt in opts:
            label = getattr(opt, "option_label", "") or ""
            text = getattr(opt, "option_text", "") or ""
            if label or text:
                parts.append(f"{label}. {text}".strip())

    stim = getattr(question, "stimulus", None)
    if stim is not None:
        stim_content = getattr(stim, "content", "") or ""
        if stim_content:
            # Short head — enough to disambiguate by passage topic but
            # not so long that two RC items on the same passage look
            # identical to the bi-encoder.
            stim_head_chars = 400
            parts.append(stim_content[:stim_head_chars].strip())

    blob = "\n".join(p for p in parts if p)
    if max_chars and len(blob) > max_chars:
        blob = blob[:max_chars]
    return blob


# ── Public API: embedding ──────────────────────────────────────────────

def embed_questions(
    questions: Iterable,
    out_path: Path,
    model_name: str = EMBEDDING_MODEL_NAME,
    batch_size: int = EMBEDDING_BATCH_SIZE,
    progress_every: int = 200,
) -> Tuple[np.ndarray, List[int]]:
    """Encode each Question's stem + stimulus + options.

    Persists the resulting matrix to ``out_path`` (.npy) and writes a
    sidecar JSON next to it (same stem, ``.qids.json``) listing the qids
    in row-order. Returns ``(embeddings, qid_list)``.

    Progress is printed every ``progress_every`` items, on a single
    line, so a 2,599-item run on CPU is observable.

    Notes
    -----
    * Embeddings are L2-normalised so downstream cosine similarity is a
      simple dot product.
    * If ``out_path`` already exists, it is OVERWRITTEN — callers that
      want versioned snapshots should embed the version tuple in the
      filename (``EMBEDDING_VERSION_TUPLE`` from ``config.py``).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    qid_list: List[int] = []
    text_list: List[str] = []
    for q in questions:
        qid = getattr(q, "id", None)
        if qid is None:
            raise ValueError("Question is missing an 'id' attribute")
        qid_list.append(int(qid))
        text_list.append(question_to_text(q))

    if not qid_list:
        empty = np.zeros((0, 0), dtype=np.float32)
        np.save(out_path, empty)
        sidecar = out_path.with_suffix(out_path.suffix + ".qids.json")
        sidecar.write_text(json.dumps([], indent=2))
        return empty, []

    model = _get_bi_encoder(model_name)

    # Batch-encode with our own loop so we can print progress predictably
    # and avoid pulling in tqdm.
    chunks: List[np.ndarray] = []
    n_total = len(text_list)
    for start in range(0, n_total, batch_size):
        end = min(start + batch_size, n_total)
        batch = text_list[start:end]
        emb = model.encode(
            batch,
            batch_size=batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        chunks.append(np.asarray(emb, dtype=np.float32))
        # Progress every ``progress_every`` items (or last batch).
        progressed = end
        if progress_every and (
            (progressed // progress_every) > ((progressed - len(batch)) // progress_every)
            or end == n_total
        ):
            print(f"  [embed] {progressed}/{n_total} ({100.0 * progressed / n_total:.1f}%)")

    embeddings = np.vstack(chunks).astype(np.float32, copy=False)
    np.save(out_path, embeddings)
    sidecar = out_path.with_suffix(out_path.suffix + ".qids.json")
    sidecar.write_text(json.dumps(qid_list))
    return embeddings, qid_list


def load_embeddings(npy_path: Path) -> Tuple[np.ndarray, List[int]]:
    """Re-hydrate ``(embeddings, qid_list)`` from disk."""
    npy_path = Path(npy_path)
    embeddings = np.load(npy_path)
    sidecar = npy_path.with_suffix(npy_path.suffix + ".qids.json")
    qid_list = json.loads(sidecar.read_text())
    return embeddings, [int(q) for q in qid_list]


# ── Public API: cosine retrieval ───────────────────────────────────────

def find_paraphrase_candidates(
    query_embedding: np.ndarray,
    all_embeddings: np.ndarray,
    qid_list: Sequence[int],
    top_k: int = EMBEDDING_TOP_K,
    cosine_threshold: float = EMBEDDING_COSINE_THRESHOLD,
) -> List[Tuple[int, float]]:
    """Return ``(qid, cosine)`` pairs that exceed ``cosine_threshold``.

    Both arrays must be L2-normalised (which is what ``embed_questions``
    produces). Results are sorted by descending cosine and capped at
    ``top_k`` (so a popular topic doesn't blow up the candidate set).
    The query item itself is INCLUDED in the result if its embedding
    appears in ``all_embeddings`` — callers that want to suppress
    self-matches should filter on qid afterward.
    """
    if all_embeddings.size == 0:
        return []
    if query_embedding.ndim != 1:
        raise ValueError(
            f"query_embedding must be 1-D, got shape={query_embedding.shape!r}"
        )
    if len(qid_list) != all_embeddings.shape[0]:
        raise ValueError(
            "qid_list length does not match all_embeddings rows: "
            f"{len(qid_list)} vs {all_embeddings.shape[0]}"
        )

    sims = all_embeddings @ query_embedding  # both normalised → cosine
    above = np.where(sims >= cosine_threshold)[0]
    if above.size == 0:
        return []

    # Sort the survivors by similarity descending.
    sorted_idx = above[np.argsort(-sims[above])]
    if top_k is not None and top_k > 0:
        sorted_idx = sorted_idx[:top_k]

    return [(int(qid_list[i]), float(sims[i])) for i in sorted_idx]


# ── Public API: cross-encoder re-ranker ────────────────────────────────


class CrossEncoderJudge:
    """Lazy-loading wrapper around ``sentence-transformers/CrossEncoder``.

    Loads the (~1.4 GB) model only on first call to ``judge_pair`` /
    ``judge_pairs_batch`` — instantiating the class is free.
    """

    def __init__(self, model_name: str = CROSS_ENCODER_MODEL_NAME) -> None:
        self.model_name = model_name
        self._model = None  # type: Optional[object]

    def _ensure_loaded(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder  # local import (slow)
            self._model = CrossEncoder(self.model_name)
        return self._model

    def judge_pair(self, text_a: str, text_b: str) -> float:
        """Return the cross-encoder regression score for one pair.

        Score is on the model's native scale (STS-B: 0..5). Higher means
        more semantically equivalent. Use
        :data:`services.dedup.config.CROSS_ENCODER_PARAPHRASE_THRESHOLD`
        as the duplicate decision boundary.
        """
        return self.judge_pairs_batch([(text_a, text_b)])[0]

    def judge_pairs_batch(
        self,
        pairs: Sequence[Tuple[str, str]],
        batch_size: int = 16,
    ) -> List[float]:
        """Vectorised version of :meth:`judge_pair`.

        Returns a list of floats, one per input pair, in the same order.
        Returns the empty list immediately for empty input (no model
        load).
        """
        if not pairs:
            return []
        model = self._ensure_loaded()
        # CrossEncoder.predict accepts a list of [a, b] pairs.
        scores = model.predict(
            [[a, b] for (a, b) in pairs],
            batch_size=batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [float(s) for s in np.asarray(scores).reshape(-1).tolist()]
