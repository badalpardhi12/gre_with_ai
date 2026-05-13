"""Tests for scripts/extract_manhattan_di_clusters.py — Phase 4 · Task #19.

Exercises the parser's set-boundary detection, cluster integrity
(one stimulus, many questions sharing its id), DB idempotency, and
--dry-run's no-write contract.

The fixture is a small synthetic JSON blob that mirrors the shape of
``data/extracted/manhattan/ch24_raw.json`` (the upstream extraction
dump) — no real Manhattan content is shipped in the test suite.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import extract_manhattan_di_clusters as emdc  # noqa: E402


# ── Synthetic fixture ─────────────────────────────────────────────────

def _fake_ch24_payload():
    """Build a fixture resembling the upstream JSON: ``stimulus_text`` is
    set ONLY on the first question of each set; subsequent siblings
    leave it null so the parser uses the prior stimulus.

    Ships 3 sets (pie chart 3Q, markdown table 3Q, line graph 4Q) —
    enough to exercise >=2 set boundaries and all three stimulus-type
    branches.
    """
    return {
        "questions": [
            # ── Set 1: pie chart, 3 Q ────────────────────────────────
            {
                "q_number": 1, "page": 10, "subtype": "mcq_single",
                "stimulus_text": "Pie chart titled 'Fruit Sales' showing "
                                 "Apples 40%, Oranges 35%, Pears 25%.",
                "prompt": "What fraction of sales were apples?",
                "options": [
                    {"label": "A", "text": "1/4"},
                    {"label": "B", "text": "2/5"},
                    {"label": "C", "text": "1/2"},
                ],
                "correct_label": "B",
                "explanation": "40% = 2/5.",
            },
            {
                "q_number": 2, "page": 10, "subtype": "mcq_single",
                "stimulus_text": None,
                "prompt": "Which fruit had lowest sales?",
                "options": [
                    {"label": "A", "text": "Apples"},
                    {"label": "B", "text": "Oranges"},
                    {"label": "C", "text": "Pears"},
                ],
                "correct_label": "C",
                "explanation": "Pears 25% is lowest.",
            },
            {
                "q_number": 3, "page": 11, "subtype": "mcq_single",
                "stimulus_text": None,
                "prompt": "Sum of apples and pears as percent?",
                "options": [
                    {"label": "A", "text": "60%"},
                    {"label": "B", "text": "65%"},
                    {"label": "C", "text": "70%"},
                ],
                "correct_label": "B",
                "explanation": "40 + 25 = 65.",
            },
            # ── Set 2: markdown table, 3 Q ───────────────────────────
            {
                "q_number": 4, "page": 12, "subtype": "mcq_single",
                "stimulus_text": "Enrollment by Grade\n\n"
                                 "| | Boys | Girls |\n"
                                 "|---|---|---|\n"
                                 "| Grade 9 | 10 | 12 |\n"
                                 "| Grade 10 | 14 | 9 |",
                "prompt": "How many boys are in grade 9?",
                "options": [
                    {"label": "A", "text": "10"},
                    {"label": "B", "text": "12"},
                    {"label": "C", "text": "14"},
                ],
                "correct_label": "A",
                "explanation": "Cell [Grade 9][Boys] = 10.",
            },
            {
                "q_number": 5, "page": 12, "subtype": "mcq_single",
                "stimulus_text": None,
                "prompt": "Total grade 10 students?",
                "options": [
                    {"label": "A", "text": "21"},
                    {"label": "B", "text": "22"},
                    {"label": "C", "text": "23"},
                ],
                "correct_label": "C",
                "explanation": "14 + 9 = 23.",
            },
            {
                "q_number": 6, "page": 12, "subtype": "mcq_single",
                "stimulus_text": None,
                "prompt": "More girls or boys overall?",
                "options": [
                    {"label": "A", "text": "Boys"},
                    {"label": "B", "text": "Girls"},
                    {"label": "C", "text": "Equal"},
                ],
                "correct_label": "A",
                "explanation": "24 boys vs 21 girls.",
            },
            # ── Set 3: line graph, 4 Q ───────────────────────────────
            {
                "q_number": 7, "page": 14, "subtype": "mcq_single",
                "stimulus_text": "Line graph of monthly revenue "
                                 "($000s): Jan 10, Feb 15, Mar 20, "
                                 "Apr 25, May 22, Jun 28.",
                "prompt": "Peak month?",
                "options": [
                    {"label": "A", "text": "Apr"},
                    {"label": "B", "text": "May"},
                    {"label": "C", "text": "Jun"},
                ],
                "correct_label": "C",
                "explanation": "Jun = 28 is the peak.",
            },
            {
                "q_number": 8, "page": 14, "subtype": "mcq_single",
                "stimulus_text": None,
                "prompt": "Change from Apr to May?",
                "options": [
                    {"label": "A", "text": "-3"},
                    {"label": "B", "text": "+3"},
                    {"label": "C", "text": "0"},
                ],
                "correct_label": "A",
                "explanation": "25 -> 22 = -3.",
            },
            {
                "q_number": 9, "page": 15, "subtype": "mcq_single",
                "stimulus_text": None,
                "prompt": "Revenue in Feb?",
                "options": [
                    {"label": "A", "text": "10"},
                    {"label": "B", "text": "15"},
                    {"label": "C", "text": "20"},
                ],
                "correct_label": "B",
                "explanation": "Feb = 15.",
            },
            {
                "q_number": 10, "page": 15, "subtype": "mcq_single",
                "stimulus_text": None,
                "prompt": "H2 average?",
                "options": [
                    {"label": "A", "text": "23"},
                    {"label": "B", "text": "25"},
                    {"label": "C", "text": "27"},
                ],
                "correct_label": "B",
                "explanation": "(25+22+28)/3 = 25.",
            },
        ]
    }


@pytest.fixture
def fake_ch24_path(tmp_path):
    path = tmp_path / "fake_ch24.json"
    path.write_text(json.dumps(_fake_ch24_payload()))
    return path


# ── Pure-parser tests (no DB) ─────────────────────────────────────────

def test_parser_identifies_three_sets(fake_ch24_path):
    raw = emdc.load_ch24_questions(fake_ch24_path)
    clusters = emdc.parse_sets(raw)
    assert len(clusters) == 3
    assert [len(c.questions) for c in clusters] == [3, 3, 4]


def test_parser_identifies_at_least_two_sets(fake_ch24_path):
    """Task #19 acceptance: parser identifies >=2 sets from synthetic input."""
    raw = emdc.load_ch24_questions(fake_ch24_path)
    clusters = emdc.parse_sets(raw)
    assert len(clusters) >= 2


