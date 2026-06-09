"""Tests for migration 044 (user-reported fixes #42/#43/#44) and the runtime
seed-write guard that fixes 'git pull doesn't update the .db'.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest


@pytest.fixture(autouse=True)
def _evict_migrations_module():
    for mod in [m for m in list(sys.modules)
                if m == "models.migrations" or m.startswith("models.migrations.")]:
        del sys.modules[mod]
    yield


# ── seed-write guard (the git-pull fix) ────────────────────────────────────

def test_seed_writes_disabled_by_default():
    """At normal launch (no GRE_BUILD_SEED) the tracked seed must be read-only,
    else runtime writes dirty the binary and block `git pull`."""
    import models.migrations as m
    assert m.SEED_WRITES_ENABLED is False


def test_migration_does_not_write_seed_at_runtime(temp_db, tmp_path, monkeypatch):
    """A data-repair migration must NOT modify the seed file when seed writes
    are disabled (the default). Point SEED_DB_PATH at a real temp seed with a
    question row and assert the file is byte-identical after the migration."""
    import models.migrations as m
    # build a minimal seed file with q5400 so 044 would retire it if it wrote
    seed = tmp_path / "seed.db"
    con = sqlite3.connect(str(seed))
    con.execute("CREATE TABLE question (id INTEGER PRIMARY KEY, status TEXT, "
                "provenance_json TEXT, prompt TEXT, explanation TEXT)")
    con.execute("INSERT INTO question (id,status) VALUES (5400,'live')")
    con.commit(); con.close()
    monkeypatch.setattr("config.SEED_DB_PATH", seed)
    assert m.SEED_WRITES_ENABLED is False  # guard off in test env
    before = seed.read_bytes()
    m._044_user_reported_fixes_2026_06_09()
    assert seed.read_bytes() == before, "seed was modified at runtime"


# ── migration 044 content ──────────────────────────────────────────────────

def test_044_registered(temp_db):
    import models.migrations as m
    applied = {r.name for r in m.SchemaMigration.select(m.SchemaMigration.name)}
    assert "044_user_reported_fixes_2026_06_09" in applied


@pytest.fixture
def issue_questions(temp_db):
    from models.database import Question, QuestionOption
    # q5379: grafted dollar options
    q = Question.create(id=5379, measure="quant", subtype="mcq_multi",
                        prompt="number of notebooks", explanation="n in {2,4,6}",
                        source="ai_synthetic_v2", status="live")
    for lbl, txt, ic in (("A", "$12", True), ("B", "$18", False), ("C", "$24", False),
                         ("D", "$30", False), ("E", "$6", False), ("F", "$4", True)):
        QuestionOption.create(question=q, option_label=lbl, option_text=txt, is_correct=ic)
    # q3196: ambiguous stem, key on E
    q2 = Question.create(id=3196, measure="quant", subtype="mcq_single",
                         prompt="If x is a number ... 20/x integer?",
                         explanation="more than 10", source="manhattan_5lb_2018",
                         status="live")
    for lbl, txt, ic in (("A", "4", False), ("B", "6", False), ("C", "8", False),
                         ("D", "10", False), ("E", "More than 10", True)):
        QuestionOption.create(question=q2, option_label=lbl, option_text=txt, is_correct=ic)
    # q5400: broken DI item
    Question.create(id=5400, measure="quant", subtype="mcq_single",
                    prompt="ratio", explanation="~2.51", source="ai_synthetic_v2",
                    status="live")
    return q, q2


def test_044_repairs_q5379_notebook_counts(issue_questions):
    from models.database import QuestionOption
    import models.migrations as m
    m._044_user_reported_fixes_2026_06_09()
    opts = list(QuestionOption.select().where(QuestionOption.question == 5379)
                .order_by(QuestionOption.option_label))
    assert {o.option_text for o in opts if o.is_correct} == {"2", "4", "6"}
    # options are integer counts, no dollar signs
    assert all("$" not in o.option_text for o in opts)


def test_044_repairs_q3196_constrains_integer_and_flips_key(issue_questions):
    from models.database import Question, QuestionOption
    import models.migrations as m
    m._044_user_reported_fixes_2026_06_09()
    q = Question.get_by_id(3196)
    assert "positive integer" in q.prompt
    correct = [o.option_label for o in QuestionOption.select()
               .where((QuestionOption.question == 3196) & (QuestionOption.is_correct == True))]  # noqa: E712
    assert correct == ["B"]  # 6 divisors


def test_044_retires_q5400(issue_questions):
    from models.database import Question
    import models.migrations as m
    m._044_user_reported_fixes_2026_06_09()
    assert Question.get_by_id(5400).status == "retired"


def test_044_idempotent(issue_questions):
    from models.database import QuestionOption
    import models.migrations as m
    m._044_user_reported_fixes_2026_06_09()
    m._044_user_reported_fixes_2026_06_09()
    assert QuestionOption.select().where(QuestionOption.question == 5379).count() == 7
