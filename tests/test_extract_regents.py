"""Tests for scripts/extract_regents.py — Phase 4 · D5.

All fixtures are hand-crafted plain text in the shape that
``pdftotext -layout`` produces on a real Regents PDF. No network is
touched; no PDFs are read.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable. conftest already does this, but
# we also need `scripts.` to be a package root — add it explicitly
# since scripts/ has no __init__.py.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import extract_regents as er  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────

# Minimal 4-item Algebra II style booklet. pdftotext flattens the
# two-column option layout, so options run (1)(2)(3)(4) in order.
ALG2_EXAM_TEXT = """\
                                 The University of the State of New York
                              REGENTS HIGH SCHOOL EXAMINATION
                                        ALGEBRA II
                                    Wednesday, June 19, 2024

Part I
Answer all 24 questions in this part. For each question, write on the
separate answer sheet the numeral preceding the word or expression
that best completes the statement or answers the question.

  1  If f(x) = 3x - 2, then f(4) equals

        (1) 10           (2) 14           (3) 5            (4) 2

  2  The expression (x + 3)(x - 3) is equivalent to

        (1) x^2 - 9      (2) x^2 + 9      (3) x^2 - 6x + 9  (4) x^2 + 6x - 9

  3  Which diagram below best represents the graph of y = x^2 ?

        (1) parabola up  (2) parabola dn  (3) line          (4) circle

  4  The solution of the equation 2x + 5 = 17 is

        (1) 6            (2) 11           (3) 22           (4) 12
"""


ALG2_KEY_TEXT = """\
                    Regents Examination in Algebra II - June 2024

                    Scoring Key and Rating Guide

                            Part I
            Question   Correct   Credit
               1          2        1
               2          1        1
               3          1        1
               4          1        1

                            Part II
            Constructed-response scoring begins below.
            Question 25 ...
"""


# ELA uses letter answers (A/B/C/D) in some years. Cover that path.
ELA_EXAM_TEXT = """\
  1  Based on the passage, the narrator's primary concern is

        (A) economic stability
        (B) social acceptance
        (C) artistic expression
        (D) family loyalty

  2  The figure of speech used in line 12 is best described as

        (A) metaphor
        (B) simile
        (C) alliteration
        (D) hyperbole
"""


ELA_KEY_TEXT = """\
Question  Correct  Credit
   1         C       1
   2         B       1
"""


# ── Answer-key parser tests ───────────────────────────────────────────

def test_parse_answer_key_numeric_form():
    key = er.parse_answer_key_text(ALG2_KEY_TEXT)
    assert key == {1: "B", 2: "A", 3: "A", 4: "A"}


def test_parse_answer_key_letter_form():
    key = er.parse_answer_key_text(ELA_KEY_TEXT)
    assert key == {1: "C", 2: "B"}


def test_parse_answer_key_stops_at_part_ii():
    # A Part II / constructed-response header must halt parsing so we
    # don't pick up essay rubric row numbers.
    text = """\
Question  Correct  Credit
   1         2       1
   2         3       1
Part II Constructed-response
   1         4       2
"""
    key = er.parse_answer_key_text(text)
    assert key == {1: "B", 2: "C"}


# ── Booklet parser tests ──────────────────────────────────────────────

def test_parse_exam_text_extracts_all_items():
    qs = er.parse_exam_text(ALG2_EXAM_TEXT, "nyc_regents_algebra2_2024_06")
    assert len(qs) == 4
    nums = [q.number for q in qs]
    assert nums == [1, 2, 3, 4]


def test_parse_exam_text_captures_stem_and_options():
    qs = er.parse_exam_text(ALG2_EXAM_TEXT, "nyc_regents_algebra2_2024_06")
    q1 = qs[0]
    assert "f(x) = 3x - 2" in q1.prompt
    assert len(q1.options) == 4
    labels = [lbl for lbl, _ in q1.options]
    assert labels == ["A", "B", "C", "D"]
    # (1) maps to "A" with text "10"
    assert q1.options[0] == ("A", "10")
    assert q1.options[1] == ("B", "14")
    assert q1.options[2] == ("C", "5")
    assert q1.options[3] == ("D", "2")


def test_parse_exam_text_flags_figure_bearing_stems():
    qs = er.parse_exam_text(ALG2_EXAM_TEXT, "nyc_regents_algebra2_2024_06")
    by_num = {q.number: q for q in qs}
    # Q3 mentions "diagram" — should be flagged.
    assert by_num[3].has_figure is True
    # Q1/Q2/Q4 have no figure language.
    assert by_num[1].has_figure is False
    assert by_num[2].has_figure is False
    assert by_num[4].has_figure is False


def test_parse_exam_text_letter_options_path():
    qs = er.parse_exam_text(ELA_EXAM_TEXT, "nyc_regents_english_2024_06")
    assert len(qs) == 2
    assert qs[0].options[0] == ("A", "economic stability")
    assert qs[1].options[2] == ("C", "alliteration")


def test_parse_exam_text_rejects_block_with_fewer_than_four_options():
    broken = """\
  1  What is 2 + 2 ?

        (1) 3           (2) 4
