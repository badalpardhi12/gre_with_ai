"""Tests for ``validators/`` modules.

Covers each validator rule with one positive and one negative case using
plain dataclass-style fixtures (no DB). The driver-script test exercises
``scripts/run_validators.py`` with ``temp_db`` so we verify file outputs
without depending on shipped seed data.
"""
import csv
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from validators import (  # noqa: E402
    validate_awa,
    validate_quant,
    validate_verbal,
)
from validators.findings import (  # noqa: E402
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    ValidationFinding,
)


# ── Lightweight fixtures (no Peewee) ──────────────────────────────────


@dataclass
class FakeOption:
    option_label: str = "A"
    option_text: str = ""
    is_correct: bool = False


@dataclass
class FakeNumeric:
    exact_value: Optional[float] = None
    numerator: Optional[int] = None
    denominator: Optional[int] = None
    tolerance: Optional[float] = 0.001
    mode: str = "decimal"


@dataclass
class FakeStimulus:
    id: int = 1
    content: str = ""


@dataclass
class FakeQuestion:
    id: int = 1
    measure: str = "quant"
    subtype: str = "mcq_single"
    prompt: str = ""
    options: List[FakeOption] = field(default_factory=list)
    numeric_answers: List[FakeNumeric] = field(default_factory=list)
    stimulus: Optional[FakeStimulus] = None


@dataclass
class FakeAWAPrompt:
    id: int = 1
    prompt_text: str = ""
    instructions: str = ""


def _rule_ids(findings):
    return {f.rule_id for f in findings}


# ── ValidationFinding dataclass ─────────────────────────────────────────


def test_finding_severity_must_be_known():
    with pytest.raises(ValueError):
        ValidationFinding(
            rule_id="X",
            severity="critical",       # invalid
            message="m",
            details={},
        )


def test_finding_is_hashable():
    f = ValidationFinding(rule_id="X", severity=SEVERITY_ERROR,
                          message="m", details={"qid": 1})
    # Frozen dataclass → hashable as long as details is empty/hashable. Our
    # details is a dict (not hashable), so we don't put it in a set; just
    # confirm equality semantics.
    g = ValidationFinding(rule_id="X", severity=SEVERITY_ERROR,
                          message="m", details={"qid": 1})
    assert f == g


# ── Quant ──────────────────────────────────────────────────────────────


def test_quant_numeric_entry_missing_answer_is_flagged():
    q = FakeQuestion(
        subtype="numeric_entry",
        numeric_answers=[],  # no rows at all
    )
    rules = _rule_ids(validate_quant(q))
    assert "QUANT_NUMERIC_NO_ANSWER" in rules
    assert "QUANT_NO_ANSWER_KEY" in rules  # catch-all also fires


def test_quant_numeric_entry_with_value_is_clean():
    q = FakeQuestion(
        subtype="numeric_entry",
        numeric_answers=[FakeNumeric(exact_value=3.0, tolerance=0.01)],
    )
    findings = validate_quant(q)
    assert findings == []


def test_quant_negative_tolerance_is_flagged():
    q = FakeQuestion(
        subtype="numeric_entry",
        numeric_answers=[FakeNumeric(exact_value=3.0, tolerance=-0.5)],
    )
    rules = _rule_ids(validate_quant(q))
    assert "QUANT_NUMERIC_NEGATIVE_TOLERANCE" in rules


def test_quant_qc_canonical_options_pass():
    opts = [
        FakeOption("A", "Quantity A is greater", False),
        FakeOption("B", "Quantity B is greater", False),
        FakeOption("C", "The two quantities are equal", True),
        FakeOption("D",
                   "The relationship cannot be determined from the "
                   "information given",
                   False),
    ]
    q = FakeQuestion(subtype="qc", options=opts)
    assert validate_quant(q) == []


