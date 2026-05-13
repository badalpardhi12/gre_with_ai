"""
Tests for the contextual vocabulary generator (P3.S2).

Covers:
  1. VocabContextItem schema roundtrip (insert → query → field types).
  2. generate_context_item caches by (word, difficulty, prompt_version).
  3. get_or_generate returns cached on second call without re-calling LLM.
  4. batch_generate respects max_n.
  5. Unique (word, difficulty, prompt_version) constraint.
  6. Payload validation rejects malformed LLM output.
  7. Graceful fallback on LLM exception.
  8. Prompt-version bump produces a new row rather than colliding.

All LLM calls are mocked via ``unittest.mock.patch`` — no test hits the
network. The ``temp_db`` fixture (tests/conftest.py) provides a fresh SQLite
DB per test.
"""
import json
from unittest.mock import MagicMock, patch

import pytest


_FAKE_LLM_PAYLOAD = {
    "passage": (
        "The lecturer's assessment was so perfunctory that students suspected "
        "she had not read their essays at all. She moved through the stack in "
        "under ten minutes, each paper receiving the same two-word margin note "
        "regardless of its content. The department chair later acknowledged "
        "that the volume of submissions had overwhelmed the original plan for "
        "individualized feedback, and that the shortcut represented a "
        "compromise rather than a considered judgment. Even so, the students "
        "were justified in their frustration: a grade ought to rest on more "
        "than a glance, and the institution's reputation for rigor depended "
        "on distinguishing careful evaluation from rote acknowledgement. The "
        "episode prompted a review of grading workloads and a renewed "
        "commitment to substantive written commentary."
    ),
    "question": (
        "In the passage, the word 'perfunctory' most nearly means:"
    ),
    "correct_answer": "performed without care or thoroughness",
    "distractors": [
        "exceedingly detailed and exhaustive",
        "delivered in a harsh or scolding tone",
        "reserved for only the top performers",
    ],
}


def _mock_llm_service(payload=None, should_raise=None):
    """Build a MagicMock that stands in for ``services.llm_service.llm_service``.

    If ``should_raise`` is given, ``generate_json`` raises it. Otherwise it
    returns ``payload`` (defaulting to ``_FAKE_LLM_PAYLOAD``). ``get_current_config``
    returns a plausible model string.
    """
    mock = MagicMock()
    if should_raise is not None:
        mock.generate_json.side_effect = should_raise
    else:
        mock.generate_json.return_value = payload or _FAKE_LLM_PAYLOAD
    mock.get_current_config.return_value = {"model": "anthropic/claude-opus-4"}
    return mock


# ── 1. schema roundtrip ────────────────────────────────────────────────


def test_vocab_context_item_schema_roundtrip(temp_db):
    from models.database import VocabContextItem

    row = VocabContextItem.create(
        word="perfunctory",
        difficulty_tier="mid",
        passage_text="A sample passage.",
        question_text="What does the word mean?",
        correct_answer="performed without care",
        distractors=json.dumps(["a", "b", "c"]),
        llm_model="anthropic/claude-opus-4",
        prompt_version=1,
    )
    fetched = VocabContextItem.get(VocabContextItem.id == row.id)
    assert fetched.word == "perfunctory"
    assert fetched.difficulty_tier == "mid"
    assert fetched.passage_text == "A sample passage."
    assert fetched.question_text == "What does the word mean?"
    assert fetched.correct_answer == "performed without care"
    assert fetched.get_distractors() == ["a", "b", "c"]
    assert fetched.prompt_version == 1
    # get_options returns correct first then 3 distractors
    assert fetched.get_options() == [
        "performed without care", "a", "b", "c",
    ]


def test_vocab_context_item_unique_per_word_tier_version(temp_db):
    """(word, difficulty_tier, prompt_version) is UNIQUE; a second insert
    for the same triple fails."""
    from peewee import IntegrityError
    from models.database import VocabContextItem

    VocabContextItem.create(
        word="perfunctory", difficulty_tier="mid",
        passage_text="p1", question_text="q1",
        correct_answer="a1", distractors="[]",
        llm_model="m", prompt_version=1,
    )
    with pytest.raises(IntegrityError):
        VocabContextItem.create(
            word="perfunctory", difficulty_tier="mid",
            passage_text="p2", question_text="q2",
            correct_answer="a2", distractors="[]",
            llm_model="m", prompt_version=1,
        )


