"""Regression tests for ``_037_quant_curated_audit_2026_05_26``.

The migration is data-driven: three static lists in
``models/migrations.py`` define the flips, retires, and explanation
repairs to apply. These tests exercise the migration's apply path
against a temp_db without depending on the production decision lists
— they monkeypatch the lists with controlled fixtures.

Why this style: the static lists may be empty or non-empty depending
on what the audit found; the tests must pass either way. We verify
the apply primitives (flip, retire, repair) and idempotence.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

import pytest


@pytest.fixture(autouse=True)
def _evict_migrations_module():
    """Each test re-imports models.migrations so module-level Peewee
    bindings re-attach to the active temp_db."""
    for prefix in ("models.migrations",):
        for mod in [m for m in list(sys.modules)
                    if m == prefix or m.startswith(prefix + ".")]:
            del sys.modules[mod]
    yield


@pytest.fixture
def quant_audit_seed_db(temp_db, monkeypatch):
    """Seed four representative quant questions:

    qA (8001): currently A is correct, audit decided flip A -> C.
    qB (8002): currently A is correct, audit decided retire.
    qC (8003): currently B is correct, audit decided repair_explanation.
    qD (8004): not in any list — must remain untouched.
    """
    from models.database import Question, QuestionOption

    qA = Question.create(
        id=8001, measure="quant", subtype="mcq_single",
        topic="arithmetic", subtopic="ratios",
        prompt="qA prompt",
        explanation="qA explanation",
        source="test", status="live",
    )
    for lbl, ic in (("A", True), ("B", False), ("C", False),
                    ("D", False), ("E", False)):
        QuestionOption.create(
            question=qA, option_label=lbl, option_text=lbl, is_correct=ic,
        )

    qB = Question.create(
        id=8002, measure="quant", subtype="mcq_single",
        topic="geometry", subtopic="triangles",
        prompt="qB prompt — references unseen figure",
        explanation="qB explanation",
        source="test", status="live",
    )
    for lbl, ic in (("A", True), ("B", False), ("C", False),
                    ("D", False), ("E", False)):
        QuestionOption.create(
            question=qB, option_label=lbl, option_text=lbl, is_correct=ic,
        )

    qC = Question.create(
        id=8003, measure="quant", subtype="qc",
        topic="algebra", subtopic="functions",
        prompt="qC prompt",
        explanation="qC explanation that is wrong / off-topic",
        source="test", status="live",
    )
    for lbl, ic in (("A", False), ("B", True),
                    ("C", False), ("D", False)):
        QuestionOption.create(
            question=qC, option_label=lbl, option_text=lbl, is_correct=ic,
        )

    qD = Question.create(
        id=8004, measure="quant", subtype="mcq_single",
        topic="data_analysis", subtopic="probability",
        prompt="qD prompt",
        explanation="qD explanation",
        source="test", status="live",
    )
    for lbl, ic in (("A", True), ("B", False), ("C", False),
                    ("D", False), ("E", False)):
        QuestionOption.create(
            question=qD, option_label=lbl, option_text=lbl, is_correct=ic,
        )

    return qA, qB, qC, qD


def test_037_runs_clean_on_empty_lists(temp_db, monkeypatch):
    """The migration must no-op cleanly when all three lists are empty
    — the production case may have zero confirmed flips and any
    combination of retires / repairs."""
    import models.migrations as m
    monkeypatch.setattr(m, "_QUANT_AUDIT_FLIPS_2026_05_26", [])
    monkeypatch.setattr(m, "_QUANT_AUDIT_RETIRES_2026_05_26", [])
    monkeypatch.setattr(m, "_QUANT_AUDIT_REPAIRS_2026_05_26", [])
    # Must not raise.
    m._037_quant_curated_audit_2026_05_26()


def test_037_runs_clean_on_missing_qids(temp_db, monkeypatch):
    """Entries that target qids not present in the DB (e.g. on a
    test fixture) must be silently skipped, not raise FK errors."""
    import models.migrations as m
    monkeypatch.setattr(m, "_QUANT_AUDIT_FLIPS_2026_05_26",
                        [(99999, "A", "C", 0.95, "test")])
    monkeypatch.setattr(m, "_QUANT_AUDIT_RETIRES_2026_05_26",
                        [(99998, ["A"], 0.85, "test")])
    monkeypatch.setattr(m, "_QUANT_AUDIT_REPAIRS_2026_05_26",
                        [(99997, "explanation", "", "test")])
    m._037_quant_curated_audit_2026_05_26()


def test_037_via_apply_pending_registers_in_ledger(temp_db):
    """Smoke test: the migration is registered in MIGRATIONS and
    applied by init_db's apply_pending path."""
    import models.migrations as m
    applied = {row.name for row in
               m.SchemaMigration.select(m.SchemaMigration.name)}
    assert "037_quant_curated_audit_2026_05_26" in applied