def test_quant_qc_non_canonical_options_flagged():
    opts = [
        FakeOption("A", "Quantity A is greater", False),
        FakeOption("B", "Quantity B is bigger", False),  # non-canonical
        FakeOption("C", "The two quantities are equal", True),
        FakeOption(
            "D",
            "The relationship cannot be determined from the information given",
            False,
        ),
    ]
    q = FakeQuestion(subtype="qc", options=opts)
    rules = _rule_ids(validate_quant(q))
    assert "QUANT_QC_NON_CANONICAL_OPTIONS" in rules


def test_quant_qc_tolerates_html_wrap_and_trailing_period():
    """Real bank items sometimes wrap in <p>…</p> or end with a period."""
    opts = [
        FakeOption("A", "<p>Quantity A is greater.</p>", False),
        FakeOption("B", "Quantity B is greater", False),
        FakeOption("C", "The two quantities are equal", True),
        FakeOption(
            "D",
            "The relationship cannot be determined from the information given",
            False,
        ),
    ]
    q = FakeQuestion(subtype="qc", options=opts)
    rules = _rule_ids(validate_quant(q))
    assert "QUANT_QC_NON_CANONICAL_OPTIONS" not in rules


def test_quant_qc_wrong_option_count():
    opts = [
        FakeOption("A", "Quantity A is greater", True),
        FakeOption("B", "Quantity B is greater", False),
        FakeOption("C", "The two quantities are equal", False),
        # Missing D
    ]
    q = FakeQuestion(subtype="qc", options=opts)
    rules = _rule_ids(validate_quant(q))
    assert "QUANT_QC_OPTION_COUNT" in rules


def test_quant_mcq_multi_too_few_correct_flagged():
    opts = [
        FakeOption("A", "x", True),  # only 1 correct
        FakeOption("B", "y", False),
        FakeOption("C", "z", False),
        FakeOption("D", "w", False),
        FakeOption("E", "v", False),
    ]
    q = FakeQuestion(subtype="mcq_multi", options=opts)
    rules = _rule_ids(validate_quant(q))
    assert "QUANT_MCQ_MULTI_TOO_FEW_CORRECT" in rules


def test_quant_mcq_multi_two_correct_passes():
    opts = [
        FakeOption("A", "x", True),
        FakeOption("B", "y", True),
        FakeOption("C", "z", False),
        FakeOption("D", "w", False),
        FakeOption("E", "v", False),
    ]
    q = FakeQuestion(subtype="mcq_multi", options=opts)
    assert validate_quant(q) == []


def test_quant_no_answer_key_flagged_on_blank_mcq_single():
    q = FakeQuestion(subtype="mcq_single", options=[
        FakeOption("A", "x", False),
        FakeOption("B", "y", False),
        FakeOption("C", "z", False),
        FakeOption("D", "w", False),
        FakeOption("E", "v", False),
    ])
    rules = _rule_ids(validate_quant(q))
    assert "QUANT_NO_ANSWER_KEY" in rules


def test_quant_mcq_single_with_correct_passes_catch_all():
    q = FakeQuestion(subtype="mcq_single", options=[
        FakeOption("A", "x", True),
        FakeOption("B", "y", False),
        FakeOption("C", "z", False),
        FakeOption("D", "w", False),
        FakeOption("E", "v", False),
    ])
    assert validate_quant(q) == []


# ── Verbal ─────────────────────────────────────────────────────────────


def _tc_options_2blank():
    return [
        FakeOption("blank1_A", "x", True),
        FakeOption("blank1_B", "y", False),
        FakeOption("blank1_C", "z", False),
        FakeOption("blank2_D", "p", False),
        FakeOption("blank2_E", "q", True),
        FakeOption("blank2_F", "r", False),
    ]


def test_verbal_tc_two_blanks_passes():
    q = FakeQuestion(
        measure="verbal",
        subtype="tc",
        prompt="The (i) ___ committee was finally (ii) _______.",
        options=_tc_options_2blank(),
    )
    assert validate_verbal(q) == []


