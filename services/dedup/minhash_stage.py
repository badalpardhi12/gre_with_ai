"""Stage 1 of the dedup pipeline: MinHash/LSH candidate generation.

Reads ``Question`` rows (joined with ``Stimulus.content`` and
``QuestionOption.option_text``) and emits candidate near-duplicate pairs
``(qid_other, jaccard_estimate)`` for any query item.

Design notes (from implementation plan §181-188 + Phase 1.1 finding):
  * Stems are tokenised with stopword stripping — removes generic phrasing
    like "what is the value of" that creates spurious overlap.
  * Distractor text keeps stopwords — Phase 1.1 found that distractor
    wording carries strong dedup signal even when stems vary.
  * Shingles are 5-grams of joined-by-space tokens (configurable via
    ``services.dedup.config.SHINGLE_SIZE``).
  * MinHash uses ``num_perm=128``; LSH ``threshold`` is the tuned value
    in ``services.dedup.config.LSH_THRESHOLD`` (rewritten by
    ``scripts/tune_minhash_threshold.py``).

This module never writes to the DB. It is a pure read+hash transform.
"""

import re
from typing import Iterable, List, Optional, Set, Tuple

from datasketch import MinHash, MinHashLSH

from services.dedup import config as dedup_config

# ── Tokenisation ───────────────────────────────────────────────────────

# A small, in-tree English stopword list. Pulled from the standard NLTK list
# but trimmed to terms that genuinely contribute zero dedup signal in GRE
# stems (we keep "no", "not", "but", "if", "than" — those flip semantics).
_STOPWORDS = frozenset({
    "a", "an", "the",
    "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "doing", "done",
    "have", "has", "had", "having",
    "of", "in", "on", "at", "to", "for", "with", "from", "by", "into",
    "about", "as", "this", "that", "these", "those",
    "i", "you", "he", "she", "it", "we", "they", "them", "us", "him", "her",
    "my", "your", "his", "their", "our", "its",
    "and", "or",
    "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
    "there", "here", "then",
    "can", "could", "would", "should", "shall", "will", "may", "might", "must",
    "also", "such", "any", "some", "all", "each", "every",
})

# Token regex: word characters (Unicode letter/digit) or single math operators
# (+ - * / = < > ≤ ≥). Keeps numbers and inequality signs that GRE quant relies
# on; drops bare punctuation.
_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_]+|[+\-*/=<>]|≤|≥|≠|≈|×|÷"
)

# HTML tag stripper for prompts/options that ship with formatting markup.
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return _HTML_TAG_RE.sub(" ", text)


def tokenize(text: str, *, strip_stopwords_from_stem: bool = True) -> List[str]:
    """Lowercase, regex-tokenize, optional stopword strip.

    Parameters
    ----------
    text:
        Raw text (HTML markup is tolerated and stripped).
    strip_stopwords_from_stem:
        If True (default) drop tokens in ``_STOPWORDS``. Pass ``False`` for
        distractor text — distractors lose signal once stripped.
    """
    if not text:
        return []
    cleaned = _strip_html(text).lower()
    tokens = _TOKEN_RE.findall(cleaned)
    if strip_stopwords_from_stem:
        tokens = [t for t in tokens if t not in _STOPWORDS]
    return tokens


def shingles(tokens: List[str], k: int = 5) -> Set[str]:
    """Return the set of contiguous k-shingles, joined by a single space.

    For sequences shorter than k, the entire token list is returned as a
    single shingle so very short stems (e.g. just a TC blank) still produce
    at least one feature.
    """
    if k <= 0:
        return set()
    if len(tokens) <= k:
        if not tokens:
            return set()
        return {" ".join(tokens)}
    out: Set[str] = set()
    for i in range(len(tokens) - k + 1):
        out.add(" ".join(tokens[i:i + k]))
    return out


# ── Question → shingle bag ─────────────────────────────────────────────

def _stimulus_text(question) -> str:
    """Pull the stimulus body if the row references one. Returns "" if not."""
    stim = getattr(question, "stimulus", None)
    if stim is None:
        return ""
    return getattr(stim, "content", "") or ""


