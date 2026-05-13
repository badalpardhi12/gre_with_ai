"""Tests for scripts/extract_ets_og.py — Phase 4 · D1.

These tests exercise the entire pipeline (PDF -> markdown -> parser)
against a synthetic 2-page PDF fixture.  No ETS content is shipped;
the fixture is built at test-collection time with invented
GRE-style items that exercise three distinct subtype branches:

    Question 1  -> quantitative comparison (QC, 4 options, difficulty=Easy)
    Question 2  -> sentence equivalence (SE, 6 options, difficulty=Medium)
    Question 3  -> reading comprehension (RC, 5 options, difficulty=Hard)

Question 3 additionally references a figure / diagram in its stem so
we can verify the has_figure classification path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import extract_ets_og as eog  # noqa: E402


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "fake_ets_og.pdf"


# ── Fixture generation ────────────────────────────────────────────────
#
# We generate the fixture on demand rather than committing a binary
# PDF — deterministic across runs because the text content, font, and
# layout are fixed.  Skips gracefully if pymupdf isn't installed (the
# project's requirements.txt already pins it, so this is really only
# defence for a partial venv).

def _ensure_fixture() -> Path:
    """Create tests/fixtures/fake_ets_og.pdf if missing. Return its path."""
    if FIXTURE_PATH.exists() and FIXTURE_PATH.stat().st_size > 0:
        return FIXTURE_PATH
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        pytest.skip("pymupdf not installed")

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Page 1: QC + SE.  We keep the layout simple — one item per text
    # block, generous vertical spacing, explicit markers the parser
    # keys off of (``Question N``, ``[Difficulty]``, ``Answer:`` line).
    doc = pymupdf.open()
    try:
        p1 = doc.new_page(width=612, height=792)
        _insert_text(p1, 72, 72, [
            "Quantitative Reasoning",
            "",
            "Question 1 [Easy]",
            "Given x > 2.",
            "Quantity A: 3x + 2",
            "Quantity B: 2x + 4",
            "",
            "(A) Quantity A is greater.",
            "(B) Quantity B is greater.",
            "(C) The two quantities are equal.",
            "(D) The relationship cannot be determined.",
            "",
            "Answer: A",
        ])
        _insert_text(p1, 72, 380, [
            "Verbal Reasoning",
            "",
            "Question 2 [Medium]",
            "Although the painting was widely ______, critics still",
            "found it to be ______ by subsequent generations.",
            "Select the two answer choices that produce sentences",
            "most similar in meaning.",
            "",
            "(A) celebrated",
            "(B) acclaimed",
            "(C) reviled",
            "(D) lacking",
            "(E) polished",
            "(F) unpolished",
            "",
            "Answer: A, B",
        ])

        # Page 2: RC passage + one RC question.
        p2 = doc.new_page(width=612, height=792)
        _insert_text(p2, 72, 72, [
            "Reading Passage",
            "",
            "In the figure below, a linear model is shown relating two",
            "variables.  The author argues that the slope reflects the",
            "underlying structural relationship rather than a purely",
            "empirical correlation, and that critics who dismiss",
            "the linear assumption miss the point.",
            "",
            "Question 3 [Hard]",
            "The passage suggests that the author believes",
            "",
            "(A) the critics are correct about the correlation.",
            "(B) the slope is a statistical artifact only.",
            "(C) the relationship is structural, not coincidental.",
            "(D) the figure is misleading.",
            "(E) linear models should be avoided.",
            "",
            "Answer: C",
        ])

        doc.save(str(FIXTURE_PATH))
    finally:
        doc.close()
    return FIXTURE_PATH


def _insert_text(page, x: float, y: float, lines):
    """Insert each line at a fixed line-height below ``y``."""
    line_height = 14
    for i, line in enumerate(lines):
        page.insert_text((x, y + i * line_height), line, fontsize=11)


@pytest.fixture(scope="module")
def fixture_pdf() -> Path:
    return _ensure_fixture()


@pytest.fixture(scope="module")
def parsed_items(fixture_pdf):
    """Run the full pipeline once per test module."""
    return eog.extract_from_ebook(fixture_pdf)


# ── Parser tests ──────────────────────────────────────────────────────

def test_pipeline_extracts_three_items(parsed_items):
    assert len(parsed_items) == 3
    nums = sorted(q.number for q in parsed_items)
    assert nums == [1, 2, 3]


def test_question_one_classified_as_qc(parsed_items):
    q = next(q for q in parsed_items if q.number == 1)
    assert q.subtype == "qc"
    assert q.measure == "quant"
    assert q.difficulty_target == 2  # Easy
    # QC items have exactly 4 options (A-D).
    assert [lbl for lbl, _ in q.options] == ["A", "B", "C", "D"]
    assert q.correct_labels == ["A"]


def test_question_two_classified_as_sentence_equiv(parsed_items):
    q = next(q for q in parsed_items if q.number == 2)
    assert q.subtype == "sentence_equiv"
    assert q.measure == "verbal"
    assert q.difficulty_target == 3  # Medium
    # SE items always have 6 options (A-F).
    assert [lbl for lbl, _ in q.options] == ["A", "B", "C", "D", "E", "F"]
    # Two correct answers.
    assert set(q.correct_labels) == {"A", "B"}


def test_question_three_classified_as_rc(parsed_items):
    q = next(q for q in parsed_items if q.number == 3)
    assert q.subtype in ("rc_single", "mcq_single")
    # The passage linker should have attached the preceding Reading
    # Passage and promoted to rc_single.
    assert q.subtype == "rc_single"
    assert q.measure == "verbal"
    assert q.difficulty_target == 4  # Hard
    assert q.correct_labels == ["C"]
    # Stimulus text should be present and mention "figure".
    assert "figure" in q.stimulus_text.lower()


def test_figure_bearing_item_flagged(parsed_items):
    """Question 3's stimulus mentions a figure, so has_figure must be True."""
    q = next(q for q in parsed_items if q.number == 3)
    assert q.has_figure is True


