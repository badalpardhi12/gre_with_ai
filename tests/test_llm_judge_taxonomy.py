"""Tests for ``scripts.llm_judge_taxonomy``.

Mocks the LLM client end-to-end so the suite runs offline. Covers
parse/validation logic against the canonical taxonomy, rejection of
out-of-taxonomy responses, and the dual-DB write path.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Project root on sys.path so ``scripts`` imports cleanly under pytest.
_HERE = Path(__file__).resolve()
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.llm_judge_taxonomy import (  # noqa: E402
    SUBTYPE_TO_QUESTION_TYPE,
    allowed_topic_subtopic_pairs,
    build_user_prompt,
    derive_question_type,
    fetch_options,
    fetch_pending_questions,
    judge_one,
    parse_judge_response,
    taxonomy_summary_for_prompt,
    write_taxonomy_both_dbs,
)


# ── Validation / parsing ────────────────────────────────────────────


def test_parse_judge_response_accepts_valid_quant_pair():
    raw = {"topic": "algebra", "subtopic": "linear_equations_systems"}
    out = parse_judge_response(raw, "quant")
    assert out == {"topic": "algebra", "subtopic": "linear_equations_systems"}


def test_parse_judge_response_accepts_valid_verbal_pair():
    raw = {"topic": "reading_comprehension", "subtopic": "rc_inference"}
    out = parse_judge_response(raw, "verbal")
    assert out == {"topic": "reading_comprehension", "subtopic": "rc_inference"}


def test_parse_judge_response_rejects_out_of_taxonomy_topic():
    """A topic id that doesn't exist anywhere is rejected."""
    raw = {"topic": "made_up_topic", "subtopic": "rc_inference"}
    assert parse_judge_response(raw, "verbal") is None


def test_parse_judge_response_rejects_out_of_taxonomy_subtopic():
    raw = {"topic": "algebra", "subtopic": "fancy_algebra"}
    assert parse_judge_response(raw, "quant") is None


def test_parse_judge_response_rejects_topic_subtopic_mismatch():
    """A valid topic and a valid subtopic that aren't paired together
    in the taxonomy must be rejected."""
    # ``rc_inference`` is a real subtopic but lives under
    # ``reading_comprehension``, NOT ``critical_reasoning``. Pairing it
    # with ``critical_reasoning`` should fail.
    raw = {"topic": "critical_reasoning", "subtopic": "rc_inference"}
    assert parse_judge_response(raw, "verbal") is None


def test_parse_judge_response_rejects_cross_measure_pair():
    """A quant subtopic must not validate under verbal."""
    raw = {"topic": "algebra", "subtopic": "linear_equations_systems"}
    assert parse_judge_response(raw, "verbal") is None


def test_parse_judge_response_rejects_missing_keys():
    assert parse_judge_response({"topic": "algebra"}, "quant") is None
    assert parse_judge_response({}, "quant") is None
    assert parse_judge_response({"topic": "", "subtopic": ""}, "quant") is None


def test_parse_judge_response_handles_string_input_with_fences():
    """JSON wrapped in markdown fences should still parse."""
    raw = (
        "```json\n"
        '{"topic": "geometry", "subtopic": "triangles"}\n'
        "```"
    )
    out = parse_judge_response(raw, "quant")
    assert out == {"topic": "geometry", "subtopic": "triangles"}


def test_parse_judge_response_returns_none_on_garbage_string():
    assert parse_judge_response("not even json", "quant") is None


def test_parse_judge_response_returns_none_on_non_dict():
    assert parse_judge_response(["topic", "algebra"], "quant") is None
    assert parse_judge_response(None, "quant") is None
    assert parse_judge_response(42, "quant") is None


# ── Helpers ─────────────────────────────────────────────────────────


def test_allowed_topic_subtopic_pairs_includes_quant_canonical():
    pairs = allowed_topic_subtopic_pairs("quant")
    assert ("algebra", "linear_equations_systems") in pairs
    assert ("geometry", "triangles") in pairs
    assert ("data_analysis", "probability") in pairs