# ── 2. generate_context_item caching ───────────────────────────────────


def test_generate_context_item_caches_by_triple(temp_db):
    """First call invokes the LLM and persists a row; a second call for
    the same (word, difficulty, prompt_version) returns the cached row
    without re-calling the LLM."""
    from services import vocab_context_gen
    from models.database import VocabContextItem

    mock = _mock_llm_service()
    with patch.object(vocab_context_gen, "PROMPT_VERSION", 1), \
            patch("services.llm_service.llm_service", mock):
        first = vocab_context_gen.generate_context_item("perfunctory", "mid")
        assert first is not None
        assert mock.generate_json.call_count == 1

        second = vocab_context_gen.generate_context_item("perfunctory", "mid")
        assert second is not None
        assert second.id == first.id
        # Crucially: the LLM was NOT called again.
        assert mock.generate_json.call_count == 1

    # Only one row in the DB despite two generate calls.
    assert VocabContextItem.select().count() == 1


def test_generate_context_item_writes_expected_fields(temp_db):
    from services import vocab_context_gen
    from models.database import VocabContextItem

    mock = _mock_llm_service()
    with patch("services.llm_service.llm_service", mock):
        row = vocab_context_gen.generate_context_item("perfunctory", "mid")

    assert row is not None
    fetched = VocabContextItem.get_by_id(row.id)
    assert fetched.word == "perfunctory"
    assert fetched.difficulty_tier == "mid"
    assert fetched.passage_text.startswith("The lecturer's assessment")
    assert fetched.correct_answer == "performed without care or thoroughness"
    assert fetched.get_distractors() == _FAKE_LLM_PAYLOAD["distractors"]
    assert fetched.llm_model == "anthropic/claude-opus-4"
    assert fetched.prompt_version == vocab_context_gen.PROMPT_VERSION


def test_generate_context_item_differentiates_by_difficulty(temp_db):
    """Different difficulty tiers for the same word produce different rows
    (and both invoke the LLM)."""
    from services import vocab_context_gen
    from models.database import VocabContextItem

    mock = _mock_llm_service()
    with patch("services.llm_service.llm_service", mock):
        easy = vocab_context_gen.generate_context_item("perfunctory", "easy")
        hard = vocab_context_gen.generate_context_item("perfunctory", "hard")

    assert easy is not None and hard is not None
    assert easy.id != hard.id
    assert mock.generate_json.call_count == 2
    assert VocabContextItem.select().count() == 2


def test_prompt_version_bump_creates_new_row(temp_db):
    """Bumping prompt_version invalidates the cache for the same (word, tier)."""
    from services import vocab_context_gen
    from models.database import VocabContextItem

    mock = _mock_llm_service()
    with patch("services.llm_service.llm_service", mock):
        v1 = vocab_context_gen.generate_context_item(
            "perfunctory", "mid", prompt_version=1,
        )
        v2 = vocab_context_gen.generate_context_item(
            "perfunctory", "mid", prompt_version=2,
        )

    assert v1 is not None and v2 is not None
    assert v1.id != v2.id
    assert mock.generate_json.call_count == 2
    assert VocabContextItem.select().count() == 2


# ── 3. get_or_generate ────────────────────────────────────────────────


def test_get_or_generate_reuses_cache(temp_db):
    """get_or_generate is a convenience wrapper; second call hits cache."""
    from services import vocab_context_gen

    mock = _mock_llm_service()
    with patch("services.llm_service.llm_service", mock):
        a = vocab_context_gen.get_or_generate("perfunctory", "mid")
        b = vocab_context_gen.get_or_generate("perfunctory", "mid")

    assert a is not None and b is not None
    assert a.id == b.id
    assert mock.generate_json.call_count == 1


# ── 4. batch_generate ────────────────────────────────────────────────