def _option_texts(question) -> List[str]:
    """Materialise the question's options as a list of strings.

    The Peewee backref ``question.options`` returns a SelectQuery, so we
    iterate it eagerly. Empty list for question types without options
    (numeric_entry, awa_issue).
    """
    out: List[str] = []
    for opt in getattr(question, "options", []) or []:
        txt = getattr(opt, "option_text", None)
        if txt:
            out.append(txt)
    return out


def question_shingles(question, k: Optional[int] = None) -> Set[str]:
    """Combined shingle set for a question.

    Stem (prompt + stimulus content) is tokenised with stopwords stripped;
    options keep stopwords. Both share the same k-shingle window so they
    contribute uniformly to the resulting MinHash.
    """
    if k is None:
        k = dedup_config.SHINGLE_SIZE

    stem_text = (getattr(question, "prompt", "") or "") + " " + _stimulus_text(question)
    stem_tokens = tokenize(stem_text, strip_stopwords_from_stem=True)
    bag = shingles(stem_tokens, k=k)

    for opt_text in _option_texts(question):
        opt_tokens = tokenize(opt_text, strip_stopwords_from_stem=False)
        bag |= shingles(opt_tokens, k=k)

    return bag


def minhash_for_shingles(shingle_set: Set[str],
                         num_perm: Optional[int] = None) -> MinHash:
    """Convert a set of string shingles to a datasketch ``MinHash``.

    Encodes shingles as utf-8 because ``MinHash.update`` wants bytes.
    """
    if num_perm is None:
        num_perm = dedup_config.LSH_NUM_PERM
    m = MinHash(num_perm=num_perm)
    for s in shingle_set:
        m.update(s.encode("utf-8"))
    return m


def minhash_for_question(question,
                         k: Optional[int] = None,
                         num_perm: Optional[int] = None) -> MinHash:
    """Convenience wrapper: shingles → MinHash for a question row."""
    return minhash_for_shingles(question_shingles(question, k=k), num_perm=num_perm)


# ── LSH index ──────────────────────────────────────────────────────────