def test_allowed_topic_subtopic_pairs_includes_verbal_canonical():
    pairs = allowed_topic_subtopic_pairs("verbal")
    assert ("reading_comprehension", "rc_inference") in pairs
    assert ("text_completion", "tc_2_blank") in pairs


def test_taxonomy_summary_includes_topic_and_subtopic_ids():
    summary = taxonomy_summary_for_prompt("quant")
    assert "algebra" in summary
    assert "linear_equations_systems" in summary
    assert "Linear Equations & Systems" in summary  # display name


def test_derive_question_type_uses_canonical_for_known_subtypes():
    assert derive_question_type("qc", None) == "quantitative_comparison"
    assert derive_question_type("rc_single", None) == "reading_comprehension"
    assert derive_question_type("numeric_entry", "") == "numeric_entry"


def test_derive_question_type_overrides_existing_short_form():
    """If the row already has a non-canonical short value (e.g. 'qc'),
    we replace it with the canonical long form."""
    assert derive_question_type("qc", "qc") == "quantitative_comparison"


def test_derive_question_type_falls_through_for_unknown_subtype():
    # No canonical mapping; preserve whatever was there.
    assert derive_question_type("weirdtype", "preserve_me") == "preserve_me"
    assert derive_question_type("weirdtype", None) == ""


def test_subtype_mapping_covers_all_known_subtypes():
    """Sanity: every subtype the schema enumerates has a mapping."""
    expected = {
        "qc", "mcq_single", "mcq_multi", "numeric_entry", "data_interp",
        "tc", "se", "rc_single", "rc_multi", "rc_select_passage",
        "awa_issue",
    }
    assert expected.issubset(set(SUBTYPE_TO_QUESTION_TYPE))


def test_build_user_prompt_includes_options_and_explanation():
    text = build_user_prompt(
        qid=42,
        prompt="What is 2 + 2?",
        options=[("A", "3", False), ("B", "4", True), ("C", "5", False)],
        explanation="Simple arithmetic.",
        measure="quant",
        subtype="mcq_single",
    )
    assert "Question id: 42" in text
    assert "What is 2 + 2?" in text
    assert "B: 4" in text
    assert "Simple arithmetic." in text


# ── Mock-LLM judge round-trip ───────────────────────────────────────