def test_parser_preserves_q_numbers_within_cluster(fake_ch24_path):
    raw = emdc.load_ch24_questions(fake_ch24_path)
    clusters = emdc.parse_sets(raw)
    assert [q.q_number for q in clusters[0].questions] == [1, 2, 3]
    assert [q.q_number for q in clusters[1].questions] == [4, 5, 6]
    assert [q.q_number for q in clusters[2].questions] == [7, 8, 9, 10]


def test_parser_classifies_stimulus_types(fake_ch24_path):
    raw = emdc.load_ch24_questions(fake_ch24_path)
    clusters = emdc.parse_sets(raw)
    # pie chart -> graph, markdown table -> table, line graph -> graph
    assert clusters[0].stimulus_type == "graph"
    assert clusters[1].stimulus_type == "table"
    assert clusters[2].stimulus_type == "graph"


def test_parser_skips_leading_questions_without_stimulus():
    """Questions that appear before any stimulus_text don't invent a set."""
    raw = [
        {"q_number": 99, "stimulus_text": None, "prompt": "orphan",
         "options": [], "correct_label": None, "explanation": ""},
        {"q_number": 100, "stimulus_text": "A bar graph.",
         "prompt": "Real Q", "options": [], "correct_label": None,
         "explanation": ""},
    ]
    clusters = emdc.parse_sets(raw)
    assert len(clusters) == 1
    assert clusters[0].questions[0].q_number == 100


def test_stimulus_type_inference_edge_cases():
    # Markdown table row -> table
    assert emdc._infer_stimulus_type("Enrollment\n| A | B |\n") == "table"
    # "following table" -> table
    assert emdc._infer_stimulus_type(
        "Questions are based on the following table.\n\nPopulation data"
    ) == "table"
    # "pie chart" -> graph
    assert emdc._infer_stimulus_type("Pie chart of sales") == "graph"
    # "scatter plot" -> graph
    assert emdc._infer_stimulus_type(
        "A scatter plot of X vs Y"
    ) == "graph"
    # Pure prose -> passage (nothing table/graph-ish).  Careful: the
    # substring "graph" would also match inside words like "paragraph",
    # so the assertion below uses prose that's clean of those collisions.
    assert emdc._infer_stimulus_type(
        "Some descriptive prose about qualitative trends only."
    ) == "passage"


# ── DB integration tests ──────────────────────────────────────────────

def test_import_creates_one_stimulus_per_set_with_shared_stimulus_id(
        temp_db, fake_ch24_path):
    """Every question in a cluster must share its cluster's stimulus_id."""
    raw = emdc.load_ch24_questions(fake_ch24_path)
    clusters = emdc.parse_sets(raw)
    stim_n, q_n, q_skip = emdc.import_to_db(clusters)
    assert stim_n == 3
    assert q_n == 10
    assert q_skip == 0

    from models.database import Question, Stimulus
    # 3 new stimuli (titled "Manhattan 5lb Ch24 Set N") — assert each has
    # all-its-questions pointing at it.
    for i in (1, 2, 3):
        stim = Stimulus.get(Stimulus.title == f"Manhattan 5lb Ch24 Set {i}")
        qs = list(Question.select().where(Question.stimulus == stim))
        assert len(qs) in (3, 4), f"Set {i} should have 3-4 Qs, got {len(qs)}"
        # All qs share the same stimulus_id.
        stim_ids = {q.stimulus_id for q in qs}
        assert stim_ids == {stim.id}


