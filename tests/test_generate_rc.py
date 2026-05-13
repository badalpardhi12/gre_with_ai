"""Tests for scripts/generate_rc_passages.py — Phase 4 · D7.

All network calls (source fetchers) and all LLM calls are mocked via
``unittest.mock``. No test touches the network. Each test uses the
``temp_db`` fixture (tests/conftest.py) so DB mutations are isolated.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make project root importable. conftest does this, but scripts/ has
# no __init__.py — add the root explicitly so `from scripts import ...`
# resolves.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Fixtures ──────────────────────────────────────────────────────────

_FAKE_SOURCE_LABEL = "gutenberg:1232 The Prince (Machiavelli)"
_FAKE_SOURCE_TEXT = (
    "A prince who wishes to maintain power must understand that fortune "
    "governs half of our actions, but leaves the other half — or "
    "thereabouts — to be steered by us. It is necessary to know how to "
    "be both a lion and a fox, for the lion cannot defend itself from "
    "snares and the fox cannot defend itself from wolves. Those who rely "
    "solely on the lion's strength do not understand the craft of "
    "governance."
) * 4  # roughly 200 words — enough to condense from.


# A plausible condensed passage at ~400 words (actual length doesn't
# matter for tests; the condense-length guard only enforces ≥ length/2).
_FAKE_PASSAGE = (
    "The argument that a ruler must balance fortitude with guile finds "
    "its clearest articulation in classical political theory, which "
    "posits that statecraft is neither pure virtue nor pure calculation "
    "but a deliberate alternation between the two. A sovereign who "
    "relies solely on martial force risks ensnarement by the subtler "
    "stratagems of rivals; one who depends only on cunning forfeits the "
    "deterrent effect that open strength confers. "
) * 10  # well over 200 words.


_FAKE_QUESTIONS_PAYLOAD = {
    "questions": [
        {
            "question_type": "main_idea",
            "stem": "The passage is primarily concerned with",
            "options": [
                "defending the use of deception in all political contexts",
                "arguing that effective rule requires combining force and cunning",
                "criticizing rulers who rely on force alone",
                "advocating for the primacy of moral virtue in governance",
                "tracing the historical origins of political realism",
            ],
            "correct_label": "B",
            "explanation": (
                "The passage frames statecraft as a deliberate alternation "
                "between force and calculation — neither on its own suffices."
            ),
        },
        {
            "question_type": "inference",
            "stem": "The passage most strongly suggests that a ruler relying solely on cunning would",
            "options": [
                "be better positioned to detect subtle stratagems",
                "inevitably succeed in a political environment free of rivals",
                "lack the deterrent effect that visible strength provides",
                "be unable to recognize threats from more martial powers",
                "maintain moral legitimacy more effectively than a martial ruler",
            ],
            "correct_label": "C",
            "explanation": (
                "The passage directly notes that guile alone forfeits the "
                "deterrent effect of open strength."
            ),
        },
        {
            "question_type": "detail",
            "stem": "According to the passage, classical political theory posits that statecraft is",
            "options": [
                "purely a matter of moral virtue",
                "best left to hereditary rulers",
                "primarily a product of favorable fortune",
                "a deliberate alternation between virtue and calculation",
                "an art that cannot be taught",
            ],
            "correct_label": "D",
            "explanation": (
                "The opening sentence explicitly names 'a deliberate "
                "alternation' between virtue and calculation."
            ),
        },
        {
            "question_type": "function",
            "stem": "The reference to 'martial force' in the passage primarily serves to",
            "options": [
                "establish the author's preference for military solutions",
                "illustrate one half of the balance the passage recommends",
                "contrast ancient and modern approaches to governance",
                "introduce a historical example that the passage will later refute",
                "anticipate an objection from pacifist critics",
            ],
            "correct_label": "B",
            "explanation": (
                "Martial force exemplifies the 'fortitude' half of the "
                "fortitude/guile balance the passage recommends."
            ),
        },
    ]
}


def _mock_llm(passage=_FAKE_PASSAGE,
              question_payload=None,
              judge_score=5,
              judge_reason="solid item"):
    """Build a MagicMock standing in for services.llm_service.llm_service.

    ``generate`` (used by the condense stage) returns ``passage``.
    ``generate_json`` is routed by which system prompt it sees so the
    same mock serves both the generate and judge stages.
    """
    if question_payload is None:
        question_payload = _FAKE_QUESTIONS_PAYLOAD

    mock = MagicMock()
    mock.generate.return_value = passage

    def _generate_json(system, user, *a, **kw):
        # Route on a unique phrase from each system prompt.
        if "item-quality judge" in system:
            score = judge_score(user) if callable(judge_score) else judge_score
            return {"score": score, "reason": judge_reason}
        if "GRE item writer" in system:
            return question_payload
        raise AssertionError(f"unexpected system prompt: {system[:80]}")

    mock.generate_json.side_effect = _generate_json
    mock.get_current_config.return_value = {"model": "anthropic/claude-opus-4"}
    return mock


def _fake_fetcher(label=_FAKE_SOURCE_LABEL, text=_FAKE_SOURCE_TEXT):
    """Return a zero-arg callable that yields a fixed (label, text)."""
    def _fetch():
        return label, text
    return _fetch


# ── 1. Happy path — one passage → 1 Stimulus + 4 Questions ────────────


def test_pipeline_builds_stimulus_and_questions(temp_db):
    from scripts import generate_rc_passages as gen
    from models.database import Stimulus, Question, QuestionOption

    llm = _mock_llm()
    summary = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(),
    )

    assert summary["attempted"] == 1
    assert summary["built"] == 1
    assert summary["inserted"] == 4
    assert summary["reused"] == 0
    assert summary["skipped"] == 0

    # Exactly one passage-typed Stimulus was created.
    stims = list(Stimulus.select().where(Stimulus.stimulus_type == "passage"))
    assert len(stims) == 1
    stim = stims[0]
    spec = json.loads(stim.render_spec)
    assert spec["pipeline"] == "ai_generated_rc_v2"
    assert spec["source_label"] == _FAKE_SOURCE_LABEL
    assert spec["source_hash"]

    # Exactly 4 questions, all linked and correctly shaped.
    qs = list(Question.select().where(Question.stimulus == stim))
    assert len(qs) == 4
    for q in qs:
        assert q.measure == "verbal"
        assert q.subtype == "rc_multi"
        assert q.source == "ai_generated_rc_v2"
        assert q.status == "candidate"
        assert q.provenance == "llm_generated"
        assert q.topic == "reading_comprehension"
        # 5 options, exactly 1 correct.
        opts = list(q.options)
        assert len(opts) == 5
        assert sum(1 for o in opts if o.is_correct) == 1
        # source_anchor is deterministic & unique per question.
        assert q.source_anchor.endswith(f"_q{qs.index(q)+1:02d}") or \
               q.source_anchor.split("_q")[-1].isdigit()
        # provenance_json captures judge score.
        prov = json.loads(q.provenance_json)
        assert prov["pipeline"] == "ai_generated_rc_v2"
        assert prov["source_hash"] == spec["source_hash"]
        assert prov["judge_score"] == 5


def test_pipeline_accepts_three_questions(temp_db):
    """The generate stage may return 3 or 4 questions — both are valid."""
    from scripts import generate_rc_passages as gen
    from models.database import Question

    three_q = {"questions": _FAKE_QUESTIONS_PAYLOAD["questions"][:3]}
    llm = _mock_llm(question_payload=three_q)
    summary = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(),
    )
    assert summary["built"] == 1
    assert summary["inserted"] == 3
    assert Question.select().count() == 3


# ── 2. Judge-reject path ─────────────────────────────────────────────


def test_judge_rejects_low_scoring_passage(temp_db):
    """All questions score 2 → candidate rejected, nothing persisted."""
    from scripts import generate_rc_passages as gen
    from models.database import Stimulus, Question

    llm = _mock_llm(judge_score=2)
    summary = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(),
    )

    assert summary["attempted"] == 1
    assert summary["built"] == 0
    assert summary["skipped"] == 1
    assert summary["inserted"] == 0
    assert Stimulus.select().count() == 0
    assert Question.select().count() == 0


def test_judge_mixed_rejects_when_under_three_survive(temp_db):
    """If fewer than 3 questions survive the judge, drop the whole passage.

    The rc_multi subtype presumes a multi-question cluster; a single
    surviving item wouldn't honour the contract.
    """
    from scripts import generate_rc_passages as gen
    from models.database import Stimulus

    # Score list: only the first item passes; others rejected.
    def _scorer(user_prompt):
        # Item stems are embedded in the judge user prompt — route on them.
        if "The passage is primarily concerned with" in user_prompt:
            return 5
        return 1

    llm = _mock_llm(judge_score=_scorer)
    summary = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(),
    )

    assert summary["built"] == 0
    assert summary["skipped"] == 1
    assert Stimulus.select().count() == 0


# ── 3. Idempotency ────────────────────────────────────────────────────


def test_pipeline_is_idempotent_on_same_source(temp_db):
    """Re-running against the same source text reuses the Stimulus
    and does NOT duplicate Questions."""
    from scripts import generate_rc_passages as gen
    from models.database import Stimulus, Question

    # First run — fresh insert.
    llm1 = _mock_llm()
    s1 = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm1, fetcher=_fake_fetcher(),
    )
    assert s1["inserted"] == 4
    assert s1["reused"] == 0
    assert Stimulus.select().count() == 1
    assert Question.select().count() == 4

    # Second run — same source text, same hash → reuse short-circuit.
    llm2 = _mock_llm()
    s2 = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm2, fetcher=_fake_fetcher(),
    )
    assert s2["attempted"] == 1
    assert s2["built"] == 1
    assert s2["inserted"] == 0
    assert s2["reused"] == 1

    # DB counts unchanged.
    assert Stimulus.select().count() == 1
    assert Question.select().count() == 4


def test_different_source_hashes_produce_distinct_stimuli(temp_db):
    """Different source text → different source_hash → new Stimulus."""
    from scripts import generate_rc_passages as gen
    from models.database import Stimulus

    llm = _mock_llm()
    gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(label="gutenberg:1 A", text="alpha " * 400),
    )
    gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(label="gutenberg:2 B", text="beta " * 400),
    )
    assert Stimulus.select().count() == 2


# ── 4. Validation & error paths ──────────────────────────────────────


def test_pipeline_skips_on_llm_exception(temp_db):
    """An LLM exception during condense → skip, no DB write."""
    from scripts import generate_rc_passages as gen
    from models.database import Stimulus

    llm = MagicMock()
    llm.generate.side_effect = RuntimeError("simulated failure")
    llm.get_current_config.return_value = {"model": "mock"}

    summary = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(),
    )
    assert summary["skipped"] == 1
    assert summary["inserted"] == 0
    assert Stimulus.select().count() == 0


def test_pipeline_skips_on_short_condense_output(temp_db):
    """If the LLM returns a condensed passage < passage_length/2 words,
    the candidate is dropped (under-compression / LLM truncation)."""
    from scripts import generate_rc_passages as gen
    from models.database import Stimulus

    llm = _mock_llm(passage="only a few words total")
    summary = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(),
    )
    assert summary["skipped"] == 1
    assert Stimulus.select().count() == 0


def test_pipeline_rejects_malformed_question_payload(temp_db):
    """A generate payload with only 2 questions (not 3-4) is rejected."""
    from scripts import generate_rc_passages as gen
    from models.database import Stimulus

    bad = {"questions": _FAKE_QUESTIONS_PAYLOAD["questions"][:2]}
    llm = _mock_llm(question_payload=bad)
    summary = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(),
    )
    assert summary["skipped"] == 1
    assert Stimulus.select().count() == 0


def test_pipeline_rejects_option_list_of_wrong_length(temp_db):
    """A question with 4 options (not 5) fails validation."""
    from scripts import generate_rc_passages as gen
    from models.database import Question

    bad_payload = {
        "questions": [
            {**q, "options": q["options"][:4]}
            for q in _FAKE_QUESTIONS_PAYLOAD["questions"][:3]
        ]
    }
    llm = _mock_llm(question_payload=bad_payload)
    summary = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(),
    )
    assert summary["skipped"] == 1
    assert Question.select().count() == 0


def test_pipeline_rejects_invalid_correct_label(temp_db):
    """A correct_label outside A–E is rejected."""
    from scripts import generate_rc_passages as gen
    from models.database import Question

    bad_payload = {
        "questions": [
            {**q, "correct_label": "Z"}
            for q in _FAKE_QUESTIONS_PAYLOAD["questions"][:3]
        ]
    }
    llm = _mock_llm(question_payload=bad_payload)
    summary = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(),
    )
    assert summary["skipped"] == 1
    assert Question.select().count() == 0


# ── 5. CLI & argument surface ────────────────────────────────────────


def test_cli_count_zero_dry_run_exits_cleanly(capsys):
    """``--count 0 --dry-run`` must never touch the DB or network."""
    from scripts import generate_rc_passages as gen

    rc = gen.main(["--count", "0", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no-op" in out or "count=0" in out


def test_cli_rejects_unknown_source():
    from scripts import generate_rc_passages as gen

    with pytest.raises(SystemExit):
        gen.main(["--count", "1", "--source", "reddit"])


def test_run_pipeline_rejects_unknown_source():
    """Defensive check: the programmatic entry point validates source."""
    from scripts import generate_rc_passages as gen

    with pytest.raises(ValueError):
        gen.run_pipeline(count=1, source="reddit")


def test_run_pipeline_count_zero_is_noop(temp_db):
    from scripts import generate_rc_passages as gen
    from models.database import Stimulus

    summary = gen.run_pipeline(count=0, source="gutenberg", llm=_mock_llm())
    assert summary == {"attempted": 0, "built": 0,
                       "inserted": 0, "reused": 0, "skipped": 0}
    assert Stimulus.select().count() == 0


# ── 6. Source fetcher registry ───────────────────────────────────────


def test_source_fetcher_registry_lists_expected_sources():
    """The three source buckets the CLI advertises must all be wired."""
    from scripts import generate_rc_passages as gen

    assert set(gen.SOURCE_FETCHERS) == {"gutenberg", "plos", "wikipedia"}


def test_gutenberg_catalog_has_fifty_entries():
    """Spec: a curated list of 50 public-domain humanities texts."""
    from scripts import generate_rc_passages as gen

    assert len(gen.GUTENBERG_CATALOG) == 50
    # Every entry has an id and a title.
    for entry in gen.GUTENBERG_CATALOG:
        assert entry["id"]
        assert entry["title"]


def test_fetcher_failure_is_survived(temp_db):
    """A fetcher that raises → pipeline logs and skips, doesn't abort."""
    from scripts import generate_rc_passages as gen
    from models.database import Stimulus

    def _boom():
        raise RuntimeError("simulated network failure")

    llm = _mock_llm()
    summary = gen.run_pipeline(
        count=2, source="gutenberg",
        llm=llm, fetcher=_boom,
    )
    assert summary["attempted"] == 2
    assert summary["skipped"] == 2
    assert summary["built"] == 0
    assert Stimulus.select().count() == 0


# ── 7. Dry-run skips DB writes ───────────────────────────────────────


def test_dry_run_does_not_write_to_db(temp_db):
    """Even when the pipeline builds a valid candidate, dry-run skips
    the upsert entirely."""
    from scripts import generate_rc_passages as gen
    from models.database import Stimulus, Question

    llm = _mock_llm()
    summary = gen.run_pipeline(
        count=1, source="gutenberg",
        llm=llm, fetcher=_fake_fetcher(), dry_run=True,
    )
    assert summary["built"] == 1
    assert summary["inserted"] == 0
    assert summary["reused"] == 0
    assert Stimulus.select().count() == 0
    assert Question.select().count() == 0


# ── 8. Source-hash determinism ───────────────────────────────────────


def test_source_hash_is_stable(temp_db):
    from scripts import generate_rc_passages as gen

    h1 = gen._source_hash("L", "body text")
    h2 = gen._source_hash("L", "body text")
    h3 = gen._source_hash("L", "body text.")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 40  # SHA-1 hex digest