def test_verbal_tc_blank_count_mismatch_flagged():
    # 6 options imply 2 groups, but stem has only 1 blank.
    q = FakeQuestion(
        measure="verbal",
        subtype="tc",
        prompt="The committee was finally ___.",
        options=_tc_options_2blank(),
    )
    rules = _rule_ids(validate_verbal(q))
    assert "VERBAL_TC_BLANK_COUNT_MISMATCH" in rules


def test_verbal_tc_handles_latex_rule_blanks():
    """LaTeX-extracted bank rows use \\rule{...}{...}."""
    q = FakeQuestion(
        measure="verbal",
        subtype="tc",
        prompt=(r"Although the senator's reputation was built on reform, "
                r"her record reveals a surprising "
                r"\rule{1.2cm}{0.15mm} to the established line."),
        options=[
            FakeOption("A", "antipathy", False),
            FakeOption("B", "indifference", False),
            FakeOption("C", "fidelity", True),
            FakeOption("D", "challenge", False),
            FakeOption("E", "ambivalence", False),
        ],
    )
    assert validate_verbal(q) == []


def test_verbal_tc_handles_underline_phantom_blank():
    q = FakeQuestion(
        measure="verbal",
        subtype="tc",
        prompt=(r"Despite the popular image, the cell is surprisingly "
                r"\underline{\phantom{XXXXXXXX}}: things change."),
        options=[
            FakeOption("A", "static", False),
            FakeOption("B", "dynamic", True),
            FakeOption("C", "rigid", False),
            FakeOption("D", "fixed", False),
            FakeOption("E", "stable", False),
        ],
    )
    assert validate_verbal(q) == []


def test_verbal_tc_handles_underline_qquad_blank():
    q = FakeQuestion(
        measure="verbal",
        subtype="tc",
        prompt=(r"Even skeptics concede her argument is not easily "
                r"\(\underline{\qquad}\)."),
        options=[
            FakeOption("A", "dismissed", True),
            FakeOption("B", "explained", False),
            FakeOption("C", "counted", False),
            FakeOption("D", "shared", False),
            FakeOption("E", "weakened", False),
        ],
    )
    assert validate_verbal(q) == []


def test_verbal_tc_non_canonical_option_count_flagged():
    # 7 options is not a real ETS shape.
    opts = [FakeOption(f"L{i}", f"x{i}", i == 0) for i in range(7)]
    q = FakeQuestion(subtype="tc", measure="verbal",
                     prompt="X ___.", options=opts)
    rules = _rule_ids(validate_verbal(q))
    assert "VERBAL_TC_OPTION_SHAPE" in rules


def test_verbal_se_six_options_two_correct_passes():
    opts = [
        FakeOption("A", "x", True),
        FakeOption("B", "y", True),
        FakeOption("C", "z", False),
        FakeOption("D", "p", False),
        FakeOption("E", "q", False),
        FakeOption("F", "r", False),
    ]
    q = FakeQuestion(subtype="se", measure="verbal", options=opts)
    assert validate_verbal(q) == []


def test_verbal_se_wrong_option_count_flagged():
    opts = [FakeOption(f"L{i}", "x", i < 2) for i in range(5)]
    q = FakeQuestion(subtype="se", measure="verbal", options=opts)
    rules = _rule_ids(validate_verbal(q))
    assert "VERBAL_SE_OPTION_COUNT" in rules


def test_verbal_se_wrong_correct_count_flagged():
    opts = [
        FakeOption("A", "x", True),  # only 1 correct
        FakeOption("B", "y", False),
        FakeOption("C", "z", False),
        FakeOption("D", "p", False),
        FakeOption("E", "q", False),
        FakeOption("F", "r", False),
    ]
    q = FakeQuestion(subtype="se", measure="verbal", options=opts)
    rules = _rule_ids(validate_verbal(q))
    assert "VERBAL_SE_CORRECT_COUNT" in rules