def test_import_every_question_has_data_interp_subtype(
        temp_db, fake_ch24_path):
    raw = emdc.load_ch24_questions(fake_ch24_path)
    clusters = emdc.parse_sets(raw)
    emdc.import_to_db(clusters)
    from models.database import Question
    rows = list(Question.select().where(Question.source == emdc.SOURCE_TAG))
    assert len(rows) == 10
    assert all(r.subtype == "data_interp" for r in rows)
    assert all(r.status == "candidate" for r in rows)
    assert all(r.measure == "quant" for r in rows)


def test_import_idempotent_second_run_inserts_nothing(
        temp_db, fake_ch24_path):
    raw = emdc.load_ch24_questions(fake_ch24_path)
    clusters = emdc.parse_sets(raw)
    emdc.import_to_db(clusters)

    # Second run: same clusters, no new rows.
    stim_n2, q_n2, q_skip2 = emdc.import_to_db(clusters)
    assert stim_n2 == 0
    assert q_n2 == 0
    assert q_skip2 == 10

    from models.database import Question, Stimulus
    # Confirm absolute counts didn't drift.
    assert Question.select().where(Question.source == emdc.SOURCE_TAG).count() == 10
    assert Stimulus.select().where(Stimulus.title.startswith(
        "Manhattan 5lb Ch24 Set")).count() == 3


def test_import_drops_singleton_clusters(temp_db, tmp_path):
    """A cluster with only 1 question (below min_cluster_size) is skipped —
    the whole point of this re-extraction is to avoid DI singletons."""
    payload = {
        "questions": [
            {"q_number": 1, "stimulus_text": "Pie chart of X",
             "subtype": "mcq_single", "prompt": "Q1", "options": [],
             "correct_label": None, "explanation": "", "page": 1},
            {"q_number": 2, "stimulus_text": "Different bar graph of Y",
             "subtype": "mcq_single", "prompt": "Q2", "options": [],
             "correct_label": None, "explanation": "", "page": 2},
            {"q_number": 3, "stimulus_text": None,
             "subtype": "mcq_single", "prompt": "Q3", "options": [],
             "correct_label": None, "explanation": "", "page": 2},
        ]
    }
    path = tmp_path / "mini.json"
    path.write_text(json.dumps(payload))

    raw = emdc.load_ch24_questions(path)
    clusters = emdc.parse_sets(raw)
    assert len(clusters) == 2
    assert len(clusters[0].questions) == 1  # singleton -> dropped
    assert len(clusters[1].questions) == 2

    stim_n, q_n, q_skip = emdc.import_to_db(clusters)
    # Only cluster 2 (>=2 Q) imports; cluster 1's 1 question counts as skipped.
    assert stim_n == 1
    assert q_n == 2
    assert q_skip == 1


def test_dry_run_does_not_touch_db(temp_db, fake_ch24_path, monkeypatch,
                                   capsys):
    """--dry-run returns 0 and leaves the DB empty."""
    monkeypatch.setattr(sys, "argv", [
        "extract_manhattan_di_clusters.py",
        "--ch24-json", str(fake_ch24_path),
        "--dry-run",
    ])
    rc = emdc.main([
        "--ch24-json", str(fake_ch24_path),
        "--dry-run",
    ])
    assert rc == 0
    from models.database import Question
    assert Question.select().where(
        Question.source == emdc.SOURCE_TAG).count() == 0


def test_cli_main_real_run_writes_rows(temp_db, fake_ch24_path):
    rc = emdc.main(["--ch24-json", str(fake_ch24_path)])
    assert rc == 0
    from models.database import Question
    rows = list(Question.select().where(Question.source == emdc.SOURCE_TAG))
    assert len(rows) == 10


def test_options_and_correct_flag_round_trip(temp_db, fake_ch24_path):
    raw = emdc.load_ch24_questions(fake_ch24_path)
    clusters = emdc.parse_sets(raw)
    emdc.import_to_db(clusters)

    from models.database import Question, QuestionOption
    # Q1 from set 1 -> correct_label='B'
    q = Question.get(
        (Question.source == emdc.SOURCE_TAG) &
        (Question.source_anchor == "set1_q1"))
    opts = list(QuestionOption.select().where(QuestionOption.question == q))
    assert {o.option_label for o in opts} == {"A", "B", "C"}
    correct = [o for o in opts if o.is_correct]
    assert len(correct) == 1 and correct[0].option_label == "B"