def test_037_flips_marked_correct(quant_audit_seed_db, monkeypatch):
    from models.database import Question, QuestionOption
    import models.migrations as m

    qA, _, _, _ = quant_audit_seed_db
    monkeypatch.setattr(m, "_QUANT_AUDIT_FLIPS_2026_05_26", [
        (qA.id, "A", "C", 0.95,
         "audit 2026-05-26: independent solve and explanation both "
         "conclude C; key marks A"),
    ])
    monkeypatch.setattr(m, "_QUANT_AUDIT_RETIRES_2026_05_26", [])
    monkeypatch.setattr(m, "_QUANT_AUDIT_REPAIRS_2026_05_26", [])
    m._037_quant_curated_audit_2026_05_26()

    correct = list(
        QuestionOption.select()
        .where(QuestionOption.question == qA)
        .where(QuestionOption.is_correct == True)  # noqa: E712
    )
    assert len(correct) == 1
    assert correct[0].option_label == "C"

    q_reloaded = Question.get_by_id(qA.id)
    prov = json.loads(q_reloaded.provenance_json or "{}")
    assert prov.get("answer_key_flipped", {}).get("from") == "A"
    assert prov.get("answer_key_flipped", {}).get("to") == "C"
    assert prov.get("answer_key_flipped_by_migration", "").endswith(
        "037_quant_curated_audit_2026_05_26"
    )


def test_037_retires_with_provenance(quant_audit_seed_db, monkeypatch):
    from models.database import Question
    import models.migrations as m

    _, qB, _, _ = quant_audit_seed_db
    monkeypatch.setattr(m, "_QUANT_AUDIT_FLIPS_2026_05_26", [])
    monkeypatch.setattr(m, "_QUANT_AUDIT_RETIRES_2026_05_26", [
        (qB.id, ["A"], 0.85,
         "audit 2026-05-26: prompt references a figure that is "
         "not attached as a stimulus"),
    ])
    monkeypatch.setattr(m, "_QUANT_AUDIT_REPAIRS_2026_05_26", [])
    m._037_quant_curated_audit_2026_05_26()

    q = Question.get_by_id(qB.id)
    assert q.status == "retired"
    prov = json.loads(q.provenance_json or "{}")
    assert "retired_reason" in prov
    assert prov.get("retired_by_migration", "").endswith(
        "037_quant_curated_audit_2026_05_26"
    )
    assert "quant_curated_audit" in prov


def test_037_repair_blanks_explanation(quant_audit_seed_db, monkeypatch):
    from models.database import Question
    import models.migrations as m

    _, _, qC, _ = quant_audit_seed_db
    original_len = len(qC.explanation)
    assert original_len > 0

    monkeypatch.setattr(m, "_QUANT_AUDIT_FLIPS_2026_05_26", [])
    monkeypatch.setattr(m, "_QUANT_AUDIT_RETIRES_2026_05_26", [])
    monkeypatch.setattr(m, "_QUANT_AUDIT_REPAIRS_2026_05_26", [
        (qC.id, "explanation", "",
         "audit 2026-05-26: explanation is off-topic; key is correct"),
    ])
    m._037_quant_curated_audit_2026_05_26()

    q = Question.get_by_id(qC.id)
    assert q.explanation == ""
    prov = json.loads(q.provenance_json or "{}")
    repairs = prov.get("explanation_repairs") or []
    assert any(r.get("previous_len") == original_len for r in repairs)


