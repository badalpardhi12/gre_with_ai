"""Regression tests for ``_039_repair_option_graft_2026_06_01``.

The migration rewrites the questionoption rows of option-grafted mcq_multi
items to reconstructed-and-verified original sets, and retires the items whose
options are unrecoverable. Tests exercise the apply primitives against a
temp_db with controlled fixtures (not the production decision lists), plus a
schema check on the production list so a malformed entry can't ship silently.
"""
from __future__ import annotations

import json
import sys

import pytest


@pytest.fixture(autouse=True)
def _evict_migrations_module():
    for mod in [m for m in list(sys.modules)
                if m == "models.migrations" or m.startswith("models.migrations.")]:
        del sys.modules[mod]
    yield


@pytest.fixture
def graft_seed_db(temp_db):
    """Seed three mcq_multi items:

    qR (8101): grafted options -> repair to {2,4 correct; 1,3 wrong}.
    qX (8102): unrecoverable    -> retire.
    qU (8103): not in any list  -> must remain untouched.
    """
    from models.database import Question, QuestionOption

    qR = Question.create(
        id=8101, measure="quant", subtype="mcq_multi",
        prompt="qR prompt", explanation="qR explanation",
        source="ai_synthetic_v2", status="live",
    )
    for lbl, txt, ic in (("A", "999", True), ("B", "998", False),
                         ("C", "997", True)):  # grafted/wrong, 3 options
        QuestionOption.create(question=qR, option_label=lbl,
                              option_text=txt, is_correct=ic)

    qX = Question.create(
        id=8102, measure="quant", subtype="mcq_multi",
        prompt="qX prompt", explanation="qX explanation",
        source="princeton_2012", status="live",
    )
    QuestionOption.create(question=qX, option_label="A", option_text="z", is_correct=True)

    qU = Question.create(
        id=8103, measure="quant", subtype="mcq_multi",
        prompt="qU prompt", explanation="qU explanation",
        source="ai_synthetic_v2", status="live",
    )
    for lbl, ic in (("A", True), ("B", False)):
        QuestionOption.create(question=qU, option_label=lbl,
                              option_text=lbl, is_correct=ic)
    return qR, qX, qU


def _run(monkeypatch, repairs, retires):
    import models.migrations as m
    monkeypatch.setattr(m, "_OPTION_GRAFT_REPAIRS_2026_06_01", repairs)
    monkeypatch.setattr(m, "_OPTION_GRAFT_RETIRES_2026_06_01", retires)
    m._039_repair_option_graft_2026_06_01()
    return m


def test_039_repairs_options_and_flags(graft_seed_db, monkeypatch):
    from models.database import Question, QuestionOption
    qR, _, _ = graft_seed_db
    _run(monkeypatch,
         [(qR.id, [("1", 0), ("2", 1), ("3", 0), ("4", 1)])],
         [])
    opts = list(QuestionOption.select()
                .where(QuestionOption.question == qR)
                .order_by(QuestionOption.option_label))
    assert [o.option_text for o in opts] == ["1", "2", "3", "4"]
    assert [o.option_label for o in opts] == ["A", "B", "C", "D"]
    assert [bool(o.is_correct) for o in opts] == [False, True, False, True]
    prov = json.loads(Question.get_by_id(qR.id).provenance_json or "{}")
    assert prov["option_graft_repair"]["by_migration"].endswith(
        "039_repair_option_graft_2026_06_01")
    # the grafted options are snapshotted for auditability
    assert {g["text"] for g in prov["option_graft_repair"]["grafted_options"]} == {
        "999", "998", "997"}


def test_039_idempotent(graft_seed_db, monkeypatch):
    from models.database import QuestionOption
    qR, _, _ = graft_seed_db
    repairs = [(qR.id, [("1", 0), ("2", 1), ("3", 0), ("4", 1)])]
    m = _run(monkeypatch, repairs, [])
    m._039_repair_option_graft_2026_06_01()  # second pass -> skip
    n = (QuestionOption.select()
         .where(QuestionOption.question == qR).count())
    assert n == 4  # not doubled


def test_039_retires_unrecoverable(graft_seed_db, monkeypatch):
    from models.database import Question
    _, qX, _ = graft_seed_db
    _run(monkeypatch, [], [(qX.id, "unrecoverable: figure missing")])
    q = Question.get_by_id(qX.id)
    assert q.status == "retired"
    prov = json.loads(q.provenance_json or "{}")
    assert prov["retired_reason"] == "unrecoverable: figure missing"


def test_039_leaves_untouched_rows_alone(graft_seed_db, monkeypatch):
    from models.database import Question, QuestionOption
    qR, _, qU = graft_seed_db
    _run(monkeypatch, [(qR.id, [("1", 1), ("2", 0)])], [])
    assert Question.get_by_id(qU.id).status == "live"
    texts = [o.option_text for o in QuestionOption.select()
             .where(QuestionOption.question == qU)]
    assert sorted(texts) == ["A", "B"]


def test_039_skips_missing_qids(temp_db, monkeypatch):
    _run(monkeypatch, [(990001, [("1", 1), ("2", 0)])],
         [(990002, "absent")])  # must not raise


def test_039_empty_lists_noop(temp_db, monkeypatch):
    _run(monkeypatch, [], [])  # must not raise


def test_039_registered_in_ledger(temp_db):
    import models.migrations as m
    applied = {row.name for row in m.SchemaMigration.select(m.SchemaMigration.name)}
    assert "039_repair_option_graft_2026_06_01" in applied


def test_039_production_list_is_well_formed():
    """Every production repair entry must be a valid mcq_multi option set:
    >=2 options, at least one correct and at least one wrong, no duplicate
    option texts, and every is_correct flag a 0/1 int."""
    import models.migrations as m
    for qid, options in m._OPTION_GRAFT_REPAIRS_2026_06_01:
        assert len(options) >= 2, qid
        corr = [t for t, ic in options if ic]
        wrong = [t for t, ic in options if not ic]
        assert corr and wrong, f"q{qid} must have both correct and wrong options"
        texts = [t for t, _ in options]
        assert len(texts) == len(set(texts)), f"q{qid} has duplicate option texts"
        assert all(ic in (0, 1, True, False) for _, ic in options), qid
    # retires must cite a non-empty reason
    for qid, reason in m._OPTION_GRAFT_RETIRES_2026_06_01:
        assert isinstance(reason, str) and reason.strip(), qid


def test_039_known_live_repairs_match_expected_correct_sets():
    """Lock the verified correct sets for the live grafts the user reported,
    so a future edit can't silently regress them (GitHub #38, #41)."""
    import models.migrations as m
    by_qid = dict(m._OPTION_GRAFT_REPAIRS_2026_06_01)
    expected = {
        5378: {"1", "2", "5", "8"},          # issue #41
        5384: {"12", "24", "36", "48"},      # issue #38
        5375: {"S is divisible by 3", "S is divisible by 6",
               "S/3 is even", "S + 6 is divisible by 6"},
    }
    for qid, want in expected.items():
        assert qid in by_qid, f"q{qid} missing from repair list"
        got = {t for t, ic in by_qid[qid] if ic}
        assert got == want, f"q{qid} correct set regressed: {got} != {want}"