def test_batch_generate_respects_max_n(temp_db):
    """batch_generate stops at ``max_n`` even if more words are passed."""
    from services import vocab_context_gen
    from models.database import VocabContextItem

    mock = _mock_llm_service()
    words = [f"word{i}" for i in range(10)]
    with patch("services.llm_service.llm_service", mock):
        out = vocab_context_gen.batch_generate(words, difficulty="mid", max_n=3)

    assert len(out) == 3
    assert mock.generate_json.call_count == 3
    assert VocabContextItem.select().count() == 3


def test_batch_generate_skips_failed_generations(temp_db):
    """Words whose LLM call raises are silently skipped; the batch returns
    only successful rows."""
    from services import vocab_context_gen
    from models.database import VocabContextItem

    # Return a valid payload for even indices, raise for odd indices.
    call_counter = {"n": 0}

    def _side_effect(*_, **__):
        call_counter["n"] += 1
        if call_counter["n"] % 2 == 0:
            raise RuntimeError("simulated LLM failure")
        return _FAKE_LLM_PAYLOAD

    mock = MagicMock()
    mock.generate_json.side_effect = _side_effect
    mock.get_current_config.return_value = {"model": "mock"}

    words = ["alpha", "beta", "gamma", "delta"]
    with patch("services.llm_service.llm_service", mock):
        out = vocab_context_gen.batch_generate(words, max_n=4)

    # calls 1,3 succeed (alpha, gamma); 2,4 fail (beta, delta).
    assert len(out) == 2
    assert {row.word for row in out} == {"alpha", "gamma"}
    assert VocabContextItem.select().count() == 2


def test_batch_generate_handles_empty_input(temp_db):
    from services import vocab_context_gen

    mock = _mock_llm_service()
    with patch("services.llm_service.llm_service", mock):
        out = vocab_context_gen.batch_generate([], max_n=20)

    assert out == []
    assert mock.generate_json.call_count == 0


# ── 5. error handling ────────────────────────────────────────────────


def test_generate_context_item_returns_none_on_llm_exception(temp_db):
    """If the LLM call raises (no API key, network block), we log and
    return None — never insert a malformed row."""
    from services import vocab_context_gen
    from models.database import VocabContextItem

    mock = _mock_llm_service(should_raise=RuntimeError("no api key"))
    with patch("services.llm_service.llm_service", mock):
        row = vocab_context_gen.generate_context_item("perfunctory", "mid")

    assert row is None
    assert VocabContextItem.select().count() == 0


@pytest.mark.parametrize("bad_payload", [
    {"passage": "p", "question": "q", "correct_answer": "a"},                    # missing distractors
    {"passage": "", "question": "q", "correct_answer": "a", "distractors": ["x", "y", "z"]},  # empty passage
    {"passage": "p", "question": "q", "correct_answer": "a", "distractors": ["x", "y"]},     # only 2 distractors
    {"passage": "p", "question": "q", "correct_answer": "a", "distractors": ["x", "", "z"]}, # empty distractor
    "not even a dict",
])
def test_generate_context_item_rejects_invalid_payload(temp_db, bad_payload):
    from services import vocab_context_gen
    from models.database import VocabContextItem

    mock = _mock_llm_service(payload=bad_payload)
    with patch("services.llm_service.llm_service", mock):
        row = vocab_context_gen.generate_context_item("perfunctory", "mid")

    assert row is None
    assert VocabContextItem.select().count() == 0


# ── 6. _cached probe ──────────────────────────────────────────────────


def test_cached_probe_returns_none_when_absent(temp_db):
    from services import vocab_context_gen

    assert vocab_context_gen._cached("missing_word", "mid") is None


def test_cached_probe_returns_row_when_present(temp_db):
    """The UI uses _cached to decide whether to spawn an LLM worker; make
    sure a prior generation is visible via that probe."""
    from services import vocab_context_gen

    mock = _mock_llm_service()
    with patch("services.llm_service.llm_service", mock):
        row = vocab_context_gen.generate_context_item("perfunctory", "mid")

    cached = vocab_context_gen._cached(
        "perfunctory", "mid", vocab_context_gen.PROMPT_VERSION
    )
    assert cached is not None
    assert cached.id == row.id
