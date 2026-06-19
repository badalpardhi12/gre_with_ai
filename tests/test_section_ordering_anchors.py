"""Section ordering-anchor tests (balancing fix #2).

Real GRE sections open with a recognizable item type — Verbal sections
open with a Text Completion, Quant sections open with a Quantitative
Comparison — and the multi-question Data-Interpretation set sits as a
contiguous block in the middle-to-late part of the Quant section, never
first. The legacy ``_cluster_aware_shuffle`` was a pure random block
shuffle, so a section could open with an RC passage or land the DI set
at position 0, which reads as "not the GRE" to anyone who has done
PowerPrep.

These tests pin the new anchored ordering while preserving the existing
invariants (cluster atomicity + solo-subtype interleaving — see
``test_cluster_aware_shuffle.py``). The anchor convention is APPROXIMATE
(prep-source, not an ETS-published rule).
"""
from __future__ import annotations

import random
from collections import Counter

import pytest

# temp_db fixture from tests/conftest.py is applied automatically.


def _make_quant_pool(_db):
    from models.database import Question, Stimulus
    stim = Stimulus.create(stimulus_type="graph", title="Chart", content="{}")
    di_ids = []
    for i in range(3):
        q = Question.create(measure="quant", subtype="data_interp",
                            stimulus=stim, prompt=f"DI-{i}",
                            time_target_seconds=90, concept_tags="[]",
                            explanation="", difficulty_target=3, status="live")
        di_ids.append(q.id)
    for i in range(8):
        Question.create(measure="quant", subtype="qc", prompt=f"QC-{i}",
                        time_target_seconds=90, concept_tags="[]",
                        explanation="", difficulty_target=3, status="live")
    for i in range(12):
        Question.create(measure="quant", subtype="mcq_single", prompt=f"MCQ-{i}",
                        time_target_seconds=90, concept_tags="[]",
                        explanation="", difficulty_target=3, status="live")
    for i in range(4):
        Question.create(measure="quant", subtype="mcq_multi", prompt=f"MM-{i}",
                        time_target_seconds=90, concept_tags="[]",
                        explanation="", difficulty_target=3, status="live")
    for i in range(4):
        Question.create(measure="quant", subtype="numeric_entry", prompt=f"NE-{i}",
                        time_target_seconds=90, concept_tags="[]",
                        explanation="", difficulty_target=3, status="live")
    return di_ids


def _make_verbal_pool(_db):
    from models.database import Question, Stimulus, QuestionOption
    stim_a = Stimulus.create(stimulus_type="passage", title="A", content="passage A")
    a_ids = []
    for i in range(3):
        q = Question.create(measure="verbal", subtype="rc_multi",
                            stimulus=stim_a, prompt=f"A-Q{i}",
                            time_target_seconds=90, concept_tags="[]",
                            explanation="", difficulty_target=3, status="live")
        a_ids.append(q.id)
        QuestionOption.create(question=q, option_label="A",
                              option_text="x", is_correct=True)
    for i in range(10):
        Question.create(measure="verbal", subtype="tc", prompt=f"TC-{i}",
                        time_target_seconds=60, concept_tags="[]",
                        explanation="", difficulty_target=3, status="live")
    for i in range(10):
        Question.create(measure="verbal", subtype="se", prompt=f"SE-{i}",
                        time_target_seconds=60, concept_tags="[]",
                        explanation="", difficulty_target=3, status="live")
    return a_ids


def _seq(qids):
    from models.database import Question
    rows = list(Question.select(Question.id, Question.subtype,
                                Question.stimulus_id)
                .where(Question.id.in_(list(qids))))
    by_id = {r.id: r for r in rows}
    return [(qid, by_id[qid].subtype, by_id[qid].stimulus_id) for qid in qids]


# ── opening-subtype anchors via select_questions_composed ─────────────

def test_quant_section_opens_with_qc(temp_db):
    from services.question_bank import QuestionBankService
    _make_quant_pool(temp_db)
    qb = QuestionBankService()
    for seed in range(25):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="quant", count=12, difficulty_band="medium")
        seq = _seq(qids)
        assert seq[0][1] == "qc", (
            f"seed {seed}: Quant section should open with QC, got "
            f"{seq[0][1]}; order={[s[1] for s in seq]}")


def test_verbal_section_opens_with_tc(temp_db):
    from services.question_bank import QuestionBankService
    _make_verbal_pool(temp_db)
    qb = QuestionBankService()
    for seed in range(25):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="verbal", count=12, difficulty_band="medium")
        seq = _seq(qids)
        assert seq[0][1] == "tc", (
            f"seed {seed}: Verbal section should open with TC, got "
            f"{seq[0][1]}; order={[s[1] for s in seq]}")


def test_di_cluster_never_opens_quant_section(temp_db):
    """The DI set must never be the first block, and on average sits past
    the first third of the section (mid-to-late bias)."""
    from services.question_bank import QuestionBankService
    di_ids = _make_quant_pool(temp_db)
    qb = QuestionBankService()
    di_set = set(di_ids)
    start_positions = []
    for seed in range(40):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="quant", count=12, difficulty_band="medium")
        if not (di_set & set(qids)):
            continue
        first_di = next(i for i, q in enumerate(qids) if q in di_set)
        assert first_di != 0, (
            f"seed {seed}: DI set must not open the section "
            f"(position {first_di})")
        start_positions.append(first_di)
    assert start_positions, "DI set never appeared — fixture/selection issue"
    mean_start = sum(start_positions) / len(start_positions)
    # 12-item section; mid-to-late bias should keep the mean DI start
    # comfortably past the first couple positions.
    assert mean_start >= 3.0, (
        f"DI set is not biased mid-to-late: mean start index "
        f"{mean_start:.2f} (positions={start_positions})")


# ── direct helper: measure-aware vs legacy ────────────────────────────

def test_shuffle_pins_opener_when_measure_given(temp_db):
    from services.question_bank import _cluster_aware_shuffle
    from models.database import Question
    qc = [Question.create(measure="quant", subtype="qc", prompt=f"q{i}",
                          time_target_seconds=90, concept_tags="[]",
                          explanation="", difficulty_target=3, status="live").id
          for i in range(2)]
    others = [Question.create(measure="quant", subtype="mcq_single", prompt=f"m{i}",
                             time_target_seconds=90, concept_tags="[]",
                             explanation="", difficulty_target=3, status="live").id
              for i in range(6)]
    qids_in = others[:3] + qc + others[3:]  # qc NOT first in input
    from models.database import Question as Q
    for seed in range(15):
        random.seed(seed)
        out = _cluster_aware_shuffle(qids_in, measure="quant")
        assert sorted(out) == sorted(qids_in)
        first_subtype = Q.get(Q.id == out[0]).subtype
        assert first_subtype == "qc", (
            f"seed {seed}: measure-aware shuffle must pin QC first, got "
            f"{first_subtype}")


def test_shuffle_legacy_no_measure_pure_shuffle(temp_db):
    """Without ``measure`` the helper keeps its legacy pure-block-shuffle
    behavior (backwards compatible with existing direct-call tests)."""
    from services.question_bank import _cluster_aware_shuffle
    from models.database import Question
    ids = [Question.create(measure="quant", subtype="mcq_single", prompt=f"m{i}",
                          time_target_seconds=90, concept_tags="[]",
                          explanation="", difficulty_target=3, status="live").id
           for i in range(8)]
    saw_reorder = False
    for seed in range(20):
        random.seed(seed)
        out = _cluster_aware_shuffle(ids)
        assert sorted(out) == sorted(ids)
        if out != ids:
            saw_reorder = True
    assert saw_reorder
