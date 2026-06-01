"""Tests for WS-D GRE faithfulness: QC canonicalization (migration 042),
current-format structure constants, and a production-data conformance guard.
"""
from __future__ import annotations

import os
import sys
import sqlite3

import pytest

QC_CANON = [
    "Quantity A is greater.",
    "Quantity B is greater.",
    "The two quantities are equal.",
    "The relationship cannot be determined from the information given.",
]


@pytest.fixture(autouse=True)
def _evict_migrations_module():
    for mod in [m for m in list(sys.modules)
                if m == "models.migrations" or m.startswith("models.migrations.")]:
        del sys.modules[mod]
    yield


def test_042_registered(temp_db):
    import models.migrations as m
    applied = {r.name for r in m.SchemaMigration.select(m.SchemaMigration.name)}
    assert "042_canonical_qc_options_2026_06_01" in applied


def test_042_normalizes_qc_text_preserving_correctness(temp_db):
    from models.database import Question, QuestionOption
    import models.migrations as m
    q = Question.create(id=9501, measure="quant", subtype="qc",
                        prompt="compare", source="ai_generated", status="live")
    # canonical ORDER, missing periods; B marked correct
    for lbl, txt, ic in (("A", "Quantity A is greater", False),
                         ("B", "Quantity B is greater", True),
                         ("C", "The two quantities are equal", False),
                         ("D", "The relationship cannot be determined from "
                               "the information given", False)):
        QuestionOption.create(question=q, option_label=lbl,
                              option_text=txt, is_correct=ic)
    m._042_canonical_qc_options_2026_06_01()
    opts = list(QuestionOption.select().where(QuestionOption.question == q)
                .order_by(QuestionOption.option_label))
    assert [o.option_text for o in opts] == QC_CANON
    # correctness preserved (B still the only correct)
    assert [o.option_label for o in opts if o.is_correct] == ["B"]


def test_042_idempotent(temp_db):
    import models.migrations as m
    m._042_canonical_qc_options_2026_06_01()
    m._042_canonical_qc_options_2026_06_01()  # must not raise


def test_section_structure_matches_current_gre():
    """Current (post-Sept-2023) short GRE: AWA 1 task/30m, V1 12Q/18m,
    V2 15Q/23m, Q1 12Q/21m, Q2 15Q/26m."""
    import config as c
    assert c.AWA_TIME == 30 * 60
    assert (c.VERBAL_S1_COUNT, c.VERBAL_S1_TIME) == (12, 18 * 60)
    assert (c.VERBAL_S2_COUNT, c.VERBAL_S2_TIME) == (15, 23 * 60)
    assert (c.QUANT_S1_COUNT, c.QUANT_S1_TIME) == (12, 21 * 60)
    assert (c.QUANT_S2_COUNT, c.QUANT_S2_TIME) == (15, 26 * 60)
    from models.exam_session import SECTION_META, SectionType
    assert SECTION_META[SectionType.AWA][3] == 1  # exactly one AWA task


def test_scoring_multi_select_is_all_or_nothing():
    from services.scoring import ScoringEngine
    options = [{"label": "A", "is_correct": True},
               {"label": "B", "is_correct": True},
               {"label": "C", "is_correct": False}]
    qd = {"subtype": "mcq_multi", "options": options}
    assert ScoringEngine.check_answer(qd, {"selected": ["A", "B"]}) is True
    # missing one correct -> no credit (all-or-nothing)
    assert ScoringEngine.check_answer(qd, {"selected": ["A"]}) is False
    # extra wrong -> no credit
    assert ScoringEngine.check_answer(qd, {"selected": ["A", "B", "C"]}) is False


def test_production_seed_is_shape_conformant():
    """The shipped seed must have zero GRE-shape violations among live items."""
    db = "data/gre_mock.db"
    if not os.path.exists(db):
        pytest.skip("seed db not present")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scripts.audit_faithfulness import audit
    conn = sqlite3.connect(db)
    _rows, viol = audit(conn, live_only=True)
    conn.close()
    total = sum(len(v) for v in viol.values())
    assert total == 0, f"shape violations in seed: {dict((k, len(v)) for k, v in viol.items())}"
