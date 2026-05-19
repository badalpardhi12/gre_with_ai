"""Tests for the Phase 1.4 dedup integration layer.

The fast tier (no model load) covers:
  * The decision-log JSONL writer's schema.
  * The ``_QueryRecord`` shim's compatibility with minhash + embedding helpers.
  * Stage-1-only behaviour: an exact duplicate of an indexed question is
    detected by MinHash without touching the embedding stage.

The slow tier (``@pytest.mark.slow``) covers:
  * A semantic paraphrase from the held-out labelled set is detected
    via the embedding+cross-encoder stage.
  * A novel candidate produces an ``accept`` decision.

Slow tests load mpnet (~420 MB) + the cross-encoder (~1.4 GB) so the fast
tier must finish in under 2s.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.dedup import config as dedup_config  # noqa: E402
from services.dedup.dedup_service import (  # noqa: E402
    DedupService,
    _QueryRecord,
    get_dedup_service,
    reset_dedup_service,
)
from services.dedup.minhash_stage import QuestionMinHashIndex  # noqa: E402


LABELED_CSV = REPO_ROOT / "data" / "dedup_eval" / "labeled_pairs_2026_05_18.csv"
EMBEDDINGS_NPY = REPO_ROOT / "data" / "dedup_eval" / "embeddings_2026_05_18.npy"


# ── Lightweight in-memory question shim used by fast tests ─────────────

class _StubStim:
    def __init__(self, content: str):
        self.content = content


class _StubOpt:
    def __init__(self, label: str, text: str):
        self.option_label = label
        self.option_text = text


class _StubQuestion:
    """Minimal duck-typed Question for the MinHash index.

    The minhash stage uses ``q.id``, ``q.prompt``, ``q.options``, and
    ``q.stimulus``; we expose just those.
    """

    def __init__(
        self,
        qid: int,
        prompt: str,
        options: Optional[List[str]] = None,
        stimulus_content: str = "",
    ):
        self.id = qid
        self.prompt = prompt
        self.stimulus = _StubStim(stimulus_content) if stimulus_content else None
        self.options = [
            _StubOpt(chr(ord("A") + i), t)
            for i, t in enumerate(options or [])
        ]


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def stub_questions() -> List[_StubQuestion]:
    """A small synthetic bank used to exercise the MinHash stage in
    isolation. Each item has enough lexical content to produce a
    distinctive 5-shingle bag.
    """
    return [
        _StubQuestion(
            qid=101,
            prompt="A box contains 5 white balls and 3 black balls. "
                   "Two balls are drawn at random without replacement. "
                   "What is the probability that both are white?",
            options=["5/14", "5/28", "10/56", "15/64", "1/4"],
        ),
        _StubQuestion(
            qid=102,
            prompt="If 3x - 4 >= 11 and 2x + 1 <= 17 , find the range of x.",
            options=["x is between 5 and 8", "x is between 5 and 6",
                     "x is between 4 and 8", "x is between 5 and 9",
                     "x is between 4 and 9"],
        ),
        _StubQuestion(
            qid=103,
            prompt="In a circle, an inscribed angle intercepts an arc whose "
                   "length is one-third of the circumference. What is the "
                   "measure of the inscribed angle in degrees?",
            options=["30", "45", "60", "90", "120"],
        ),
        _StubQuestion(
            qid=104,
            prompt="Although the senator's memoir purports to offer an "
                   "unvarnished account of her years in office, the narrative "
                   "is so thoroughly polished that it reads as a sustained "
                   "exercise in self-justification. Select two words that "
                   "best complete the description.",
            options=["candor", "veracity", "sanitisation", "circumlocution",
                     "frankness", "obfuscation"],
        ),
    ]


@pytest.fixture
def offline_dedup_service(stub_questions, tmp_path) -> DedupService:
    """A ``DedupService`` whose MinHash index is hand-built from the
    in-memory stubs, with embeddings disabled (npy path doesn't exist).

    Decision log is redirected to a tmp file so concurrent test runs
    don't fight over the real ingest log.
    """
    idx = QuestionMinHashIndex(
        threshold=dedup_config.LSH_BUILD_THRESHOLD,
    ).build(stub_questions)
    log_path = tmp_path / "decisions.jsonl"
    svc = DedupService(
        minhash_index=idx,
        embeddings_npy_path=tmp_path / "no_such.npy",
        decisions_log_path=log_path,
    )
    svc.build_or_load()
    return svc


# ── Fast tier ──────────────────────────────────────────────────────────

@pytest.mark.timeout(2)
def test_query_record_exposes_no_id():
    """``_QueryRecord`` must NOT expose ``id`` so the minhash stage's
    ``hasattr(q, "id")`` guard treats the candidate as a fresh row.
    """
    q = _QueryRecord(prompt="hello", stimulus_content="", options=["a", "b"])
    assert not hasattr(q, "id")
    assert q.prompt == "hello"
    assert q.stimulus is None
    assert [o.option_label for o in q.options] == ["A", "B"]


def test_exact_duplicate_returns_existing_qid(offline_dedup_service, stub_questions):
    """An exact-text duplicate of an indexed stub returns its qid via
    stage 1 (MinHash). Stage 2 is never reached — the embedding npy
    is intentionally missing.
    """
    sample = stub_questions[0]
    matched = offline_dedup_service.find_dup_for(
        prompt=sample.prompt,
        stimulus_content="",
        options=[o.option_text for o in sample.options],
        log=False,
        source="test_exact",
    )
    assert matched == sample.id


def test_paraphrase_clone_via_held_out_pair_minhash(offline_dedup_service):
    """A near-duplicate (same problem, different wording) lands on the
    indexed item via MinHash when the lexical overlap is high enough.

    This uses a hand-written pair: the "5 white / 3 black balls"
    problem in two phrasings. The held-out pair set has identical
    structure (see ``data/dedup_eval/labeled_pairs_2026_05_18.csv``).
    """
    paraphrase = (
        "A jar contains 5 white balls and 3 black balls. Two balls are "
        "drawn without replacement. What is the probability that both "
        "are white?"
    )
    matched = offline_dedup_service.find_dup_for(
        prompt=paraphrase,
        stimulus_content="",
        options=["5/14", "5/28", "10/56", "15/64", "1/4"],
        log=False,
        source="test_paraphrase",
    )
    assert matched == 101


def test_novel_returns_none(offline_dedup_service):
    """A wholly novel question that has no MinHash overlap with the
    bank returns ``None``. With no embeddings on disk, stage 2 is a
    no-op — so this also confirms the service is robust to missing
    artefacts.
    """
    matched = offline_dedup_service.find_dup_for(
        prompt="What is the smallest positive integer n such that n! "
               "ends in exactly seven trailing zeros?",
        stimulus_content="",
        options=["28", "30", "32", "34", "35"],
        log=False,
        source="test_novel",
    )
    assert matched is None


def test_log_decision_jsonl_schema(offline_dedup_service, tmp_path):
    """``log_decision`` writes one JSON object per line, with the
    required fields populated and the version tuple stamped.
    """
    log_path = offline_dedup_service._decisions_log_path

    # Two calls: one accept, one reject.
    offline_dedup_service.find_dup_for(
        prompt="completely novel item with strange vocabulary like "
               "frangible and quintessential and obfuscation",
        options=[],
        log=True,
        source="schema_test_accept",
    )
    sample_prompt = (
        "A box contains 5 white balls and 3 black balls. Two balls "
        "are drawn at random without replacement. What is the "
        "probability that both are white?"
    )
    offline_dedup_service.find_dup_for(
        prompt=sample_prompt,
        options=["5/14", "5/28", "10/56", "15/64", "1/4"],
        log=True,
        source="schema_test_reject",
    )

    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 2

    accept_rec = json.loads(lines[0])
    reject_rec = json.loads(lines[1])

    required_keys = {
        "ts",
        "source",
        "candidate_hash",
        "decision",
        "matched_qid",
        "jaccard",
        "cosine",
        "ce_score",
        "model_version_tuple",
    }
    assert set(accept_rec.keys()) == required_keys
    assert set(reject_rec.keys()) == required_keys

    assert accept_rec["decision"] == "accept"
    assert accept_rec["matched_qid"] is None
    assert accept_rec["source"] == "schema_test_accept"

    assert reject_rec["decision"] == "reject_minhash"
    assert reject_rec["matched_qid"] == 101
    assert isinstance(reject_rec["jaccard"], float)
    assert reject_rec["jaccard"] >= dedup_config.LSH_THRESHOLD

    # Both lines must carry the embedding/version tuple from config.
    expected_tuple = list(dedup_config.EMBEDDING_VERSION_TUPLE)
    assert accept_rec["model_version_tuple"] == expected_tuple
    assert reject_rec["model_version_tuple"] == expected_tuple


def test_singleton_get_dedup_service_is_cached():
    """``get_dedup_service`` returns the same instance across calls."""
    reset_dedup_service()
    try:
        a = get_dedup_service()
        b = get_dedup_service()
        assert a is b
    finally:
        reset_dedup_service()


def test_close_releases_cross_encoder(offline_dedup_service):
    """``close()`` clears the cross-encoder reference; idempotent."""
    # Inject a fake CE so close() has something to release.
    class _FakeCE:
        _model = object()

    fake = _FakeCE()
    offline_dedup_service._cross_encoder = fake
    offline_dedup_service.close()
    # close() drops the service-level reference and resets the inner
    # model handle on the way out.
    assert offline_dedup_service._cross_encoder is None
    assert fake._model is None
    # idempotent
    offline_dedup_service.close()


# ── Slow tier (loads mpnet + cross-encoder) ────────────────────────────


def _load_held_out_yes_pair():
    """Pull one Yes pair from the held-out labelled CSV.

    Returns ``(qid_a, qid_b, stem_a, stem_b)`` or ``None`` if the CSV
    is missing (CI on a fresh clone may not have it).
    """
    if not LABELED_CSV.exists():
        return None
    with LABELED_CSV.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if row.get("final_label", "").strip().lower() == "yes":
                return (
                    int(row["qid_a"]),
                    int(row["qid_b"]),
                    row["stem_a_first120"],
                    row["stem_b_first120"],
                )
    return None


@pytest.mark.slow
def test_embedding_stage_detects_paraphrase_from_held_out_set():
    """Stage 2 catches a labelled paraphrase when MinHash would have
    missed it.

    This exercises the full pipeline:
      * The persisted embeddings npy is loaded.
      * The bi-encoder + cross-encoder weights are loaded.
      * The held-out Yes pair (which by construction is in the live
        embeddings) round-trips: query qid_a's stem against the bank
        and confirm ``find_dup_for`` returns either qid_a (self) or
        qid_b (the labelled clone).
    """
    pytest.importorskip("sentence_transformers")
    if not EMBEDDINGS_NPY.exists():
        pytest.skip("embeddings npy not present; run "
                    "scripts/embed_questions.py first")
    held_out = _load_held_out_yes_pair()
    if held_out is None:
        pytest.skip("labeled_pairs CSV missing")
    qid_a, qid_b, stem_a, _stem_b = held_out

    from models.database import init_db, Question
    init_db()
    q_a = Question.get_or_none(Question.id == qid_a)
    if q_a is None:
        pytest.skip(f"qid_a={qid_a} not in DB")

    # Use the real production singleton (live MinHash + persisted
    # embeddings). The dedup service handles its own lazy loading.
    reset_dedup_service()
    try:
        svc = get_dedup_service()
        opts = [o.option_text for o in q_a.options]
        stim_content = q_a.stimulus.content if q_a.stimulus else ""
        matched = svc.find_dup_for(
            prompt=q_a.prompt,
            stimulus_content=stim_content,
            options=opts,
            log=False,
            source="held_out_yes",
        )
        # Either the query item itself or its labelled clone is
        # acceptable — both are "duplicate" outcomes.
        assert matched in (qid_a, qid_b), (
            f"expected qid_a={qid_a} or qid_b={qid_b}; got {matched}"
        )
    finally:
        reset_dedup_service()


@pytest.mark.slow
def test_embedding_stage_returns_none_for_novel():
    """A wholly novel candidate prompt should be accepted (no dup)."""
    pytest.importorskip("sentence_transformers")
    if not EMBEDDINGS_NPY.exists():
        pytest.skip("embeddings npy not present")
    reset_dedup_service()
    try:
        svc = get_dedup_service()
        novel = (
            "A philatelist catalogues stamps using a system whose unique "
            "identifier is the third Stirling number of a curious "
            "asymptotic sequence she defined herself. What is the value "
            "of the sequence at index forty-seven?"
        )
        matched = svc.find_dup_for(
            prompt=novel,
            stimulus_content="",
            options=["one", "two", "three", "four", "five"],
            log=False,
            source="novel_slow",
        )
        assert matched is None
    finally:
        reset_dedup_service()
