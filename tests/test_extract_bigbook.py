"""Tests for scripts/extract_ets_bigbook.py — Phase 4 · D2.

These tests exercise the parser against both a hand-crafted markdown
fixture (fast, deterministic) and a synthetic multi-page PDF rendered
with pymupdf (covers the marker_pipeline → parser handoff end-to-end).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import extract_ets_bigbook as bb  # noqa: E402

FIXTURE_PDF = ROOT / "tests" / "fixtures" / "fake_bigbook.pdf"


# ── Hand-crafted markdown fixtures ────────────────────────────────────
#
# The Big Book's post-extraction markdown looks roughly like:
#
#   Test 1
#
#   Section 1  Verbal Reasoning
#
#   1. The scientist's claim was met with _____ skepticism by her peers,
#      whose own findings suggested the opposite conclusion.
#      (A) measured
#      (B) reflexive
#      (C) muted
#      (D) genuine
#      (E) tepid
#
#   2. PLACID:
#      (A) turbulent    (B) serene    (C) distant
#      (D) frozen       (E) deep
#
#   ... (more questions) ...
#
#   Answer Key for Test 1
#   Section 1
#   1. B   2. A   3. C   4. D   5. B
#
# For tests we use simplified markdown that the parser handles the
# same way as real extracted content.

MINI_BOOK_MARKDOWN = """\
Preface and front matter here — ignored.

Test 1

Section 1  Verbal Reasoning

1. The senator's argument was _____ by the opposition, whose own
    evidence contradicted nearly every premise she had advanced.
    (A) challenged
    (B) supported
    (C) ignored
    (D) endorsed
    (E) misread

2. The passage most strongly suggests that the author views the
    reforms with cautious optimism. According to the passage, the
    author considers line 12 to be
    (A) ironic
    (B) literal
    (C) hyperbolic
    (D) metaphorical
    (E) neutral

3. PLACID:
    (A) turbulent
    (B) serene
    (C) distant
    (D) frozen
    (E) deep

4. TRAIN : LOCOMOTIVE ::
    (A) car : engine
    (B) plane : wing
    (C) ship : rudder
    (D) bus : wheel
    (E) bike : pedal

5. Quantitative Comparison — Column A : Column B.
    Column A: 3 + 4
    Column B: 2 + 5
    (A) Column A is greater
    (B) Column B is greater
    (C) The two are equal
    (D) Cannot be determined

Section 2  Analytical Ability

1. A library has five shelves labeled P, Q, R, S, T. If P must be
    to the left of Q and Q to the left of R ...
    (A) P Q R S T
    (B) P Q S R T
    (C) S P Q R T
    (D) T S P Q R
    (E) R Q P S T

Answer Key for Test 1

Section 1
1. A   2. A   3. A   4. A   5. C

Section 2
1. C
"""


# ── Unit tests: markdown → records ────────────────────────────────────

def test_split_tests_picks_numbered_tests_only():
    md = """
Test 1

Section 1
content
Test 99 is not a real test

Test 2

Section 1
more content
"""
    tests = bb._split_tests(md)
    assert set(tests.keys()) == {1, 2}


def test_split_sections_yields_section_number_and_body():
    body = "Section 1  Verbal\npreamble\n\nSection 2  Quant\nother"
    sections = bb._split_sections(body)
    assert len(sections) == 2
    nums = [s[0] for s in sections]
    assert nums == [1, 2]


def test_parse_answer_key_numeric_rows():
    key = bb.parse_answer_key_text("1. A\n2. B\n3. C\n4. D\n5. E\n")
    assert key == {1: "A", 2: "B", 3: "C", 4: "D", 5: "E"}


def test_extract_test_level_keys_bucketed_by_section():
    test_body = """\
Section 1 Verbal
1. A
    (A) x  (B) y  (C) z  (D) w  (E) v

Answer Key for Test 1

Section 1
1. A   2. B

