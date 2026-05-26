"""Regression tests for ``scripts/audit_answer_key_drift.py`` and
the ``_036_answer_key_drift_repair_2026_05_25`` migration.

The detector script is LLM-driven; we mock the LLM at the
``services.llm_service.llm_service`` import site so the triage logic
can be exercised offline. The migration is exercised against a
``temp_db`` (Peewee, no real seed) — the seed-mirror branch silently
short-circuits because the test fixture points ``SEED_DB_PATH`` at a
non-existent file.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

import pytest


@pytest.fixture(autouse=True)
def _evict_scripts_module():
    """``conftest.temp_db`` only evicts ``models`` and ``services``
    from ``sys.modules`` between tests. The audit script also captures
    a ``Question`` reference at module-import time (and so does the
    migrations module via Peewee). Evict both so each test re-binds
    to the current ``temp_db``."""
    for prefix in ("scripts", "models.migrations"):
        for mod in [m for m in list(sys.modules)
                    if m == prefix or m.startswith(prefix + ".")]:
            del sys.modules[mod]
    yield


# ── Triage logic (offline, no DB needed) ──────────────────────────


class FakeOption:
    def __init__(self, label, text="", is_correct=False):
        self.option_label = label
        self.option_text = text
        self.is_correct = is_correct


class FakeQuestion:
    def __init__(self, qid, subtype="mcq_single", explanation="", prompt=""):
        self.id = qid
        self.subtype = subtype
        self.explanation = explanation
        self.prompt = prompt


def test_triage_high_conf_single_answer_drift_flips():
    """Flip requires BOTH high LLM confidence AND independent
    heuristic agreement. The explanation here mentions C explicitly
    multiple times near positive-verdict words so the heuristic
    seconds the LLM."""
    from scripts.audit_answer_key_drift import triage
    q = FakeQuestion(
        1, subtype="mcq_single",
        explanation=(
            "C is the right choice. Option C is the best answer. "
            "We pick C as correct because of foo."
        ),
    )
    options = [
        FakeOption("A", "x", is_correct=True),
        FakeOption("B"), FakeOption("C"), FakeOption("D"), FakeOption("E"),
    ]
    judgement = {
        "intended_correct": "C",
        "confidence": 0.95,
        "reasoning_summary": "Explanation explicitly defends C.",
    }
    decision, reason = triage(q, options, judgement)
    assert decision == "flip"
    assert "C" in reason


def test_triage_high_conf_but_heuristic_disagrees_retires():
    """Even at LLM confidence 0.95, if the heuristic finds neither
    the LLM's claim nor the marked option defended, the LLM is
    over-committing on a vague explanation. Skip rather than
    retire -- when both options are equally undefended the LLM is
    likely hallucinating, and retiring valid content is worse than
    missing a (rare) real bug. The launch-time
    ``audit_data_corruption.py`` heuristic is the safety net."""
    from scripts.audit_answer_key_drift import triage
    q = FakeQuestion(
        2, subtype="rc_single",
        # No explicit defense of any option.
        explanation=(
            "The author makes the point that we should consider both "
            "sides. Looking at this question carefully, we see that "
            "the answer is supported by the passage."
        ),
    )
    options = [
        FakeOption("A", is_correct=True),
        FakeOption("B"), FakeOption("C"), FakeOption("D"), FakeOption("E"),
    ]
    judgement = {
        "intended_correct": "C",
        "confidence": 0.95,
        "reasoning_summary": "Looks like C.",
    }
    decision, reason = triage(q, options, judgement)
    assert decision == "skip"
    assert "likely LLM error" in reason.lower() or \
           "keeping question live" in reason.lower()


def test_triage_high_conf_but_heuristic_defends_current_skips():
    """When the heuristic clearly finds the marked-correct option
    defended but the LLM still says flip to a different option, the
    LLM is hallucinating drift. SKIP -- the question is fine."""
    from scripts.audit_answer_key_drift import triage
    q = FakeQuestion(
        3, subtype="rc_single",
        # B is heavily defended; C is never defended.
        explanation=(
            "(B) is correct because the passage explicitly states X. "
            "B fits the meaning of the passage. We pick B as the "
            "right answer here."
        ),
    )
    options = [
        FakeOption("A"),
        FakeOption("B", is_correct=True),
        FakeOption("C"), FakeOption("D"), FakeOption("E"),
    ]
    judgement = {
        "intended_correct": "C",
        "confidence": 0.95,
        "reasoning_summary": "I think it's C.",
    }
    decision, reason = triage(q, options, judgement)
    assert decision == "skip"
    assert "likely LLM error" in reason or "keeping question live" in reason


def test_triage_medium_conf_single_answer_retires():
    from scripts.audit_answer_key_drift import triage
    q = FakeQuestion(2, subtype="mcq_single",
                     explanation="The answer is C because of foo.")
    options = [
        FakeOption("A", "x", is_correct=True),
        FakeOption("B"), FakeOption("C"), FakeOption("D"), FakeOption("E"),
    ]
    judgement = {
        "intended_correct": "C",
        "confidence": 0.78,
        "reasoning_summary": "Lean toward C but reasoning is muddy.",
    }
    decision, _ = triage(q, options, judgement)
    assert decision == "retire"


def test_triage_low_conf_skips():
    from scripts.audit_answer_key_drift import triage
    q = FakeQuestion(3, subtype="mcq_single", explanation="...")
    options = [
        FakeOption("A", is_correct=True),
        FakeOption("B"), FakeOption("C"),
    ]
    judgement = {
        "intended_correct": "C",
        "confidence": 0.4,
        "reasoning_summary": "Not sure.",
    }
    decision, _ = triage(q, options, judgement)
    assert decision == "skip"


def test_triage_judge_agrees_with_key_skips():
    from scripts.audit_answer_key_drift import triage
    q = FakeQuestion(4, subtype="mcq_single", explanation="...")
    options = [
        FakeOption("A"),
        FakeOption("B", is_correct=True),
        FakeOption("C"),
    ]
    judgement = {
        "intended_correct": "B",
        "confidence": 0.99,
        "reasoning_summary": "Defends B.",
    }
    decision, _ = triage(q, options, judgement)
    assert decision == "skip"


def test_triage_null_intended_skips():
    from scripts.audit_answer_key_drift import triage
    q = FakeQuestion(5, subtype="mcq_single", explanation="...")
    options = [FakeOption("A", is_correct=True), FakeOption("B")]
    judgement = {
        "intended_correct": None,
        "confidence": 0.0,
        "reasoning_summary": "",
    }
    decision, _ = triage(q, options, judgement)
    assert decision == "skip"


def test_triage_hallucinated_label_skips():
    """If the LLM returns an option label that doesn't exist on the
    question, we must not blindly trust it. Skip rather than crash or
    incorrectly retire."""
    from scripts.audit_answer_key_drift import triage
    q = FakeQuestion(6, subtype="mcq_single", explanation="...")
    options = [
        FakeOption("A", is_correct=True),
        FakeOption("B"), FakeOption("C"),
    ]
    judgement = {
        "intended_correct": "Z",  # bogus
        "confidence": 0.99,
        "reasoning_summary": "",
    }
    decision, reason = triage(q, options, judgement)
    assert decision == "skip"
    assert "hallucinated" in reason


def test_triage_multi_blank_always_retires_even_high_conf():
    """Multi-blank items default to retire even at high confidence
    because partial flips are worse than no flip — a learner only
    benefits from a fully-corrected answer."""
    from scripts.audit_answer_key_drift import triage
    q = FakeQuestion(7, subtype="tc",
                     explanation="blank1_C and blank2_A are correct.")
    options = [
        FakeOption("blank1_A"),
        FakeOption("blank1_B", is_correct=True),
        FakeOption("blank1_C"),
        FakeOption("blank2_A"),
        FakeOption("blank2_B", is_correct=True),
        FakeOption("blank2_C"),
    ]
    judgement = {
        "intended_correct": ["blank1_C", "blank2_A"],
        "confidence": 0.96,
        "reasoning_summary": "blank1_C and blank2_A.",
    }
    decision, _ = triage(q, options, judgement)
    assert decision == "retire"


def test_triage_multi_answer_partial_retires():
    """mcq_multi where the LLM gives a single label is ambiguous — not
    enough info to flip safely, retire."""
    from scripts.audit_answer_key_drift import triage
    q = FakeQuestion(8, subtype="mcq_multi", explanation="...")
    options = [
        FakeOption("A", is_correct=True),
        FakeOption("B", is_correct=True),
        FakeOption("C"),
        FakeOption("D"),
    ]
    judgement = {
        "intended_correct": "C",
        "confidence": 0.85,
        "reasoning_summary": "Maybe C.",
    }
    decision, _ = triage(q, options, judgement)
    assert decision == "retire"


# ── Audit pre-filter heuristic ────────────────────────────────────


def test_pre_filter_skips_explanations_without_label_mention():
    """Cheap pre-filter: an explanation that never mentions an option
    label has no drift to detect — skip the LLM call."""
    from scripts.audit_answer_key_drift import (
        _explanation_mentions_any_label,
    )
    expl = "Solar power is the answer because it is renewable."
    labels = {"A", "B", "C"}
    # "A" is not in expl as a word boundary; "B" appears in "because"
    # but on its own as B with whitespace around -- it's not. Actually
    # "because" contains "B" but the regex requires word boundary.
    assert not _explanation_mentions_any_label(expl, labels)


def test_pre_filter_admits_explanation_naming_label():
    from scripts.audit_answer_key_drift import (
        _explanation_mentions_any_label,
    )
    expl = "Option C is correct because foo."
    labels = {"A", "B", "C"}
    assert _explanation_mentions_any_label(expl, labels)


def test_pre_filter_admits_blank_label_form():
    from scripts.audit_answer_key_drift import (
        _explanation_mentions_any_label,
    )
    expl = "We pick blank1_C because it fits the contrast."
    labels = {"blank1_A", "blank1_B", "blank1_C"}
    assert _explanation_mentions_any_label(expl, labels)


# ── Migration smoke tests (Peewee, real schema) ───────────────────


@pytest.fixture
def migration_seed_db(temp_db, monkeypatch):
    """Seed three questions into a temp_db so the migration has
    concrete rows to walk:

      q1: marked-correct A, audit decided flip A->C  (will be flipped)
      q2: marked-correct B, audit decided retire (will be retired)
      q3: not in either decision list (untouched)
    """
    from models.database import Question, QuestionOption

    # q1 — flip target
    q1 = Question.create(
        id=9001, measure="verbal", subtype="mcq_single",
        prompt="Pick one.",
        explanation="The answer is C because foo.",
        source="test", status="live",
    )
    QuestionOption.create(
        question=q1, option_label="A", option_text="x", is_correct=True,
    )
    QuestionOption.create(
        question=q1, option_label="B", option_text="y", is_correct=False,
    )
    QuestionOption.create(
        question=q1, option_label="C", option_text="z", is_correct=False,
    )

    # q2 — retire target
    q2 = Question.create(
        id=9002, measure="verbal", subtype="tc",
        prompt="Multi-blank stem.",
        explanation="blank1_C and blank2_A are correct.",
        source="test", status="live",
    )
    for lbl in ("blank1_A", "blank1_B", "blank1_C",
                "blank2_A", "blank2_B", "blank2_C"):
        QuestionOption.create(
            question=q2, option_label=lbl, option_text=lbl,
            is_correct=lbl in ("blank1_B", "blank2_B"),
        )

    # q3 — untouched control
    q3 = Question.create(
        id=9003, measure="verbal", subtype="mcq_single",
        prompt="Untouched.", explanation="...",
        source="test", status="live",
    )
    QuestionOption.create(
        question=q3, option_label="A", option_text="x", is_correct=True,
    )
    QuestionOption.create(
        question=q3, option_label="B", option_text="y", is_correct=False,
    )

    return q1, q2, q3


def test_migration_036_runs_clean_on_empty_db(temp_db, monkeypatch):
    """The migration must no-op cleanly on a fresh DB that doesn't
    carry any of the audit's qids — it shouldn't raise FK errors or
    crash on missing rows."""
    # Inject a flip + retire pair that targets non-existent qids.
    import models.migrations as migrations_mod
    monkeypatch.setattr(
        migrations_mod, "_ANSWER_KEY_FLIPS_2026_05_25",
        [(99999, "A", "C", 0.95, "test")],
    )
    monkeypatch.setattr(
        migrations_mod, "_ANSWER_KEY_RETIRES_2026_05_25",
        [(99998, ["A"], "C", 0.8, "test")],
    )
    # Must not raise.
    migrations_mod._036_answer_key_drift_repair_2026_05_25()


def test_migration_036_via_apply_pending(temp_db):
    """Smoke test: the migration is registered in MIGRATIONS and
    applies cleanly when invoked through ``apply_pending_migrations``
    (the runtime path init_db uses on every launch)."""
    import models.migrations as migrations_mod
    # init_db (called by temp_db fixture) already ran apply_pending,
    # so the migration row should be in the SchemaMigration ledger.
    applied = {m.name for m in
               migrations_mod.SchemaMigration.select(
                   migrations_mod.SchemaMigration.name)}
    assert "036_answer_key_drift_repair_2026_05_25" in applied


def test_migration_036_flips_marked_correct(migration_seed_db, monkeypatch):
    """A flip entry must move ``is_correct=True`` from ``from_label``
    to ``to_label`` and stamp provenance."""
    from models.database import Question, QuestionOption
    import models.migrations as migrations_mod

    q1, _, _ = migration_seed_db
    monkeypatch.setattr(
        migrations_mod, "_ANSWER_KEY_FLIPS_2026_05_25",
        [(q1.id, "A", "C", 0.95, "explanation defends C")],
    )
    monkeypatch.setattr(
        migrations_mod, "_ANSWER_KEY_RETIRES_2026_05_25",
        [],
    )
    migrations_mod._036_answer_key_drift_repair_2026_05_25()

    correct_after = list(
        QuestionOption.select()
        .where(QuestionOption.question == q1)
        .where(QuestionOption.is_correct == True)  # noqa: E712
    )
    assert len(correct_after) == 1
    assert correct_after[0].option_label == "C"

    # Provenance stamped.
    q_reloaded = Question.get_by_id(q1.id)
    prov = json.loads(q_reloaded.provenance_json or "{}")
    assert "answer_key_flipped" in prov
    assert prov["answer_key_flipped"]["from"] == "A"
    assert prov["answer_key_flipped"]["to"] == "C"


def test_migration_036_retires_with_provenance(migration_seed_db, monkeypatch):
    from models.database import Question
    import models.migrations as migrations_mod

    _, q2, _ = migration_seed_db
    monkeypatch.setattr(
        migrations_mod, "_ANSWER_KEY_FLIPS_2026_05_25",
        [],
    )
    monkeypatch.setattr(
        migrations_mod, "_ANSWER_KEY_RETIRES_2026_05_25",
        [(q2.id, ["blank1_B", "blank2_B"],
          ["blank1_C", "blank2_A"], 0.96,
          "multi-blank drift; retire conservatively")],
    )
    migrations_mod._036_answer_key_drift_repair_2026_05_25()

    q_reloaded = Question.get_by_id(q2.id)
    assert q_reloaded.status == "retired"
    prov = json.loads(q_reloaded.provenance_json or "{}")
    assert "answer_key_drift_audit" in prov
    assert "retired_reason" in prov


def test_migration_036_idempotent(migration_seed_db, monkeypatch):
    """Re-running the migration must be a no-op (no double-flip)."""
    from models.database import QuestionOption
    import models.migrations as migrations_mod

    q1, _, _ = migration_seed_db
    monkeypatch.setattr(
        migrations_mod, "_ANSWER_KEY_FLIPS_2026_05_25",
        [(q1.id, "A", "C", 0.95, "explanation defends C")],
    )
    monkeypatch.setattr(
        migrations_mod, "_ANSWER_KEY_RETIRES_2026_05_25",
        [],
    )
    migrations_mod._036_answer_key_drift_repair_2026_05_25()
    migrations_mod._036_answer_key_drift_repair_2026_05_25()  # second run

    correct_after = list(
        QuestionOption.select()
        .where(QuestionOption.question == q1)
        .where(QuestionOption.is_correct == True)  # noqa: E712
    )
    # Still exactly one correct option.
    assert len(correct_after) == 1
    assert correct_after[0].option_label == "C"


def test_migration_036_leaves_untouched_rows_alone(
    migration_seed_db, monkeypatch
):
    from models.database import Question, QuestionOption
    import models.migrations as migrations_mod

    q1, _, q3 = migration_seed_db
    monkeypatch.setattr(
        migrations_mod, "_ANSWER_KEY_FLIPS_2026_05_25",
        [(q1.id, "A", "C", 0.95, "test")],
    )
    monkeypatch.setattr(
        migrations_mod, "_ANSWER_KEY_RETIRES_2026_05_25",
        [],
    )
    migrations_mod._036_answer_key_drift_repair_2026_05_25()

    q3_reloaded = Question.get_by_id(q3.id)
    assert q3_reloaded.status == "live"
    correct_for_q3 = list(
        QuestionOption.select()
        .where(QuestionOption.question == q3)
        .where(QuestionOption.is_correct == True)  # noqa: E712
    )
    assert len(correct_for_q3) == 1
    assert correct_for_q3[0].option_label == "A"


# ── Mocked end-to-end audit run ────────────────────────────────────


class _StubLLMService:
    """Minimal stand-in for ``services.llm_service.llm_service`` so we
    can exercise the audit pipeline without a real network call."""

    def __init__(self, fixed_response: str):
        self._response = fixed_response

    def generate(self, system_prompt, user_prompt, model=None):
        return self._response


def test_audit_pipeline_records_flip_decision(temp_db, monkeypatch, tmp_path):
    """End-to-end: stub the LLM to return a high-confidence drift
    verdict and confirm the script records a flip in the decisions
    JSON. The explanation must defend C strongly enough that the
    heuristic second-opinion seconds the LLM."""
    from models.database import Question, QuestionOption

    # Seed one drift-eligible question with an explanation that
    # heavily defends C (so the heuristic agrees with the LLM).
    q = Question.create(
        id=4001, measure="verbal", subtype="mcq_single",
        prompt="Pick one.",
        explanation=(
            "Option C is the right answer. C fits the contrast. "
            "We pick C as the best choice because foo."
        ),
        source="test", status="live",
    )
    QuestionOption.create(
        question=q, option_label="A", option_text="x", is_correct=True,
    )
    QuestionOption.create(
        question=q, option_label="B", option_text="y", is_correct=False,
    )
    QuestionOption.create(
        question=q, option_label="C", option_text="z", is_correct=False,
    )

    # Stub the LLM service module to return a high-confidence drift.
    import sys
    fake_llm = _StubLLMService(json.dumps({
        "intended_correct": "C",
        "confidence": 0.95,
        "reasoning_summary": "Explanation explicitly defends C.",
    }))
    fake_module = type(sys)("services.llm_service")
    fake_module.llm_service = fake_llm
    monkeypatch.setitem(sys.modules, "services.llm_service", fake_module)

    findings_path = tmp_path / "findings.json"
    decisions_path = tmp_path / "decisions.json"

    from scripts.audit_answer_key_drift import audit_answer_key_drift
    counts = audit_answer_key_drift(
        limit=10,
        resume=False,
        dry_run=False,
        skip_no_label_mention=False,
        findings_path=findings_path,
        decisions_path=decisions_path,
    )
    assert counts["flip"] == 1
    decisions = json.loads(decisions_path.read_text())
    assert decisions["flip_count"] == 1
    flip = decisions["flips"][0]
    assert flip["qid"] == 4001
    assert flip["from_correct"] == ["A"]
    assert flip["to_correct"] == "C"