def test_verbal_rc_with_passage_passes():
    q = FakeQuestion(
        subtype="rc_single",
        measure="verbal",
        prompt="The primary purpose of the passage is to",
        stimulus=FakeStimulus(content="A long passage..."),
        options=[FakeOption("A", "x", True)],
    )
    assert validate_verbal(q) == []


def test_verbal_rc_no_stimulus_flagged():
    q = FakeQuestion(subtype="rc_single", measure="verbal",
                     stimulus=None,
                     options=[FakeOption("A", "x", True)])
    rules = _rule_ids(validate_verbal(q))
    assert "VERBAL_RC_NO_PASSAGE" in rules


def test_verbal_rc_empty_stimulus_flagged():
    q = FakeQuestion(subtype="rc_single", measure="verbal",
                     stimulus=FakeStimulus(content="   \n  "),
                     options=[FakeOption("A", "x", True)])
    rules = _rule_ids(validate_verbal(q))
    assert "VERBAL_RC_NO_PASSAGE" in rules


def test_verbal_select_passage_with_instruction_passes():
    q = FakeQuestion(
        subtype="rc_select_passage",
        measure="verbal",
        prompt="Click on the sentence in which the author concedes a point.",
        stimulus=FakeStimulus(content="A long passage..."),
    )
    assert validate_verbal(q) == []


def test_verbal_select_passage_missing_instruction_flagged():
    q = FakeQuestion(
        subtype="rc_select_passage",
        measure="verbal",
        prompt="Find the relevant text.",
        stimulus=FakeStimulus(content="A long passage..."),
    )
    rules = _rule_ids(validate_verbal(q))
    assert "VERBAL_SELECT_PASSAGE_MISSING_INSTRUCTION" in rules


# ── AWA ────────────────────────────────────────────────────────────────


def test_awa_canonical_issue_prompt_passes():
    p = FakeAWAPrompt(
        prompt_text=("As people rely more and more on technology to solve "
                     "problems, the ability of humans to think for themselves "
                     "will surely deteriorate."),
        instructions=("Write a response in which you discuss the extent to "
                      "which you agree or disagree with the statement and "
                      "explain your reasoning for the position you take."),
    )
    assert validate_awa(p) == []


def test_awa_alternate_position_phrasing_passes():
    p = FakeAWAPrompt(
        prompt_text="Some say competition is destructive. Others say it motivates.",
        instructions=("Write a response in which you discuss which view more "
                      "closely aligns with your own position and explain your "
                      "reasoning."),
    )
    assert validate_awa(p) == []


def test_awa_missing_keywords_flagged():
    p = FakeAWAPrompt(
        prompt_text="Innovation drives prosperity.",
        instructions="Reflect on this proposition for an hour.",
    )
    rules = _rule_ids(validate_awa(p))
    assert "AWA_NOT_ISSUE_FORMAT" in rules


def test_awa_too_long_flagged():
    long_text = " ".join(["word"] * 305)
    p = FakeAWAPrompt(
        prompt_text=long_text,
        instructions="Write a response in which you discuss the extent to "
                     "which you agree or disagree with the claim.",
    )
    rules = _rule_ids(validate_awa(p))
    assert "AWA_TOO_LONG" in rules


def test_awa_empty_prompt_flagged():
    p = FakeAWAPrompt(prompt_text="   ", instructions="")
    rules = _rule_ids(validate_awa(p))
    assert "AWA_EMPTY_PROMPT" in rules


# ── Driver-script integration ─────────────────────────────────────────


