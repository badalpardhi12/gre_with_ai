"""Tests for WS-C: duplicate-group dedup at assembly + wider cross-mock window.

Covers migration 041 (add column / populate groups / retire exact dupes), the
``_groups_for`` helper + within/cross-call group dedup in assembly, and the
``get_recent_mock_qids`` window.
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


# ── migration 041 ────────────────────────────────────────────────────────

def test_041_registered_and_column_present(temp_db):
    import models.migrations as m
    from models.database import Question
    applied = {r.name for r in m.SchemaMigration.select(m.SchemaMigration.name)}
    assert "041_duplicate_group_id_2026_06_01" in applied
    assert hasattr(Question, "duplicate_group_id")


def test_041_idempotent_column_add(temp_db):
    import models.migrations as m
    # second run must not raise (column already exists)
    m._041_duplicate_group_id_2026_06_01()
    m._041_duplicate_group_id_2026_06_01()


def test_duplicate_group_id_not_model_indexed():
    """Regression guard for the 'database disk image is malformed' bug.

    duplicate_group_id is added to an existing table by migration 041, so its
    index MUST be created by the migration (after the column exists). If the
    model declared index=True, create_tables() — which runs BEFORE migrations
    on launch — would build the index on a not-yet-existing column on upgraded
    user DBs and corrupt the file on first restart after pull.
    """
    from models.database import Question
    field = Question._meta.fields["duplicate_group_id"]
    assert not getattr(field, "index", False), (
        "duplicate_group_id must NOT be index=True; migration 041 owns the index")


def test_041_creates_index_after_column(temp_db):
    """Migration 041 must leave the duplicate_group_id index in place."""
    from models.database import db
    idx = [r[0] for r in db.execute_sql(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='question_duplicate_group_id'").fetchall()]
    assert idx == ["question_duplicate_group_id"]


def test_041_populates_groups_and_retires_dupes(temp_db, monkeypatch):
    from models.database import Question
    import models.migrations as m
    # seed the members of one group + an exact-dupe pair
    for qid in (1657, 2224, 2228, 2232):
        Question.create(id=qid, measure="quant", subtype="mcq_single",
                        prompt=f"q{qid}", source="ai_generated", status="live")
    m._041_duplicate_group_id_2026_06_01()
    assert Question.get_by_id(1657).duplicate_group_id == "dg_q_g3"
    assert Question.get_by_id(2224).duplicate_group_id == "dg_q_g3"
    # exact dupe 2232 retired in favor of 1657
    assert Question.get_by_id(2232).status == "retired"
    prov = json.loads(Question.get_by_id(2232).provenance_json or "{}")
    assert "duplicate of q1657" in prov["retired_reason"]


# ── assembly dedup ─────────────────────────────────────────────────────────

@pytest.fixture
def grouped_quant_pool(temp_db):
    """A quant pool where two items share a duplicate_group_id plus filler."""
    from models.database import Question, QuestionOption
    made = []
    # two same-group items
    for qid in (9101, 9102):
        q = Question.create(id=qid, measure="quant", subtype="mcq_single",
                            prompt=f"grouped {qid}", source="ai_generated",
                            status="live", difficulty_target=3,
                            duplicate_group_id="dg_test")
        for lbl in "ABCDE":
            QuestionOption.create(question=q, option_label=lbl,
                                  option_text=lbl, is_correct=(lbl == "A"))
        made.append(qid)
    # filler distinct items so a section can still be filled
    for qid in range(9200, 9240):
        q = Question.create(id=qid, measure="quant", subtype="mcq_single",
                            prompt=f"filler {qid}", source="ai_generated",
                            status="live", difficulty_target=3)
        for lbl in "ABCDE":
            QuestionOption.create(question=q, option_label=lbl,
                                  option_text=lbl, is_correct=(lbl == "A"))
    return made


def test_groups_for_helper(grouped_quant_pool):
    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    assert qb._groups_for([9101]) == {"dg_test"}
    assert qb._groups_for([9101, 9102]) == {"dg_test"}
    assert qb._groups_for([9200]) == set()      # filler has empty group
    assert qb._groups_for([]) == set()


def test_assembly_never_co_places_group_members(grouped_quant_pool):
    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    for _ in range(30):
        ids = set(qb.select_questions_composed(
            measure="quant", count=12, difficulty_band="medium"))
        assert not ({9101, 9102} <= ids), "both group members co-occur"


def test_assembly_excludes_group_sibling_of_excluded(grouped_quant_pool):
    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    # excluding 9101 must also keep its sibling 9102 out (group dedup via exclude)
    for _ in range(20):
        ids = set(qb.select_questions_composed(
            measure="quant", count=12, difficulty_band="medium",
            exclude_ids=[9101]))
        assert 9102 not in ids


# ── cross-mock window ──────────────────────────────────────────────────────

def test_get_recent_mock_qids_window(temp_db):
    """get_recent_mock_qids(n) unions the n most recent completed mocks;
    get_previous_mock_qids returns only the latest."""
    from datetime import datetime, timedelta
    from models.database import Session, SectionResult
    import models.exam_session as es

    base = datetime(2026, 1, 1)
    specs = [("m1", [1, 2]), ("m2", [3, 4]), ("m3", [5, 6]), ("m4", [7, 8])]
    for i, (name, qids) in enumerate(specs):
        s = Session.create(test_type="full_mock", state="completed",
                           started_at=base + timedelta(days=i),
                           ended_at=base + timedelta(days=i, hours=2))
        SectionResult.create(session=s, section_name=f"{name}_quant_s1",
                             measure="quant", section_index=1,
                             time_limit_seconds=1260,
                             question_ids=json.dumps(qids))
    # latest mock only
    assert es.get_previous_mock_qids() == {7, 8}
    # last 3 mocks (m2,m3,m4)
    assert es.get_recent_mock_qids(n=3) == {3, 4, 5, 6, 7, 8}
    # n larger than available -> all
    assert es.get_recent_mock_qids(n=10) == {1, 2, 3, 4, 5, 6, 7, 8}
