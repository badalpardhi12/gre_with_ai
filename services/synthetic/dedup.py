"""
Paraphrase deduplication for synthetic-pipeline batches.

Plan §6: after generation, embed each item's stem and reject if cosine
similarity ≥ 0.92 against any item in the same subtopic's last K
items. We don't ship a sentence-embedding dependency by default
(`sentence-transformers` adds ~500MB of weights), so this module
provides two backends:

- `EmbeddingDeduper`: real cosine-distance dedup; constructed lazily
  when `sentence-transformers` is importable. Falls back gracefully.
- `JaccardDeduper`: token-set Jaccard fallback. Coarse but
  zero-dependency; useful for unit tests and as a safety net.

Both implement `is_duplicate(stem) -> bool` and `register(stem) -> None`.
The orchestrator calls `is_duplicate` before judging and `register`
after persistence.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Protocol, Tuple


class Deduper(Protocol):
    def is_duplicate(self, stem: str, *, subtopic: str = "") -> Tuple[bool, float]: ...
    def register(self, stem: str, *, subtopic: str = "") -> None: ...
    def reset(self) -> None: ...


def _tokenize(text: str) -> List[str]:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union


@dataclass
class JaccardDeduper:
    """Fallback dedup using token-set Jaccard similarity.

    `threshold` is the minimum Jaccard ratio that counts as a
    duplicate. 0.85 is intentionally permissive (paraphrases share
    most tokens) and tuned for *single-stem* comparisons; for full
    item dedup including options/explanation, use a tighter threshold.

    `window_per_subtopic` caps how many recent items to remember per
    subtopic (default 200, matching plan §6).
    """
    threshold: float = 0.85
    window_per_subtopic: int = 200
    # subtopic -> deque of token lists from recent items
    _history: Dict[str, Deque[List[str]]] = field(
        default_factory=dict, init=False, repr=False,
    )

    def is_duplicate(self, stem: str, *, subtopic: str = "") -> Tuple[bool, float]:
        tokens = _tokenize(stem)
        if not tokens:
            return False, 0.0
        history = self._history.get(subtopic or "_default", deque())
        max_sim = 0.0
        for prior in history:
            sim = _jaccard(tokens, prior)
            if sim > max_sim:
                max_sim = sim
            if sim >= self.threshold:
                return True, sim
        return False, max_sim

    def register(self, stem: str, *, subtopic: str = "") -> None:
        tokens = _tokenize(stem)
        if not tokens:
            return
        key = subtopic or "_default"
        bucket = self._history.setdefault(
            key, deque(maxlen=self.window_per_subtopic)
        )
        bucket.append(tokens)

    def reset(self) -> None:
        self._history.clear()


@dataclass
class EmbeddingDeduper:
    """Cosine-similarity dedup using sentence-transformers.

    Constructed lazily; if `sentence_transformers` isn't available
    `available()` returns False and the orchestrator falls back to
    JaccardDeduper.
    """
    threshold: float = 0.92
    window_per_subtopic: int = 200
    model_name: str = "all-MiniLM-L6-v2"
    _model: Optional[object] = field(default=None, init=False, repr=False)
    _history: Dict[str, Deque[Tuple[List[float], List[str]]]] = field(
        default_factory=dict, init=False, repr=False,
    )

    @staticmethod
    def available() -> bool:
        try:
            import sentence_transformers  # noqa: F401
            return True
        except ImportError:
            return False

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        return self._model

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    def _embed(self, stem: str) -> List[float]:
        model = self._load()
        return list(model.encode([stem])[0])

    def is_duplicate(self, stem: str, *, subtopic: str = "") -> Tuple[bool, float]:
        if not stem:
            return False, 0.0
        history = self._history.get(subtopic or "_default", deque())
        if not history:
            return False, 0.0
        emb = self._embed(stem)
        max_sim = 0.0
        for prior_emb, _ in history:
            sim = self._cosine(emb, prior_emb)
            if sim > max_sim:
                max_sim = sim
            if sim >= self.threshold:
                return True, sim
        return False, max_sim

    def register(self, stem: str, *, subtopic: str = "") -> None:
        if not stem:
            return
        emb = self._embed(stem)
        key = subtopic or "_default"
        bucket = self._history.setdefault(
            key, deque(maxlen=self.window_per_subtopic)
        )
        bucket.append((emb, _tokenize(stem)))

    def reset(self) -> None:
        self._history.clear()


def make_default_deduper() -> Deduper:
    """Construct the best-available deduper.

    Uses embeddings when `sentence-transformers` is installed; falls
    back to Jaccard otherwise. The fallback is good enough for our
    in-batch dedup use case (we mostly want to catch obvious
    repetition), but log a one-shot warning so the operator notices.
    """
    if EmbeddingDeduper.available():
        return EmbeddingDeduper()
    # TODO: when `sentence-transformers` is added to requirements,
    # promote this to the default and drop JaccardDeduper. For now,
    # Jaccard is the runtime default.
    return JaccardDeduper()