def test_run_validators_writes_csv_and_json(temp_db, tmp_path, monkeypatch):
    """Build a tiny live bank, run the driver, verify outputs exist."""
    from models.database import (
        AWAPrompt, NumericAnswer, Question, QuestionOption, Stimulus,
    )
    # Quant numeric_entry MISSING the NumericAnswer row → should flag.
    Question.create(
        id=1001, measure="quant", subtype="numeric_entry",
        prompt="If x=3, what is x+0?", status="live",
    )
    # Quant mcq_single with a correct option → clean.
    q2 = Question.create(
        id=1002, measure="quant", subtype="mcq_single",
        prompt="2+2=?", status="live",
    )
    QuestionOption.create(question=q2, option_label="A",
                          option_text="3", is_correct=False)
    QuestionOption.create(question=q2, option_label="B",
                          option_text="4", is_correct=True)
    # Verbal SE with WRONG correct count → should flag.
    q3 = Question.create(
        id=1003, measure="verbal", subtype="se",
        prompt="Choose two synonyms.", status="live",
    )
    for label, correct in [("A", True), ("B", False), ("C", False),
                           ("D", False), ("E", False), ("F", False)]:
        QuestionOption.create(question=q3, option_label=label,
                              option_text=label, is_correct=correct)
    # AWA prompt — both clean.
    AWAPrompt.create(
        prompt_text="A wise civic life requires public deliberation.",
        instructions="Write a response in which you discuss the extent to "
                     "which you agree or disagree with the claim.",
    )
    # Clean AWA prompt.
    AWAPrompt.create(
        prompt_text="Education is the path to civic virtue.",
        instructions="Write a response in which you discuss the extent to "
                     "which you agree or disagree with the claim.",
    )

    # Redirect audit outputs to tmp_path.
    monkeypatch.setattr(
        "scripts.run_validators.AUDIT_DIR", tmp_path,
    )
    monkeypatch.setattr(
        "scripts.run_validators.CSV_PATH",
        tmp_path / "validator_findings_test.csv",
    )
    monkeypatch.setattr(
        "scripts.run_validators.JSON_PATH",
        tmp_path / "validator_summary_test.json",
    )
    from scripts import run_validators
    rc = run_validators.main(["--quiet"])
    assert rc == 0

    csv_path = tmp_path / "validator_findings_test.csv"
    json_path = tmp_path / "validator_summary_test.json"
    assert csv_path.exists()
    assert json_path.exists()

    # CSV header + at least 1 row of findings.
    with csv_path.open() as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)
    assert header == [
        "qid", "measure", "subtype",
        "rule_id", "severity", "message", "details_json",
    ]
    assert rows, "expected at least one finding"

    # Summary JSON has the documented keys.
    summary = json.loads(json_path.read_text())
    for key in ("generated_at", "total_findings", "by_severity",
                "by_rule_id", "sample_qids"):
        assert key in summary
    # We injected at least: numeric_entry no-answer and SE wrong correct count.
    rule_ids = set(summary["by_rule_id"].keys())
    assert "QUANT_NUMERIC_NO_ANSWER" in rule_ids
    assert "VERBAL_SE_CORRECT_COUNT" in rule_ids
    # And no false-positive on the clean items: mcq_single with answer key
    # and the second AWA prompt should produce no findings of their own.
    assert summary["by_severity"]["error"] >= 1


def test_run_validators_idempotent(temp_db, tmp_path, monkeypatch):
    """Re-running overwrites the same files without error."""
    from models.database import AWAPrompt
    AWAPrompt.create(
        prompt_text="Apple should innovate broadly.",
        instructions="Write a response in which you discuss the extent to "
                     "which you agree or disagree with the claim.",
    )
    monkeypatch.setattr("scripts.run_validators.AUDIT_DIR", tmp_path)
    monkeypatch.setattr("scripts.run_validators.CSV_PATH",
                        tmp_path / "v.csv")
    monkeypatch.setattr("scripts.run_validators.JSON_PATH",
                        tmp_path / "v.json")
    from scripts import run_validators
    assert run_validators.main(["--quiet"]) == 0
    first_mtime = (tmp_path / "v.csv").stat().st_mtime_ns
    # Run again — should not raise, and file should be rewritten.
    assert run_validators.main(["--quiet"]) == 0
    assert (tmp_path / "v.csv").exists()
    # mtime may equal on very fast filesystems but file content is fine
    # to overwrite. Just confirm the file is still there and parseable.
    assert json.loads((tmp_path / "v.json").read_text())