def test_037_idempotent_double_run(quant_audit_seed_db, monkeypatch):
    """Re-running the migration must be a no-op: no double-flip on the
    options, no double provenance entries beyond the first apply."""
    from models.database import Question, QuestionOption
    import models.migrations as m

    qA, _, _, _ = quant_audit_seed_db
    monkeypatch.setattr(m, "_QUANT_AUDIT_FLIPS_2026_05_26", [
        (qA.id, "A", "C", 0.95, "audit 2026-05-26: idempotent fixture"),
    ])
    monkeypatch.setattr(m, "_QUANT_AUDIT_RETIRES_2026_05_26", [])
    monkeypatch.setattr(m, "_QUANT_AUDIT_REPAIRS_2026_05_26", [])
    m._037_quant_curated_audit_2026_05_26()
    m._037_quant_curated_audit_2026_05_26()  # second run

    correct = list(
        QuestionOption.select()
        .where(QuestionOption.question == qA)
        .where(QuestionOption.is_correct == True)  # noqa: E712
    )
    assert len(correct) == 1
    assert correct[0].option_label == "C"


def test_037_leaves_untouched_rows_alone(quant_audit_seed_db, monkeypatch):
    from models.database import Question, QuestionOption
    import models.migrations as m

    qA, _, _, qD = quant_audit_seed_db
    monkeypatch.setattr(m, "_QUANT_AUDIT_FLIPS_2026_05_26", [
        (qA.id, "A", "C", 0.95, "audit 2026-05-26: untouched fixture"),
    ])
    monkeypatch.setattr(m, "_QUANT_AUDIT_RETIRES_2026_05_26", [])
    monkeypatch.setattr(m, "_QUANT_AUDIT_REPAIRS_2026_05_26", [])
    m._037_quant_curated_audit_2026_05_26()

    qD_after = Question.get_by_id(qD.id)
    assert qD_after.status == "live"
    correct_qD = list(
        QuestionOption.select()
        .where(QuestionOption.question == qD)
        .where(QuestionOption.is_correct == True)  # noqa: E712
    )
    assert len(correct_qD) == 1
    assert correct_qD[0].option_label == "A"


def test_037_skips_already_retired_question(quant_audit_seed_db, monkeypatch):
    """A question already retired by an earlier migration must not be
    re-touched, and must not have its options flipped after retirement."""
    from models.database import Question, QuestionOption
    import models.migrations as m

    qA, _, _, _ = quant_audit_seed_db
    # Pre-retire qA.
    qA.status = "retired"
    qA.save()

    monkeypatch.setattr(m, "_QUANT_AUDIT_FLIPS_2026_05_26", [
        (qA.id, "A", "C", 0.95, "audit 2026-05-26: should be skipped"),
    ])
    monkeypatch.setattr(m, "_QUANT_AUDIT_RETIRES_2026_05_26", [])
    monkeypatch.setattr(m, "_QUANT_AUDIT_REPAIRS_2026_05_26", [])
    m._037_quant_curated_audit_2026_05_26()

    # Options should still have A as correct (no flip applied because
    # the question was retired before the flip arrived).
    correct = list(
        QuestionOption.select()
        .where(QuestionOption.question == qA)
        .where(QuestionOption.is_correct == True)  # noqa: E712
    )
    assert len(correct) == 1
    assert correct[0].option_label == "A"


