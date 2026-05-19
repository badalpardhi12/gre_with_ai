"""Top-level integration of the two-stage dedup pipeline (Phase 1.4).

Composes the cheap MinHash/LSH stage (P1.2) and the expensive embedding +
cross-encoder stage (P1.3) into a single ``find_dup_for(...)`` callable
that ingest scripts can invoke before each ``Question.create``.

Algorithm
---------
For every candidate question (prompt + optional stimulus + optional
options) we ask, "is there an existing live question that is the same
item?":

1. **Stage 1 — MinHash/LSH** (cheap, lexical). Build a query MinHash,
   query the LSH index, and post-filter survivors by the persisted
   ``LSH_THRESHOLD``. The highest-jaccard survivor wins.
2. **Stage 2 — embedding + cross-encoder** (expensive, semantic). Embed
   the candidate with the bi-encoder, find cosine ≥
   ``EMBEDDING_COSINE_THRESHOLD`` survivors over the persisted
   embeddings matrix, then rerank with the cross-encoder. The highest
   CE-score survivor whose score ≥ ``CROSS_ENCODER_PARAPHRASE_THRESHOLD``
   wins.
3. If neither stage flags a duplicate, return ``None``.

Stages are evaluated lazily: if stage 1 finds a hit, stage 2's
embedding model never loads.

Persistence
-----------
Every accept / reject decision is appended (one JSON object per line)
to ``data/dedup_eval/ingest_decisions.jsonl``. The schema is documented
in :meth:`DedupService.log_decision`. Each line carries the
``EMBEDDING_VERSION_TUPLE`` from :mod:`services.dedup.config` so a
later threshold sweep doesn't silently contradict shipped decisions
(W5 risk A4).

Process-local singleton
-----------------------
``get_dedup_service()`` returns a process-cached instance. The first
call lazy-builds the MinHash index from the live DB and lazy-loads the
embeddings npy. Subsequent calls reuse the in-memory state, so a long
extractor run pays the build cost exactly once.

Python 3.9 compatible (Optional[X], no ``X | Y``, no ``match``).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.dedup import config as dedup_config
from services.dedup.embedding_stage import (
    CrossEncoderJudge,
    _get_bi_encoder,
    find_paraphrase_candidates,
    load_embeddings,
    question_to_text,
)
from services.dedup.minhash_stage import (
    QuestionMinHashIndex,
    minhash_for_shingles,
    shingles,
    tokenize,
)


# ── Default artifact paths (W5 risk A4: every persisted decision is
#    tagged with the version tuple, so changing these requires a new
#    artifact filename — see ``EMBEDDING_VERSION_TUPLE`` in config). ────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEDUP_EVAL_DIR = _REPO_ROOT / "data" / "dedup_eval"
DEFAULT_EMBEDDINGS_NPY = _DEDUP_EVAL_DIR / "embeddings_2026_05_18.npy"
DEFAULT_QIDS_JSON = _DEDUP_EVAL_DIR / "embeddings_2026_05_18.npy.qids.json"
DEFAULT_DECISIONS_LOG = _DEDUP_EVAL_DIR / "ingest_decisions.jsonl"


# ── Lightweight query record ────────────────────────────────────────────

class _QueryRecord:
    """Duck-typed Question stand-in for shingle/embedding helpers.

    The dedup helpers use ``getattr(q, "prompt", "")`` /
    ``getattr(q, "options", [])`` / ``getattr(q, "stimulus", None)`` —
    so a tiny class that exposes those attributes is sufficient. We
    avoid building a real ``Question`` row to skip the unique-key
    constraints and the ORM round-trip.
    """

    class _Stim:
        def __init__(self, content: str):
            self.content = content

    class _Opt:
        def __init__(self, label: str, text: str):
            self.option_label = label
            self.option_text = text

    def __init__(self,
                 prompt: str,
                 stimulus_content: str = "",
                 options: Optional[List[str]] = None) -> None:
        # Don't expose ``id`` at all — minhash_stage.find_candidates
        # uses ``hasattr(q, "id")`` to decide whether to dedupe against
        # the query's own row, and a candidate that hasn't been written
        # yet has no id. The minhash module guards on ``id is None`` via
        # an explicit ``hasattr`` check; we satisfy that by simply not
        # defining the attribute.
        self.prompt = prompt or ""
        self.stimulus = (
            _QueryRecord._Stim(stimulus_content) if stimulus_content else None
        )
        opts = options or []
        # Synthesise A/B/C/... labels so the bi-encoder text canonicalisation
        # (which prepends "A. ...") gets a stable signal.
        self.options = [
            _QueryRecord._Opt(chr(ord("A") + i), txt)
            for i, txt in enumerate(opts)
        ]


# ── DedupService ────────────────────────────────────────────────────────

class DedupService:
    """Two-stage ingest-time deduplication callable.

    Build it once per process (the default singleton via
    :func:`get_dedup_service` does this for you), then call
    :meth:`find_dup_for` on every candidate before insert.
    """

    def __init__(
        self,
        *,
        minhash_index: Optional[QuestionMinHashIndex] = None,
        embeddings_npy_path: Optional[Path] = None,
        qids_json_path: Optional[Path] = None,
        cross_encoder: Optional[CrossEncoderJudge] = None,
        decisions_log_path: Optional[Path] = None,
    ) -> None:
        self._minhash_index = minhash_index
        self._embeddings_npy_path = (
            Path(embeddings_npy_path) if embeddings_npy_path is not None
            else DEFAULT_EMBEDDINGS_NPY
        )
        self._qids_json_path = (
            Path(qids_json_path) if qids_json_path is not None
            else DEFAULT_QIDS_JSON
        )
        self._cross_encoder = cross_encoder
        self._decisions_log_path = (
            Path(decisions_log_path) if decisions_log_path is not None
            else DEFAULT_DECISIONS_LOG
        )

        # Lazy state (populated on first build_or_load).
        self._embeddings = None  # type: Optional[Any]
        self._embedding_qids: List[int] = []
        # qid -> question_to_text(...) cached so the cross-encoder pass
        # doesn't re-fetch every survivor's stem from the DB.
        self._embedding_text_cache: Dict[int, str] = {}
        self._loaded = False

    # ── Build / load ────────────────────────────────────────────────────

    def build_or_load(self) -> None:
        """Lazy-build the MinHash index and lazy-load the embeddings.

        Idempotent — re-calling is a no-op once both artefacts are
        cached on the instance.
        """
        if self._loaded:
            return

        if self._minhash_index is None:
            self._minhash_index = self._build_minhash_index_from_db()

        if self._embeddings is None and self._embeddings_npy_path.exists():
            try:
                embeddings, qid_list = load_embeddings(self._embeddings_npy_path)
                self._embeddings = embeddings
                self._embedding_qids = list(qid_list)
            except Exception:
                # Partial / corrupt artefacts shouldn't break ingestion
                # — the MinHash stage still works on its own.
                self._embeddings = None
                self._embedding_qids = []

        self._loaded = True

    def _build_minhash_index_from_db(self) -> QuestionMinHashIndex:
        """Pull live questions from the DB and build a fresh index.

        Imports are local because importing models eagerly would force
        DB initialisation on every test that imports this module.
        """
        from models.database import init_db, Question

        init_db()
        live = list(Question.select().where(Question.status == "live"))
        return QuestionMinHashIndex(
            threshold=dedup_config.LSH_BUILD_THRESHOLD,
        ).build(live)

    # ── Core dedup decision ─────────────────────────────────────────────

    def find_dup_for(
        self,
        *,
        prompt: str,
        stimulus_content: str = "",
        options: Optional[List[str]] = None,
        log: bool = True,
        source: str = "",
    ) -> Optional[int]:
        """Return the qid of an existing duplicate, or ``None``.

        Parameters
        ----------
        prompt:
            Candidate question stem. Required.
        stimulus_content:
            Optional passage / chart body — used by both stages.
        options:
            Optional list of answer-choice texts (no labels). Order is
            preserved; we synthesise A/B/C/... for bi-encoder input.
        log:
            If True (default), append a structured JSON line to the
            decisions log. Pass ``log=False`` from tests / CI.
        source:
            Optional source tag (e.g. ``"agieval_lsat_lr"``) propagated
            into the decisions log.
        """
        self.build_or_load()
        query = _QueryRecord(prompt=prompt,
                             stimulus_content=stimulus_content,
                             options=options or [])

        scores: Dict[str, Optional[float]] = {
            "jaccard": None,
            "cosine": None,
            "ce_score": None,
        }
        candidate_payload = {
            "prompt": prompt,
            "stimulus_content": stimulus_content,
            "options": options or [],
        }

        # ── Stage 1 — MinHash/LSH ──────────────────────────────────────
        mh_match = self._minhash_match(query)
        if mh_match is not None:
            matched_qid, jaccard = mh_match
            scores["jaccard"] = jaccard
            if log:
                self.log_decision(
                    candidate_payload=candidate_payload,
                    decision="reject_minhash",
                    matched_qid=matched_qid,
                    scores=scores,
                    source=source,
                )
            return matched_qid

        # ── Stage 2 — embedding + cross-encoder ────────────────────────
        embed_match = self._embedding_match(query, scores)
        if embed_match is not None:
            matched_qid = embed_match
            if log:
                self.log_decision(
                    candidate_payload=candidate_payload,
                    decision="reject_embedding",
                    matched_qid=matched_qid,
                    scores=scores,
                    source=source,
                )
            return matched_qid

        # ── No duplicate ──────────────────────────────────────────────
        if log:
            self.log_decision(
                candidate_payload=candidate_payload,
                decision="accept",
                matched_qid=None,
                scores=scores,
                source=source,
            )
        return None

    def find_dup_for_question(self, question) -> Optional[int]:
        """Convenience wrapper for an in-memory Peewee ``Question`` row."""
        opts = []
        for opt in getattr(question, "options", []) or []:
            txt = getattr(opt, "option_text", "") or ""
            if txt:
                opts.append(txt)
        stim_content = ""
        stim = getattr(question, "stimulus", None)
        if stim is not None:
            stim_content = getattr(stim, "content", "") or ""
        return self.find_dup_for(
            prompt=getattr(question, "prompt", "") or "",
            stimulus_content=stim_content,
            options=opts,
            source=str(getattr(question, "source", "") or ""),
        )

    # ── Stage helpers ───────────────────────────────────────────────────

    def _minhash_match(self, query) -> Optional[Tuple[int, float]]:
        """Return ``(qid, jaccard)`` of the best LSH survivor, or None."""
        if self._minhash_index is None:
            return None
        # Pull survivors at the build threshold; post-filter by the tuned
        # accept threshold.
        candidates = self._minhash_index.find_candidates(
            query,
            top_k=20,
            exclude_shared_stimulus=False,
        )
        accept_threshold = dedup_config.LSH_THRESHOLD
        winners = [(qid, j) for (qid, j) in candidates if j >= accept_threshold]
        if not winners:
            return None
        winners.sort(key=lambda t: t[1], reverse=True)
        return winners[0]

    def _embedding_match(
        self,
        query,
        scores: Dict[str, Optional[float]],
    ) -> Optional[int]:
        """Run stage 2: cosine retrieval + cross-encoder rerank."""
        if self._embeddings is None or len(self._embedding_qids) == 0:
            return None

        # Bi-encoder: encode the query once.
        text = question_to_text(query)
        if not text:
            return None
        try:
            model = _get_bi_encoder()
        except Exception:
            return None
        try:
            query_emb = model.encode(
                [text],
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception:
            return None
        import numpy as np
        query_vec = np.asarray(query_emb[0], dtype=self._embeddings.dtype)

        cos_hits = find_paraphrase_candidates(
            query_vec,
            self._embeddings,
            self._embedding_qids,
            top_k=dedup_config.EMBEDDING_TOP_K,
            cosine_threshold=dedup_config.EMBEDDING_COSINE_THRESHOLD,
        )
        if not cos_hits:
            return None
        # Best cosine survivor — recorded for the audit log even if the
        # cross-encoder rejects.
        scores["cosine"] = cos_hits[0][1]

        # Cross-encoder rerank. Lazily instantiate the judge.
        if self._cross_encoder is None:
            self._cross_encoder = CrossEncoderJudge()

        # Need the existing question's text to feed the CE — pull from
        # DB once per qid and cache.
        survivor_texts: List[Tuple[int, str]] = []
        for qid, _cos in cos_hits:
            existing_text = self._fetch_question_text(int(qid))
            if existing_text:
                survivor_texts.append((int(qid), existing_text))

        if not survivor_texts:
            return None

        try:
            ce_scores = self._cross_encoder.judge_pairs_batch(
                [(text, et) for (_qid, et) in survivor_texts]
            )
        except Exception:
            return None

        ce_threshold = dedup_config.CROSS_ENCODER_PARAPHRASE_THRESHOLD
        accepted: List[Tuple[int, float]] = []
        for (qid, _existing), score in zip(survivor_texts, ce_scores):
            if score >= ce_threshold:
                accepted.append((qid, float(score)))

        if not accepted:
            scores["ce_score"] = max(ce_scores) if ce_scores else None
            return None

        accepted.sort(key=lambda t: t[1], reverse=True)
        scores["ce_score"] = accepted[0][1]
        return accepted[0][0]

    def _fetch_question_text(self, qid: int) -> str:
        """Cache the bi-encoder's canonical text for an existing qid."""
        if qid in self._embedding_text_cache:
            return self._embedding_text_cache[qid]
        try:
            from models.database import Question
            q = Question.get_or_none(Question.id == qid)
        except Exception:
            return ""
        if q is None:
            return ""
        text = question_to_text(q)
        self._embedding_text_cache[qid] = text
        return text

    # ── Decision log ────────────────────────────────────────────────────

    def log_decision(
        self,
        *,
        candidate_payload: dict,
        decision: str,
        matched_qid: Optional[int],
        scores: dict,
        source: str = "",
    ) -> None:
        """Append a structured JSON line to the decisions log.

        Schema::

            {
              "ts": ISO-8601 UTC timestamp,
              "source": str,
              "candidate_hash": sha1 prefix of the canonicalised prompt,
              "decision": "accept" | "reject_minhash" | "reject_embedding",
              "matched_qid": int | null,
              "jaccard": float | null,
              "cosine": float | null,
              "ce_score": float | null,
              "model_version_tuple": [...]   # EMBEDDING_VERSION_TUPLE
            }
        """
        ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        canonical = json.dumps(
            {
                "prompt": candidate_payload.get("prompt", ""),
                "stimulus_content": candidate_payload.get("stimulus_content", ""),
                "options": candidate_payload.get("options", []),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        cand_hash = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
        record = {
            "ts": ts,
            "source": source,
            "candidate_hash": cand_hash,
            "decision": decision,
            "matched_qid": matched_qid,
            "jaccard": scores.get("jaccard"),
            "cosine": scores.get("cosine"),
            "ce_score": scores.get("ce_score"),
            "model_version_tuple": list(dedup_config.EMBEDDING_VERSION_TUPLE),
        }
        self._decisions_log_path.parent.mkdir(parents=True, exist_ok=True)
        # Append atomically — JSONL is line-oriented so a partial write
        # would corrupt at most the last entry.
        with self._decisions_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ── Cleanup ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Release the cross-encoder weights. Idempotent.

        The bi-encoder is module-level (cached in
        ``embedding_stage._BI_ENCODER``) so we don't try to evict it
        here — a fresh ``DedupService`` will reuse it.
        """
        if self._cross_encoder is not None:
            ce = self._cross_encoder
            try:
                ce._model = None
            except Exception:
                pass
            self._cross_encoder = None


# ── Module-level singleton ──────────────────────────────────────────────

_DEDUP_SERVICE: Optional[DedupService] = None


def get_dedup_service() -> DedupService:
    """Return a process-local :class:`DedupService` instance.

    First call constructs the service AND triggers
    :meth:`DedupService.build_or_load` so subsequent ``find_dup_for``
    calls don't pay the index-build cost.
    """
    global _DEDUP_SERVICE
    if _DEDUP_SERVICE is None:
        svc = DedupService()
        # Eagerly load so the first ``find_dup_for`` call is fast.
        # A failure here (e.g. missing DB during a test that monkeypatches
        # the service) is non-fatal — the service is still usable; the
        # next call will retry build_or_load.
        try:
            svc.build_or_load()
        except Exception:
            pass
        _DEDUP_SERVICE = svc
    return _DEDUP_SERVICE


def reset_dedup_service() -> None:
    """Drop the cached singleton — used by tests that need a fresh instance."""
    global _DEDUP_SERVICE
    if _DEDUP_SERVICE is not None:
        try:
            _DEDUP_SERVICE.close()
        except Exception:
            pass
    _DEDUP_SERVICE = None