Section 2
1. C   2. D
"""
    keys = bb._extract_test_level_keys(test_body)
    assert keys == {1: {1: "A", 2: "B"}, 2: {1: "C", 2: "D"}}


def test_classify_section_analytical_marked_obsolete():
    assert bb._classify_section("Analytical Ability", "") == bb.SUBTYPE_ANALYTICAL


def test_classify_section_verbal_is_mixed_pending_per_item_refinement():
    # Verbal sections in the Big Book mix TC / RC / antonym / analogy,
    # so the section classifier emits a sentinel and each item is
    # classified individually by _classify_question.
    body = "1. The _____ was clearly..."
    assert bb._classify_section("Verbal Ability", body) == "verbal_mixed"


def test_classify_question_refines_verbal_by_stem_shape():
    tc = "The claim was met with _____ skepticism."
    assert bb._classify_question("verbal_mixed", tc) == bb.SUBTYPE_TC
    ant = "PLACID:\n(A) turbulent\n(B) serene"
    assert bb._classify_question("verbal_mixed", ant) == bb.SUBTYPE_ANTONYM
    ana = "TRAIN :: LOCOMOTIVE :\n(A) car : engine"
    assert bb._classify_question("verbal_mixed", ana) == bb.SUBTYPE_ANALOGY
    rc = "According to the passage in line 12, the author..."
    assert bb._classify_question("verbal_mixed", rc) == bb.SUBTYPE_RC


def test_extract_test_returns_five_items_from_fixture_markdown():
    # The markdown fixture in this file has 5 items in section 1 + 1
    # in section 2 (analytical).
    items = bb.extract_test(1, MINI_BOOK_MARKDOWN.split("Test 1", 1)[1])
    # Section 1 has 5 items; section 2 has 1. Parser should find all 6,
    # classifying section 2's item as analytical.
    assert len(items) == 6
    by_section = {}
    for it in items:
        by_section.setdefault(it.section_num, []).append(it)
    assert len(by_section[1]) == 5
    assert len(by_section[2]) == 1
    assert by_section[2][0].subtype == bb.SUBTYPE_ANALYTICAL


def test_difficulty_prior_quartile_mapping():
    # 8-item section: positions 1,2 → Q1 (diff=2), 3..6 → Q2/Q3 (diff=3),
    # 7,8 → Q4 (diff=4).
    def mk(pos):
        return bb.BigBookQuestion(
            test_num=1, section_num=1, number=pos, section_size=8,
            subtype=bb.SUBTYPE_PS, prompt="x",
        )
    assert mk(1).difficulty_target == 2
    assert mk(2).difficulty_target == 2
    assert mk(3).difficulty_target == 3
    assert mk(5).difficulty_target == 3
    assert mk(6).difficulty_target == 3
    assert mk(7).difficulty_target == 4
    assert mk(8).difficulty_target == 4


def test_subtype_measure_mapping():
    assert bb.SUBTYPE_MEASURE[bb.SUBTYPE_RC] == "verbal"
    assert bb.SUBTYPE_MEASURE[bb.SUBTYPE_TC] == "verbal"
    assert bb.SUBTYPE_MEASURE[bb.SUBTYPE_QC] == "quant"
    assert bb.SUBTYPE_MEASURE[bb.SUBTYPE_DI] == "quant"
    assert bb.SUBTYPE_MEASURE[bb.SUBTYPE_PS] == "quant"


def test_obsolete_set_contents():
    assert bb.SUBTYPE_ANTONYM in bb.OBSOLETE_SUBTYPES
    assert bb.SUBTYPE_ANALOGY in bb.OBSOLETE_SUBTYPES
    assert bb.SUBTYPE_ANALYTICAL in bb.OBSOLETE_SUBTYPES
    assert bb.SUBTYPE_RC not in bb.OBSOLETE_SUBTYPES
    assert bb.SUBTYPE_TC not in bb.OBSOLETE_SUBTYPES


def test_parse_range_forms():
    assert bb._parse_range("1-10") == (1, 10)
    assert bb._parse_range("7") == (7, 7)
    assert bb._parse_range("  3-3 ") == (3, 3)


# ── PDF-backed end-to-end (uses the synthetic fixture) ────────────────

def _ensure_fixture_built() -> None:
    """Regenerate tests/fixtures/fake_bigbook.pdf if missing or stale."""
    if FIXTURE_PDF.exists() and FIXTURE_PDF.stat().st_size > 0:
        return
    from tests.fixtures import build_fake_bigbook  # noqa: WPS433
    build_fake_bigbook.main()


def test_fixture_pdf_exists_or_is_buildable():
    _ensure_fixture_built()
    assert FIXTURE_PDF.exists()
    assert FIXTURE_PDF.stat().st_size > 0


def test_end_to_end_parse_keeps_three_drops_two(tmp_path):
    """Fixture has 5 items: 3 keepers (TC, RC, QC) + 2 obsolete
    (antonym, analogy). With --skip-obsolete we expect 3 to reach
    extract_book's result tagged non-obsolete.
    """
    _ensure_fixture_built()
    from scripts.lib.marker_pipeline import (
        extract_pdf_to_markdown, extractor_available,
    )
    if not extractor_available():
        pytest.skip("pymupdf4llm not installed")

    workdir = tmp_path / "md"
    md_files = extract_pdf_to_markdown(FIXTURE_PDF, workdir)
    items = bb.extract_book(md_files, (1, 1))
    assert len(items) >= 5, f"expected ≥5 parsed items, got {len(items)}"

    kept = [it for it in items if not it.is_obsolete]
    dropped = [it for it in items if it.is_obsolete]
    assert len(kept) == 3
    assert len(dropped) == 2
    kept_subtypes = {it.subtype for it in kept}
    assert kept_subtypes == {bb.SUBTYPE_TC, bb.SUBTYPE_RC, bb.SUBTYPE_QC}
    dropped_subtypes = {it.subtype for it in dropped}
    assert dropped_subtypes == {bb.SUBTYPE_ANTONYM, bb.SUBTYPE_ANALOGY}


def test_answer_key_matching_from_fixture(tmp_path):
    _ensure_fixture_built()
    from scripts.lib.marker_pipeline import (
        extract_pdf_to_markdown, extractor_available,
    )
    if not extractor_available():
        pytest.skip("pymupdf4llm not installed")

    workdir = tmp_path / "md"
    md_files = extract_pdf_to_markdown(FIXTURE_PDF, workdir)
    items = bb.extract_book(md_files, (1, 1))
    # Every kept item should have a correct_label from the fixture's
    # embedded answer key.
    for it in items:
        if it.is_obsolete:
            continue
        assert it.correct_label is not None, (
            f"{it.source_anchor}: no key match"
        )
        labels = {lbl for lbl, _ in it.options}
        assert it.correct_label in labels


def test_tests_range_filter(tmp_path):
    """A range that excludes the fixture's test should yield zero items."""
    _ensure_fixture_built()
    from scripts.lib.marker_pipeline import (
        extract_pdf_to_markdown, extractor_available,
    )
    if not extractor_available():
        pytest.skip("pymupdf4llm not installed")

    workdir = tmp_path / "md"
    md_files = extract_pdf_to_markdown(FIXTURE_PDF, workdir)
    # Fixture is labeled "Test 1". Asking for tests 2-3 should find nothing.
    items_out_of_range = bb.extract_book(md_files, (2, 3))
    assert items_out_of_range == []
    # Asking for 1-3 should include test 1.
    items_in_range = bb.extract_book(md_files, (1, 3))
    assert len(items_in_range) >= 5