class QuestionMinHashIndex:
    """Build-and-query wrapper around ``datasketch.MinHashLSH`` for questions.

    Usage::

        idx = QuestionMinHashIndex(threshold=0.7).build(Question.select().where(...))
        for qid_other, jaccard in idx.find_candidates(query_q, top_k=20):
            ...

    Implementation detail: we keep the raw ``MinHash`` per qid in
    ``self._minhashes`` because ``MinHashLSH.query`` only returns keys, and
    we want to surface the actual jaccard estimate per candidate (callers
    use it for downstream confidence scoring).

    Shared-stimulus filter: RC and DI questions reuse a ``Stimulus`` row,
    so two qids that point to the same stimulus_id can have very high
    fingerprint overlap (the passage dominates) without being duplicates
    — they're sibling questions of the same passage. The index records
    each qid's stimulus_id at build time so ``find_candidates`` can drop
    siblings by default. Set ``exclude_shared_stimulus=False`` to opt out.
    """

    def __init__(self,
                 threshold: Optional[float] = None,
                 num_perm: Optional[int] = None,
                 k: Optional[int] = None):
        self.threshold = (
            threshold if threshold is not None else dedup_config.LSH_THRESHOLD
        )
        self.num_perm = (
            num_perm if num_perm is not None else dedup_config.LSH_NUM_PERM
        )
        self.k = k if k is not None else dedup_config.SHINGLE_SIZE
        self.lsh: Optional[MinHashLSH] = None
        self._minhashes = {}    # qid -> MinHash
        self._stimulus_ids = {}  # qid -> stimulus_id (or None)

    def build(self, questions: Iterable) -> "QuestionMinHashIndex":
        """Index an iterable of ``Question`` rows. Idempotent — replaces prior state."""
        self.lsh = MinHashLSH(threshold=self.threshold, num_perm=self.num_perm)
        self._minhashes = {}
        self._stimulus_ids = {}
        for q in questions:
            mh = minhash_for_question(q, k=self.k, num_perm=self.num_perm)
            qid = int(q.id)
            self._minhashes[qid] = mh
            stim = getattr(q, "stimulus", None)
            self._stimulus_ids[qid] = int(stim.id) if stim is not None else None
            # ``MinHashLSH`` rejects duplicate keys with a ValueError, so the
            # caller is expected to feed deduplicated rows. Coerce to str
            # because LSH keys are stored as bytes anyway.
            self.lsh.insert(str(qid), mh)
        return self

    def get_stimulus_id(self, qid: int) -> Optional[int]:
        return self._stimulus_ids.get(int(qid))

    def get_minhash(self, qid: int) -> Optional[MinHash]:
        return self._minhashes.get(int(qid))

    def find_candidates(self,
                        question_record,
                        *,
                        top_k: int = 50,
                        exclude_shared_stimulus: bool = True) -> List[Tuple[int, float]]:
        """Return ``(qid_other, jaccard_estimate)`` candidates for a query.

        The query item is excluded from its own results. Output is sorted
        by descending jaccard estimate, capped at ``top_k``.

        ``exclude_shared_stimulus`` (default True) drops candidates that
        share a stimulus_id with the query — these are sibling RC/DI
        questions of the same passage/chart, never duplicates.
        """
        if self.lsh is None:
            raise RuntimeError("QuestionMinHashIndex not built; call .build() first")

        query_id = int(question_record.id) if hasattr(question_record, "id") else None
        query_stim = None
        if exclude_shared_stimulus:
            if query_id is not None and query_id in self._stimulus_ids:
                query_stim = self._stimulus_ids.get(query_id)
            else:
                stim = getattr(question_record, "stimulus", None)
                query_stim = int(stim.id) if stim is not None else None

        # Reuse the cached MinHash if the query was indexed; otherwise
        # compute fresh (callers may legitimately query an item not in the
        # index, e.g. for ingest-time dedup against the existing bank).
        if query_id is not None and query_id in self._minhashes:
            query_mh = self._minhashes[query_id]
        else:
            query_mh = minhash_for_question(
                question_record, k=self.k, num_perm=self.num_perm,
            )

        raw_keys = self.lsh.query(query_mh)
        results: List[Tuple[int, float]] = []
        for key in raw_keys:
            other_id = int(key)
            if query_id is not None and other_id == query_id:
                continue
            if exclude_shared_stimulus and query_stim is not None:
                other_stim = self._stimulus_ids.get(other_id)
                if other_stim is not None and other_stim == query_stim:
                    continue
            other_mh = self._minhashes.get(other_id)
            if other_mh is None:  # pragma: no cover — defensive
                continue
            results.append((other_id, query_mh.jaccard(other_mh)))

        results.sort(key=lambda t: t[1], reverse=True)
        if top_k is not None and len(results) > top_k:
            results = results[:top_k]
        return results

    # ── Pairwise jaccard helper (used by the tuning sweep + tests) ──────

    def pairwise_jaccard(self, qid_a: int, qid_b: int) -> Optional[float]:
        a = self._minhashes.get(int(qid_a))
        b = self._minhashes.get(int(qid_b))
        if a is None or b is None:
            return None
        return a.jaccard(b)


# ── Module-level functional API (mirrors spec wording) ─────────────────

def build(questions: Iterable,
          threshold: float,
          num_perm: int = 128) -> QuestionMinHashIndex:
    """Build a ``QuestionMinHashIndex`` at the given threshold.

    Returns the index (the caller may also reach the underlying LSH via
    ``index.lsh`` if they want raw keys).
    """
    return QuestionMinHashIndex(
        threshold=threshold, num_perm=num_perm,
    ).build(questions)


def find_candidates(question_record,
                    index: QuestionMinHashIndex,
                    *,
                    top_k: int = 50,
                    exclude_shared_stimulus: bool = True) -> List[Tuple[int, float]]:
    """Module-level alias for ``QuestionMinHashIndex.find_candidates``."""
    return index.find_candidates(
        question_record,
        top_k=top_k,
        exclude_shared_stimulus=exclude_shared_stimulus,
    )
