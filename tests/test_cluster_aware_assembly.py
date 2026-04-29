"""
Cluster-aware assembly tests.

`select_questions_composed` must treat each ``(stimulus_id, subtype)`` group
as an atomic unit. A section either includes every sibling in a cluster or
none — it never ships a partial passage / chart.
"""
import random

import pytest

# temp_db fixture from tests/conftest.py is applied automatically.


def _make_verbal_fixture(db):
    """Build a small but representative verbal pool.

    * 1 RC cluster of 3 questions (stimulus A, rc_multi)
    * 1 RC cluster of 2 questions (stimulus B, rc_multi)
    * 1 RC cluster of 1 question  (stimulus C, rc_single)
    * 10 TC singletons
    * 10 SE singletons
    """
    from models.database import Question, Stimulus, QuestionOption

    stim_a = Stimulus.create(stimulus_type="passage",
                             title="A", content="passage A")
    stim_b = Stimulus.create(stimulus_type="passage",
                             title="B", content="passage B")
    stim_c = Stimulus.create(stimulus_type="passage",
                             title="C", content="passage C")

    created = {"rc_multi_a": [], "rc_multi_b": [], "rc_single_c": [],
               "tc": [], "se": []}

    for i in range(3):
        q = Question.create(measure="verbal", subtype="rc_multi",
                            stimulus=stim_a, prompt=f"A-Q{i}",
                            time_target_seconds=90,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        created["rc_multi_a"].append(q.id)
        QuestionOption.create(question=q, option_label="A",
                              option_text="x", is_correct=True)

    for i in range(2):
        q = Question.create(measure="verbal", subtype="rc_multi",
                            stimulus=stim_b, prompt=f"B-Q{i}",
                            time_target_seconds=90,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        created["rc_multi_b"].append(q.id)

    q = Question.create(measure="verbal", subtype="rc_single",
                        stimulus=stim_c, prompt="C-Q0",
                        time_target_seconds=90,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    created["rc_single_c"].append(q.id)

    for i in range(10):
        q = Question.create(measure="verbal", subtype="tc",
                            prompt=f"TC-{i}",
                            time_target_seconds=60,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        created["tc"].append(q.id)

    for i in range(10):
        q = Question.create(measure="verbal", subtype="se",
                            prompt=f"SE-{i}",
                            time_target_seconds=60,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        created["se"].append(q.id)

    return created


def _make_quant_fixture(db):
    """Build a small quant pool with one DI cluster + singletons."""
    from models.database import Question, Stimulus

    stim = Stimulus.create(stimulus_type="graph",
                           title="Chart", content="{}")
    di_ids = []
    for i in range(3):
        q = Question.create(measure="quant", subtype="data_interp",
                            stimulus=stim, prompt=f"DI-{i}",
                            time_target_seconds=90,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        di_ids.append(q.id)

    qc_ids, mcq_ids = [], []
    for i in range(6):
        q = Question.create(measure="quant", subtype="qc",
                            prompt=f"QC-{i}", time_target_seconds=90,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        qc_ids.append(q.id)
    for i in range(10):
        q = Question.create(measure="quant", subtype="mcq_single",
                            prompt=f"MCQ-{i}", time_target_seconds=90,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        mcq_ids.append(q.id)
    return {"di": di_ids, "qc": qc_ids, "mcq_single": mcq_ids}


def test_rc_cluster_is_atomic(temp_db):
    """If any RC cluster member is chosen, every sibling comes with it."""
    from services.question_bank import QuestionBankService

    created = _make_verbal_fixture(temp_db)
    qb = QuestionBankService()

    # Run many seeds to make sure atomicity isn't a lucky ordering.
    for seed in range(20):
        random.seed(seed)
        ids = qb.select_questions_composed(
            measure="verbal", count=12, difficulty_band="medium",
        )
        id_set = set(ids)
        # Cluster A: all 3 or none
        a = set(created["rc_multi_a"])
        assert a.issubset(id_set) or not (a & id_set), \
            f"partial A cluster at seed {seed}: {a & id_set}"
        # Cluster B: all 2 or none
        b = set(created["rc_multi_b"])
        assert b.issubset(id_set) or not (b & id_set), \
            f"partial B cluster at seed {seed}: {b & id_set}"


def test_di_cluster_is_atomic(temp_db):
    """3-Q DI cluster either ships whole or not at all."""
    from services.question_bank import QuestionBankService

    created = _make_quant_fixture(temp_db)
    qb = QuestionBankService()

    for seed in range(20):
        random.seed(seed)
        ids = qb.select_questions_composed(
            measure="quant", count=12, difficulty_band="medium",
        )
        id_set = set(ids)
        di = set(created["di"])
        assert di.issubset(id_set) or not (di & id_set), \
            f"partial DI cluster at seed {seed}: {di & id_set}"


def test_section_size_respected(temp_db):
    """Selected count should not exceed requested count even with clusters."""
    from services.question_bank import QuestionBankService

    _make_verbal_fixture(temp_db)
    qb = QuestionBankService()
    for seed in range(10):
        random.seed(seed)
        ids = qb.select_questions_composed(
            measure="verbal", count=12, difficulty_band="medium",
        )
        assert len(ids) <= 12


def test_oversized_cluster_skipped_for_smaller_budget(temp_db):
    """If only a 3-Q cluster remains and budget=2, the cluster is skipped.

    This exercises the "never ship a partial cluster" rule: the assembler
    should leave the slots unfilled rather than truncate the cluster.
    """
    from models.database import Question, Stimulus
    from services.question_bank import QuestionBankService

    stim = Stimulus.create(stimulus_type="passage", title="X", content="x")
    big_ids = []
    for i in range(3):
        q = Question.create(measure="verbal", subtype="rc_multi",
                            stimulus=stim, prompt=f"X-{i}",
                            time_target_seconds=90,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        big_ids.append(q.id)

    qb = QuestionBankService()
    # Budget 2 -> the only available cluster (size 3) must be skipped.
    taken = qb._take_cluster_aware(
        measure="verbal", subtype="rc_multi", target=2,
        difficulty_band="medium", exclude=set(),
    )
    assert taken == []


def test_section_fills_from_singletons_when_clusters_exhausted(temp_db):
    """With a 3-Q RC cluster + 10 TC + 10 SE, a 12-count section should
    pick enough singletons to fill."""
    from services.question_bank import QuestionBankService

    _make_verbal_fixture(temp_db)
    qb = QuestionBankService()
    random.seed(42)
    ids = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
    )
    assert len(ids) == 12
    # No duplicates
    assert len(set(ids)) == 12