# ── DB import / idempotency ───────────────────────────────────────────

def test_import_writes_only_kept_items_with_skip_obsolete(temp_db, tmp_path):
    _ensure_fixture_built()
    from scripts.lib.marker_pipeline import (
        extract_pdf_to_markdown, extractor_available,
    )
    if not extractor_available():
        pytest.skip("pymupdf4llm not installed")
    from models.database import Question

    workdir = tmp_path / "md"
    md_files = extract_pdf_to_markdown(FIXTURE_PDF, workdir)
    items = bb.extract_book(md_files, (1, 1))

    inserted, skipped, dropped = bb.import_to_db(items, skip_obsolete=True)
    assert inserted == 3
    assert dropped == 2
    assert skipped == 0

    rows = list(Question.select().where(
        Question.source == "ets_big_book_t01"
    ))
    assert len(rows) == 3
    # All imports land as candidates.
    assert all(r.status == "candidate" for r in rows)
    assert all(r.provenance == "imported" for r in rows)
    # Subtypes should not include obsolete.
    subtypes = {r.subtype for r in rows}
    assert subtypes.issubset({bb.SUBTYPE_TC, bb.SUBTYPE_RC, bb.SUBTYPE_QC,
                               bb.SUBTYPE_PS, bb.SUBTYPE_DI})
    # provenance_json carries pipeline metadata.
    first = rows[0]
    payload = json.loads(first.provenance_json)
    assert payload["pipeline"] == "ets_big_book"
    assert "test_num" in payload


def test_import_is_idempotent(temp_db, tmp_path):
    _ensure_fixture_built()
    from scripts.lib.marker_pipeline import (
        extract_pdf_to_markdown, extractor_available,
    )
    if not extractor_available():
        pytest.skip("pymupdf4llm not installed")

    workdir = tmp_path / "md"
    md_files = extract_pdf_to_markdown(FIXTURE_PDF, workdir)
    items = bb.extract_book(md_files, (1, 1))

    inserted1, _, _ = bb.import_to_db(items, skip_obsolete=True)
    inserted2, skipped2, _ = bb.import_to_db(items, skip_obsolete=True)
    assert inserted1 == 3
    assert inserted2 == 0
    assert skipped2 == 3


# ── CLI smoke ─────────────────────────────────────────────────────────

def test_cli_dry_run_against_fixture_exits_zero(capsys):
    _ensure_fixture_built()
    from scripts.lib.marker_pipeline import extractor_available
    if not extractor_available():
        pytest.skip("pymupdf4llm not installed")

    rc = bb.main(["--pdf", str(FIXTURE_PDF), "--dry-run", "--tests", "1-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "[dry-run-summary]" in out


def test_cli_errors_on_missing_pdf(capsys):
    rc = bb.main(["--pdf", "/nonexistent/path.pdf", "--dry-run"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err.lower()