class _MockLLM:
    """Minimal stand-in for ``services.llm_service.llm_service``.

    ``responses`` is a list of payloads to return one-by-one from
    ``generate_json``. Each entry can be a dict (returned as-is) or
    an Exception subclass (raised). The mock raises when responses
    run out, which is what we want — any extra LLM call is a bug.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate_json(self, system_prompt, user_prompt, model=None):
        self.calls.append(
            {"system": system_prompt, "user": user_prompt, "model": model}
        )
        if not self._responses:
            raise AssertionError(
                "MockLLM ran out of responses (call #{})".format(
                    len(self.calls)
                )
            )
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_judge_one_returns_decision_on_first_call():
    mock = _MockLLM([{"topic": "algebra", "subtopic": "quadratics"}])
    decision, err = judge_one(
        mock,
        measure="quant",
        subtype="mcq_single",
        qid=1,
        prompt_text="Solve x^2 - 5x + 6 = 0",
        options=[("A", "x=2,3", True), ("B", "x=1,6", False)],
        explanation="Factor the quadratic.",
    )
    assert err is None
    assert decision == {"topic": "algebra", "subtopic": "quadratics"}
    assert len(mock.calls) == 1


def test_judge_one_retries_once_on_invalid_response():
    """First reply is out-of-taxonomy; retry yields a valid one."""
    mock = _MockLLM(
        [
            {"topic": "made_up", "subtopic": "nope"},
            {"topic": "geometry", "subtopic": "triangles"},
        ]
    )
    decision, err = judge_one(
        mock,
        measure="quant",
        subtype="mcq_single",
        qid=2,
        prompt_text="A right triangle has legs 3 and 4. Hypotenuse?",
        options=[("A", "5", True)],
        explanation="3-4-5.",
    )
    assert err is None
    assert decision == {"topic": "geometry", "subtopic": "triangles"}
    assert len(mock.calls) == 2
    # Retry uses the stricter system prompt.
    assert "rejected" in mock.calls[1]["system"]


def test_judge_one_rejects_after_two_invalid_replies():
    mock = _MockLLM(
        [
            {"topic": "junk", "subtopic": "junk2"},
            {"topic": "still_bad", "subtopic": "bad"},
        ]
    )
    decision, err = judge_one(
        mock,
        measure="quant",
        subtype="qc",
        qid=3,
        prompt_text="?",
        options=[],
        explanation="",
    )
    assert decision is None
    assert err == "invalid_after_retry"
    assert len(mock.calls) == 2


def test_judge_one_handles_first_call_exception():
    mock = _MockLLM(
        [
            RuntimeError("transient network blip"),
            {"topic": "arithmetic", "subtopic": "percents"},
        ]
    )
    decision, err = judge_one(
        mock,
        measure="quant",
        subtype="mcq_single",
        qid=4,
        prompt_text="What is 25% of 80?",
        options=[("A", "20", True)],
        explanation="0.25 * 80 = 20.",
    )
    assert err is None
    assert decision == {"topic": "arithmetic", "subtopic": "percents"}


# ── Dual-DB write smoke test ────────────────────────────────────────


def _bootstrap_question_table(conn: sqlite3.Connection, rows):
    """Create a minimal ``question`` table with the columns we touch."""
    conn.execute(
        "CREATE TABLE question ("
        "  id INTEGER PRIMARY KEY,"
        "  measure TEXT NOT NULL,"
        "  subtype TEXT NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'live',"
        "  prompt TEXT NOT NULL DEFAULT '',"
        "  explanation TEXT NOT NULL DEFAULT '',"
        "  topic TEXT NOT NULL DEFAULT '',"
        "  subtopic TEXT NOT NULL DEFAULT '',"
        "  question_type TEXT NOT NULL DEFAULT '',"
        "  updated_at TEXT NOT NULL DEFAULT ''"
        ")"
    )
    conn.execute(
        "CREATE TABLE questionoption ("
        "  id INTEGER PRIMARY KEY,"
        "  question_id INTEGER NOT NULL,"
        "  option_label TEXT NOT NULL,"
        "  option_text TEXT NOT NULL,"
        "  is_correct INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO question (id, measure, subtype, status, prompt, "
            "                      explanation, topic, subtopic, "
            "                      question_type, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            r,
        )
    conn.commit()


def test_write_taxonomy_updates_both_dbs(tmp_path):
    user_path = tmp_path / "user.db"
    seed_path = tmp_path / "seed.db"

    rows = [
        # (id, measure, subtype, status, prompt, explanation,
        #  topic, subtopic, question_type, updated_at)
        (1, "quant", "qc", "live", "p1", "e1", "", "", "", ""),
        (2, "verbal", "rc_single", "live", "p2", "e2", "", "", "", ""),
    ]

    user_conn = sqlite3.connect(str(user_path))
    seed_conn = sqlite3.connect(str(seed_path))
    _bootstrap_question_table(user_conn, rows)
    _bootstrap_question_table(seed_conn, rows)
    user_conn.row_factory = sqlite3.Row

    write_taxonomy_both_dbs(
        user_conn, seed_conn, qid=1,
        topic="arithmetic", subtopic="percents",
        question_type="quantitative_comparison",
    )

    for label, path in [("user", user_path), ("seed", seed_path)]:
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM question WHERE id=1").fetchone()
        assert row["topic"] == "arithmetic", label
        assert row["subtopic"] == "percents", label
        assert row["question_type"] == "quantitative_comparison", label
        assert row["updated_at"]  # not empty
        c.close()

    user_conn.close()
    seed_conn.close()


def test_write_taxonomy_rolls_back_user_when_seed_fails(tmp_path):
    """If the seed write fails the user-DB write must be rolled back so
    the two DBs don't diverge."""
    user_path = tmp_path / "user.db"
    seed_path = tmp_path / "seed.db"

    rows = [
        (1, "quant", "qc", "live", "p1", "e1", "", "", "", ""),
    ]
    user_conn = sqlite3.connect(str(user_path))
    seed_conn = sqlite3.connect(str(seed_path))
    _bootstrap_question_table(user_conn, rows)
    _bootstrap_question_table(seed_conn, rows)

    # Drop the seed's question table to force the seed write to fail
    # without breaking the user write.
    seed_conn.execute("DROP TABLE question")
    seed_conn.commit()

    with pytest.raises(sqlite3.OperationalError):
        write_taxonomy_both_dbs(
            user_conn, seed_conn, qid=1,
            topic="arithmetic", subtopic="percents",
            question_type="quantitative_comparison",
        )

    # User row should NOT have been committed.
    user_conn.row_factory = sqlite3.Row
    row = user_conn.execute("SELECT * FROM question WHERE id=1").fetchone()
    assert row["topic"] == ""
    assert row["subtopic"] == ""
    assert row["question_type"] == ""

    user_conn.close()
    seed_conn.close()