def test_037_static_lists_have_expected_shape():
    """Light schema check on the production lists: each entry is a
    tuple with the right number of fields and well-typed pieces.
    The lists may be empty — that's allowed — but if they're non-empty
    they must conform."""
    import models.migrations as m

    for entry in m._QUANT_AUDIT_FLIPS_2026_05_26:
        assert len(entry) == 5, f"flip entry shape: {entry!r}"
        qid, from_label, to_label, conf, reason = entry
        assert isinstance(qid, int)
        assert isinstance(from_label, str) and from_label
        assert isinstance(to_label, str) and to_label
        assert from_label != to_label
        assert isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0
        assert isinstance(reason, str) and reason

    for entry in m._QUANT_AUDIT_RETIRES_2026_05_26:
        assert len(entry) == 4, f"retire entry shape: {entry!r}"
        qid, current, conf, reason = entry
        assert isinstance(qid, int)
        assert isinstance(current, list)
        assert isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0
        assert isinstance(reason, str) and reason

    for entry in m._QUANT_AUDIT_REPAIRS_2026_05_26:
        assert len(entry) == 4, f"repair entry shape: {entry!r}"
        qid, field, new_value, reason = entry
        assert isinstance(qid, int)
        assert field == "explanation"  # only supported field today
        assert isinstance(new_value, str)
        assert isinstance(reason, str) and reason


# ── Tests for _038_targeted_issue_fixes_2026_05_27 ─────────────────


@pytest.fixture
def targeted_issue_seed_db(temp_db, monkeypatch):
    """Seed two questions for the 038 retire path:

    qX (8101): live mcq_multi the audit decided to retire.
    qY (8102): live mcq_single not in any list — must remain untouched.
    """
    from models.database import Question, QuestionOption

    qX = Question.create(
        id=8101, measure="quant", subtype="mcq_multi",
        topic="geometry", subtopic="cones",
        prompt="qX prompt — options incoherent with prompt units",
        explanation="qX explanation",
        source="test", status="live",
    )
    for lbl, ic in (("A", True), ("B", False), ("C", True),
                    ("D", False), ("E", True), ("F", False),
                    ("G", False)):
        QuestionOption.create(
            question=qX, option_label=lbl, option_text=lbl, is_correct=ic,
        )

    qY = Question.create(
        id=8102, measure="quant", subtype="mcq_single",
        topic="data_analysis", subtopic="probability",
        prompt="qY prompt",
        explanation="qY explanation",
        source="test", status="live",
    )
    for lbl, ic in (("A", True), ("B", False), ("C", False),
                    ("D", False), ("E", False)):
        QuestionOption.create(
            question=qY, option_label=lbl, option_text=lbl, is_correct=ic,
        )

    return qX, qY


def test_038_runs_clean_on_empty_list(temp_db, monkeypatch):
    import models.migrations as m
    monkeypatch.setattr(m, "_TARGETED_ISSUE_RETIRES_2026_05_27", [])
    m._038_targeted_issue_fixes_2026_05_27()


def test_038_runs_clean_on_missing_qids(temp_db, monkeypatch):
    """Entries pointing at qids absent from the DB must silently skip,
    not raise — needed so the migration is safe on test fixtures."""
    import models.migrations as m
    monkeypatch.setattr(m, "_TARGETED_ISSUE_RETIRES_2026_05_27",
                        [(99996, ["A"], 0.9, "test missing qid")])
    m._038_targeted_issue_fixes_2026_05_27()


def test_038_via_apply_pending_registers_in_ledger(temp_db):
    """Smoke: the migration is registered in MIGRATIONS and applied
    by init_db's apply_pending path."""
    import models.migrations as m
    applied = {row.name for row in
               m.SchemaMigration.select(m.SchemaMigration.name)}
    assert "038_targeted_issue_fixes_2026_05_27" in applied