def test_source_anchor_shape(parsed_items):
    anchors = sorted(q.source_anchor for q in parsed_items)
    assert anchors == ["q001", "q002", "q003"]


# ── CLI tests ─────────────────────────────────────────────────────────

def test_dry_run_exits_zero_and_writes_nothing(capsys, fixture_pdf, temp_db):
    """--dry-run should not touch the DB even when it's available."""
    from models.database import Question

    before = Question.select().count()
    rc = eog.main(["--dry-run", "--ebook", str(fixture_pdf)])
    after = Question.select().count()

    assert rc == 0
    assert before == after  # no DB writes
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "[dry-run-summary]" in out
    # The summary line should be valid JSON that parses cleanly.
    summary_line = [l for l in out.splitlines() if "[dry-run-summary]" in l][0]
    _, payload = summary_line.split(" ", 1)
    data = json.loads(payload.replace("[dry-run-summary] ", ""))
    assert data["total"] == 3


def test_cli_missing_ebook_exits_nonzero(tmp_path):
    missing = tmp_path / "nope.pdf"
    rc = eog.main(["--dry-run", "--ebook", str(missing)])
    assert rc == 2


# ── DB import tests ──────────────────────────────────────────────────

def test_import_inserts_three_candidates(temp_db, parsed_items):
    from models.database import Question, QuestionOption, Stimulus

    inserted, skipped = eog.import_to_db(parsed_items)
    assert inserted == 3
    assert skipped == 0

    rows = list(Question.select().where(Question.source == "ets_og_3rd"))
    assert len(rows) == 3
    for r in rows:
        assert r.status == "candidate"
        assert r.provenance == "imported"

    # RC item should have a linked Stimulus.
    rc = Question.get(Question.source_anchor == "q003")
    assert rc.stimulus is not None
    assert rc.stimulus.stimulus_type == "passage"

    # QC item: option A marked correct.
    qc = Question.get(Question.source_anchor == "q001")
    correct = [o for o in qc.options if o.is_correct]
    assert len(correct) == 1
    assert correct[0].option_label == "A"

    # SE item: options A and B marked correct.
    se = Question.get(Question.source_anchor == "q002")
    correct_labels = sorted(o.option_label for o in se.options if o.is_correct)
    assert correct_labels == ["A", "B"]


def test_import_is_idempotent(temp_db, parsed_items):
    from models.database import Question

    inserted1, skipped1 = eog.import_to_db(parsed_items)
    assert inserted1 == 3
    assert skipped1 == 0

    inserted2, skipped2 = eog.import_to_db(parsed_items)
    assert inserted2 == 0
    assert skipped2 == 3

    # No duplicates.
    assert Question.select().where(Question.source == "ets_og_3rd").count() == 3


def test_figure_bearing_item_marked_for_audit(temp_db, parsed_items):
    """Figure-bearing items should carry the audit-pending signal."""
    from models.database import Question

    eog.import_to_db(parsed_items)
    rc = Question.get(Question.source_anchor == "q003")
    refs = rc.get_figure_refs()
    assert refs == ["pending-audit"]
    prov = rc.get_provenance()
    assert prov.get("figure_audit_pending") is True
    assert prov.get("has_figure") is True


# ── Difficulty mapping ───────────────────────────────────────────────

def test_difficulty_mapping_labels_to_targets():
    assert eog.DIFFICULTY_MAP["easy"] == 2
    assert eog.DIFFICULTY_MAP["medium"] == 3
    assert eog.DIFFICULTY_MAP["hard"] == 4


def test_difficulty_target_defaults_to_medium_when_unlabeled():
    q = eog.ETSQuestion(
        number=99, measure="quant", subtype="mcq_single",
        prompt="dummy", difficulty=None,
    )
    assert q.difficulty_target == 3