"""
    qs = er.parse_exam_text(broken, "test_exam")
    assert qs == []


# ── Join / full-pipeline tests ────────────────────────────────────────

def test_extract_one_joins_key_and_marks_correct_option():
    items = er.extract_one(
        "nyc_regents_algebra2_2024_06",
        ALG2_EXAM_TEXT,
        ALG2_KEY_TEXT,
    )
    assert len(items) == 4
    # Q1: key says "2" → label B → option text "14"
    q1 = items[0]
    assert q1.correct_label == "B"
    # sanity: stem + correct-option-text still match
    correct_text = dict(q1.options)[q1.correct_label]
    assert correct_text == "14"


def test_extract_one_drops_items_missing_from_key():
    # Strip Q3 and Q4 out of the key; parser should drop them.
    truncated_key = """\
Question  Correct  Credit
   1         2       1
   2         1       1
"""
    items = er.extract_one("slug", ALG2_EXAM_TEXT, truncated_key)
    assert [i.number for i in items] == [1, 2]


# ── Measure / subtype mapping ─────────────────────────────────────────

def test_measure_mapping_algebra_is_quant():
    items = er.extract_one(
        "nyc_regents_algebra2_2024_06",
        ALG2_EXAM_TEXT,
        ALG2_KEY_TEXT,
    )
    assert all(i.measure == "quant" for i in items)
    assert all(i.subtype == "mcq_single" for i in items)


def test_measure_mapping_english_is_verbal():
    items = er.extract_one(
        "nyc_regents_english_2024_06",
        ELA_EXAM_TEXT,
        ELA_KEY_TEXT,
    )
    assert all(i.measure == "verbal" for i in items)


def test_source_and_source_anchor_shape():
    items = er.extract_one(
        "nyc_regents_geometry_2024_06",
        ALG2_EXAM_TEXT.replace("ALGEBRA II", "GEOMETRY"),
        ALG2_KEY_TEXT,
    )
    assert items[0].source == "nyc_regents_geometry_2024_06"
    assert items[0].source_anchor == "q01"
    assert items[3].source_anchor == "q04"


# ── Target catalog sanity ─────────────────────────────────────────────

def test_target_catalog_has_nine_exams_across_three_subjects():
    subjects = {e["subject"] for e in er.TARGET_EXAMS}
    assert subjects == {"algebra2", "geometry", "english"}
    counts = {}
    for e in er.TARGET_EXAMS:
        counts[e["subject"]] = counts.get(e["subject"], 0) + 1
    assert counts == {"algebra2": 3, "geometry": 3, "english": 3}


def test_target_catalog_urls_look_like_nysed():
    for entry in er.TARGET_EXAMS:
        assert entry["exam_url"].startswith("https://www.nysedregents.org/")
        assert entry["key_url"].startswith("https://www.nysedregents.org/")
        assert entry["exam_url"].endswith(".pdf")
        assert entry["key_url"].endswith(".pdf")


# ── DB import (via temp_db fixture) ───────────────────────────────────

def test_import_to_db_inserts_as_candidate_with_correct_answer(temp_db):
    # temp_db fixture (conftest.py) gives us a clean sqlite with all tables.
    from models.database import Question, QuestionOption

    items = er.extract_one(
        "nyc_regents_algebra2_2024_06",
        ALG2_EXAM_TEXT,
        ALG2_KEY_TEXT,
    )
    inserted, skipped = er.import_to_db(items)
    assert inserted == 4
    assert skipped == 0

    rows = list(Question.select()
                .where(Question.source == "nyc_regents_algebra2_2024_06")
                .order_by(Question.source_anchor))
    assert len(rows) == 4
    first = rows[0]
    assert first.status == "candidate"
    assert first.difficulty_target == 2
    assert first.measure == "quant"
    assert first.subtype == "mcq_single"
    assert first.source_anchor == "q01"

    correct = [o for o in first.options if o.is_correct]
    assert len(correct) == 1
    assert correct[0].option_label == "B"  # numeric "2" → B


def test_import_to_db_is_idempotent(temp_db):
    items = er.extract_one(
        "nyc_regents_algebra2_2024_06",
        ALG2_EXAM_TEXT,
        ALG2_KEY_TEXT,
    )
    inserted1, _ = er.import_to_db(items)
    inserted2, skipped2 = er.import_to_db(items)
    assert inserted1 == 4
    assert inserted2 == 0
    assert skipped2 == 4


# ── CLI dry-run smoke ─────────────────────────────────────────────────

def test_dry_run_exits_zero_and_writes_nothing(capsys, monkeypatch, tmp_path):
    # Force cache dir to a temp location with no PDFs — extract_all
    # will report "skip" for every exam, and the summarizer will print
    # zero items. Crucially the command should not touch the DB.
    monkeypatch.setattr(er, "CACHE_DIR", tmp_path / "regents_cache")
    # Also redirect _cache_path so it reflects the patched CACHE_DIR.
    monkeypatch.setattr(er, "_cache_path",
                        lambda slug, kind:
                        (tmp_path / "regents_cache" / f"{slug}_{kind}.pdf"))

    rc = er.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "[dry-run-summary]" in out