def test_038_retires_with_provenance(targeted_issue_seed_db, monkeypatch):
    from models.database import Question
    import models.migrations as m

    qX, _ = targeted_issue_seed_db
    monkeypatch.setattr(m, "_TARGETED_ISSUE_RETIRES_2026_05_27", [
        (qX.id, ["A", "C", "E"], 0.9,
         "audit 2026-05-27 (issue #99): mcq_multi options incoherent "
         "with the prompt's units"),
    ])
    m._038_targeted_issue_fixes_2026_05_27()

    q = Question.get_by_id(qX.id)
    assert q.status == "retired"
    prov = json.loads(q.provenance_json or "{}")
    assert "retired_reason" in prov
    assert prov.get("retired_by_migration", "").endswith(
        "038_targeted_issue_fixes_2026_05_27"
    )
    assert "targeted_issue_audit" in prov
    assert prov["targeted_issue_audit"]["current_correct"] == ["A", "C", "E"]


def test_038_idempotent_double_run(targeted_issue_seed_db, monkeypatch):
    """Re-running the migration is a no-op once a question is retired."""
    from models.database import Question
    import models.migrations as m

    qX, _ = targeted_issue_seed_db
    monkeypatch.setattr(m, "_TARGETED_ISSUE_RETIRES_2026_05_27", [
        (qX.id, ["A", "C", "E"], 0.9,
         "audit 2026-05-27: idempotent fixture"),
    ])
    m._038_targeted_issue_fixes_2026_05_27()
    q1 = Question.get_by_id(qX.id)
    prov1 = json.loads(q1.provenance_json or "{}")

    m._038_targeted_issue_fixes_2026_05_27()  # second run
    q2 = Question.get_by_id(qX.id)
    prov2 = json.loads(q2.provenance_json or "{}")

    assert q2.status == "retired"
    # The second call short-circuits on status='retired' so provenance
    # is unchanged.
    assert prov1 == prov2


def test_038_leaves_untouched_rows_alone(targeted_issue_seed_db,
                                         monkeypatch):
    from models.database import Question, QuestionOption
    import models.migrations as m

    qX, qY = targeted_issue_seed_db
    monkeypatch.setattr(m, "_TARGETED_ISSUE_RETIRES_2026_05_27", [
        (qX.id, ["A", "C", "E"], 0.9, "audit 2026-05-27: untouched"),
    ])
    m._038_targeted_issue_fixes_2026_05_27()

    qY_after = Question.get_by_id(qY.id)
    assert qY_after.status == "live"
    correct_qY = list(
        QuestionOption.select()
        .where(QuestionOption.question == qY)
        .where(QuestionOption.is_correct == True)  # noqa: E712
    )
    assert len(correct_qY) == 1
    assert correct_qY[0].option_label == "A"


def test_038_skips_already_retired_question(targeted_issue_seed_db,
                                            monkeypatch):
    """A question retired by an earlier migration must not be
    re-touched (no provenance overwrite)."""
    from models.database import Question
    import models.migrations as m

    qX, _ = targeted_issue_seed_db
    qX.status = "retired"
    qX.provenance_json = json.dumps(
        {"retired_by_migration": "previous_migration"}
    )
    qX.save()

    monkeypatch.setattr(m, "_TARGETED_ISSUE_RETIRES_2026_05_27", [
        (qX.id, ["A", "C", "E"], 0.9,
         "audit 2026-05-27: should be skipped"),
    ])
    m._038_targeted_issue_fixes_2026_05_27()

    q = Question.get_by_id(qX.id)
    prov = json.loads(q.provenance_json or "{}")
    # Must not have been overwritten — the prior migration's marker
    # is still intact and our marker is absent.
    assert prov.get("retired_by_migration") == "previous_migration"
    assert "targeted_issue_audit" not in prov


def test_038_static_list_has_expected_shape():
    """Schema check on the production retire list."""
    import models.migrations as m

    for entry in m._TARGETED_ISSUE_RETIRES_2026_05_27:
        assert len(entry) == 4, f"retire entry shape: {entry!r}"
        qid, current, conf, reason = entry
        assert isinstance(qid, int)
        assert isinstance(current, list)
        assert isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0
        assert isinstance(reason, str) and reason
        # Reasons should cite a github issue number for traceability.
        assert "issue #" in reason