# ── fetch_pending + fetch_options happy path ────────────────────────


def test_fetch_pending_questions_filters_by_status_and_emptiness(tmp_path):
    user_path = tmp_path / "user.db"
    rows = [
        # all three populated → not pending
        (1, "quant", "qc", "live", "p1", "e1",
         "algebra", "linear_equations_systems", "quantitative_comparison", ""),
        # missing topic → pending
        (2, "quant", "mcq_single", "live", "p2", "e2",
         "", "fractions_decimals", "multiple_choice", ""),
        # status=retired → not pending even if empty
        (3, "verbal", "tc", "retired", "p3", "e3", "", "", "", ""),
        # missing question_type only → pending
        (4, "verbal", "rc_single", "live", "p4", "e4",
         "reading_comprehension", "rc_inference", "", ""),
    ]
    conn = sqlite3.connect(str(user_path))
    _bootstrap_question_table(conn, rows)
    conn.row_factory = sqlite3.Row

    pending = fetch_pending_questions(conn, limit=None)
    pending_ids = sorted(r["id"] for r in pending)
    assert pending_ids == [2, 4]

    pending_limited = fetch_pending_questions(conn, limit=1)
    assert len(pending_limited) == 1
    conn.close()


def test_fetch_options_returns_correct_marker(tmp_path):
    user_path = tmp_path / "user.db"
    conn = sqlite3.connect(str(user_path))
    _bootstrap_question_table(conn, [
        (1, "quant", "mcq_single", "live", "p", "e", "", "", "", ""),
    ])
    conn.execute(
        "INSERT INTO questionoption (id, question_id, option_label, "
        "                            option_text, is_correct) "
        "VALUES (1,1,'A','wrong',0), (2,1,'B','right',1)"
    )
    conn.commit()
    conn.row_factory = sqlite3.Row

    opts = fetch_options(conn, 1)
    assert opts == [("A", "wrong", False), ("B", "right", True)]
    conn.close()


def test_qtype_only_fastpath_is_a_canonical_pair_check():
    """The fast-path lookup uses ``allowed_topic_subtopic_pairs`` — this
    test pins the lookup table behavior so the main loop's branch keeps
    working even if the taxonomy gains new entries later."""
    pairs = allowed_topic_subtopic_pairs("quant")
    # Canonical pair → fast-path eligible.
    assert ("arithmetic", "exponents_roots") in pairs
    # Non-canonical pair (real subtopic, wrong topic) → fast-path skipped.
    assert ("algebra", "exponents_roots") not in pairs
    # Out-of-taxonomy values → fast-path skipped.
    assert ("probability", "Basic Probability") not in pairs
