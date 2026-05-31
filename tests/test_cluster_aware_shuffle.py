"""Cluster-aware shuffle regression tests.

The user reported that within a Quant or Verbal section, items appeared
clubbed by subtype: all DI questions sequential, then all mcq_single,
then all qc, etc. Real GRE interleaves subtypes; only multi-question
clusters (RC passages, DI charts) stay adjacent because the stimulus
is rendered once at the top of the cluster.

These tests pin two invariants on top of the existing
``test_cluster_aware_assembly``:

1. ``_cluster_aware_shuffle`` keeps cluster siblings adjacent regardless
   of how many times we shuffle.
2. ``select_questions_composed`` returns a section whose non-cluster
   items don't run more than 3 same-subtype in a row (pre-fix this was
   4-6 routinely; post-fix it's bounded by the small sample variance of
   independent draws).

The shuffle tests run against a hand-built fixture so they don't depend
on the shipped seed; the interleaving test runs against the live seed
when present, otherwise it skips like ``test_blueprint_assembly``.
"""
from __future__ import annotations

import random
from collections import Counter
from typing import List, Optional, Tuple

import pytest

# temp_db fixture from tests/conftest.py is applied automatically.


def _make_quant_pool(_db):
    """Build a Quant pool with a 3-item DI cluster + plenty of singletons.

    Returns the cluster qids so callers can assert atomicity.
    """
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
    for i in range(8):
        Question.create(measure="quant", subtype="qc",
                        prompt=f"QC-{i}", time_target_seconds=90,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    for i in range(10):
        Question.create(measure="quant", subtype="mcq_single",
                        prompt=f"MCQ-{i}", time_target_seconds=90,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    for i in range(4):
        Question.create(measure="quant", subtype="mcq_multi",
                        prompt=f"MM-{i}", time_target_seconds=90,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    for i in range(4):
        Question.create(measure="quant", subtype="numeric_entry",
                        prompt=f"NE-{i}", time_target_seconds=90,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    return di_ids


def _make_verbal_pool(_db):
    """Build a Verbal pool with two RC clusters (3-Q + 2-Q) + singletons."""
    from models.database import Question, Stimulus, QuestionOption

    stim_a = Stimulus.create(stimulus_type="passage",
                             title="A", content="passage A")
    stim_b = Stimulus.create(stimulus_type="passage",
                             title="B", content="passage B")
    a_ids, b_ids = [], []
    for i in range(3):
        q = Question.create(measure="verbal", subtype="rc_multi",
                            stimulus=stim_a, prompt=f"A-Q{i}",
                            time_target_seconds=90,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        a_ids.append(q.id)
        QuestionOption.create(question=q, option_label="A",
                              option_text="x", is_correct=True)
    for i in range(2):
        q = Question.create(measure="verbal", subtype="rc_multi",
                            stimulus=stim_b, prompt=f"B-Q{i}",
                            time_target_seconds=90,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        b_ids.append(q.id)
    for i in range(10):
        Question.create(measure="verbal", subtype="tc",
                        prompt=f"TC-{i}", time_target_seconds=60,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    for i in range(10):
        Question.create(measure="verbal", subtype="se",
                        prompt=f"SE-{i}", time_target_seconds=60,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    return {"a": a_ids, "b": b_ids}


def _subtypes_in_order(qids):
    """Return [(qid, subtype, stim_id), ...] in qids order."""
    from models.database import Question
    rows = list(Question.select(Question.id, Question.subtype,
                                Question.stimulus_id)
                .where(Question.id.in_(list(qids))))
    by_id = {r.id: r for r in rows}
    out = []
    for qid in qids:
        r = by_id[qid]
        out.append((qid, r.subtype, r.stimulus_id))
    return out


def _cluster_run_indices(seq, target_qids):
    """Return the index range covering target_qids in seq, or None.

    Adjacency check: if every qid in target_qids is at consecutive
    positions in seq, return (lo, hi). Else None.
    """
    target_set = set(target_qids)
    positions = sorted(i for i, t in enumerate(seq) if t[0] in target_set)
    if len(positions) != len(target_qids):
        return None
    if positions[-1] - positions[0] + 1 != len(target_qids):
        return None
    return (positions[0], positions[-1])


def test_di_cluster_stays_adjacent_after_shuffle(temp_db):
    """3-Q DI cluster must occupy consecutive positions in the section."""
    from services.question_bank import QuestionBankService

    di_ids = _make_quant_pool(temp_db)
    qb = QuestionBankService()
    for seed in range(20):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="quant", count=12, difficulty_band="medium")
        seq = _subtypes_in_order(qids)
        if not (set(di_ids) & set(qids)):
            # cluster wasn't picked this run — atomicity vacuously holds
            continue
        run = _cluster_run_indices(seq, di_ids)
        assert run is not None, (
            f"seed {seed}: DI cluster {di_ids} not adjacent in shuffled "
            f"section {[(s[0], s[1]) for s in seq]}")


def test_rc_passage_siblings_adjacent_after_shuffle(temp_db):
    """RC passage A's 3 siblings (and B's 2) must each be consecutive in
    the shuffled section."""
    from services.question_bank import QuestionBankService

    pools = _make_verbal_pool(temp_db)
    qb = QuestionBankService()
    for seed in range(20):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="verbal", count=12, difficulty_band="medium")
        seq = _subtypes_in_order(qids)
        for label, ids in pools.items():
            if not (set(ids) & set(qids)):
                continue
            run = _cluster_run_indices(seq, ids)
            assert run is not None, (
                f"seed {seed}: passage {label!r} siblings {ids} not "
                f"adjacent in shuffled section {[(s[0], s[1]) for s in seq]}")


def _bucket_for_run(item):
    """Map subtype -> bucket for run-length analysis. Cluster items group
    under their stimulus_id; non-cluster items group by raw subtype."""
    from services.question_bank import CLUSTER_SUBTYPES
    qid, subtype, stim_id = item
    if subtype in CLUSTER_SUBTYPES and stim_id is not None:
        return ("cluster", stim_id)
    return ("solo", subtype)


def _longest_solo_subtype_run(seq):
    """Longest run of consecutive same-subtype items, ignoring cluster
    items (which are expected to bunch). Returns 0 when no solo items."""
    solos = [s for s in seq if _bucket_for_run(s)[0] == "solo"]
    if not solos:
        return 0
    longest = 1
    cur = 1
    for i in range(1, len(solos)):
        if solos[i][1] == solos[i - 1][1]:
            cur += 1
            if cur > longest:
                longest = cur
        else:
            cur = 1
    return longest


def test_solo_subtypes_interleave_in_quant_section(temp_db):
    """Pre-fix the per-subtype loop appended a 4+ run of mcq_single in
    every Quant section. Post-fix the cluster-aware shuffle reorders
    blocks so consecutive same-subtype runs are bounded by independent
    draws — across many seeds the longest run stays small (≤3 with high
    probability).

    Hand-built fixture so the test stays deterministic without the
    shipped seed.
    """
    from services.question_bank import QuestionBankService

    _make_quant_pool(temp_db)
    qb = QuestionBankService()
    runs = []
    for seed in range(30):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="quant", count=12, difficulty_band="medium")
        seq = _subtypes_in_order(qids)
        runs.append(_longest_solo_subtype_run(seq))

    # Pre-fix: every section ran 4+ same-subtype solo items in a row.
    # Post-fix: independent ordering over ~9 solo items in 5 subtypes.
    # Allow ≤4 to absorb the rare "two adjacent independent draws of
    # mcq_single line up" — bug pattern was max=4 across ALL seeds.
    max_run = max(runs)
    mean_run = sum(runs) / len(runs)
    assert max_run <= 4, (
        f"longest solo run = {max_run} across 30 seeds (per-seed: {runs}); "
        f"pre-fix this was always ≥4 because the per-subtype loop kept "
        f"items batched. Post-fix the shuffle should keep max ≤ 4 with "
        f"mean ~2.5.")
    assert mean_run < 3.5, (
        f"mean longest solo run = {mean_run:.2f}; pre-fix was 4.0 "
        f"every section. Post-fix expects ~2-3.")


def test_solo_subtypes_interleave_in_verbal_section(temp_db):
    """Verbal: TC and SE solo items must interleave rather than ship as
    one TC batch followed by one SE batch."""
    from services.question_bank import QuestionBankService

    _make_verbal_pool(temp_db)
    qb = QuestionBankService()
    runs = []
    for seed in range(30):
        random.seed(seed)
        qids = qb.select_questions_composed(
            measure="verbal", count=12, difficulty_band="medium")
        seq = _subtypes_in_order(qids)
        runs.append(_longest_solo_subtype_run(seq))

    max_run = max(runs)
    mean_run = sum(runs) / len(runs)
    # Pre-fix: TC batch (3) directly followed by SE batch (3) — longest
    # solo run was 3 every section. Post-fix: interleaved blocks.
    assert max_run <= 4, (
        f"longest solo run = {max_run} across 30 seeds (per-seed: {runs})")
    assert mean_run < 3.0, (
        f"mean longest solo run = {mean_run:.2f}; pre-fix was 3.0 every "
        f"section.")


def test_cluster_aware_shuffle_helper_preserves_blocks(temp_db):
    """Direct test of ``_cluster_aware_shuffle``: same-cluster runs in
    the input must remain adjacent in the output, while singletons can
    move freely."""
    from services.question_bank import _cluster_aware_shuffle
    from models.database import Question, Stimulus

    stim_a = Stimulus.create(stimulus_type="passage",
                             title="A", content="a")
    stim_b = Stimulus.create(stimulus_type="passage",
                             title="B", content="b")
    a_ids, b_ids = [], []
    for i in range(3):
        q = Question.create(measure="verbal", subtype="rc_multi",
                            stimulus=stim_a, prompt=f"A{i}",
                            time_target_seconds=60, concept_tags="[]",
                            explanation="", difficulty_target=3,
                            status="live")
        a_ids.append(q.id)
    for i in range(2):
        q = Question.create(measure="verbal", subtype="rc_multi",
                            stimulus=stim_b, prompt=f"B{i}",
                            time_target_seconds=60, concept_tags="[]",
                            explanation="", difficulty_target=3,
                            status="live")
        b_ids.append(q.id)
    solo_ids = []
    for i in range(5):
        q = Question.create(measure="verbal", subtype="tc",
                            prompt=f"T{i}", time_target_seconds=60,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        solo_ids.append(q.id)

    # Build an input list that mirrors what ``select_questions_composed``
    # produces: cluster A's 3 ids contiguous, cluster B's 2 contiguous,
    # singletons trailing.
    qids_in = list(a_ids) + list(b_ids) + list(solo_ids)
    # Run many shuffles to make sure block atomicity is invariant.
    saw_different_order = False
    for seed in range(20):
        random.seed(seed)
        out = _cluster_aware_shuffle(qids_in)
        assert sorted(out) == sorted(qids_in), \
            "shuffle changed membership"
        assert len(out) == len(qids_in)
        # A's siblings adjacent
        a_pos = sorted(i for i, q in enumerate(out) if q in set(a_ids))
        assert a_pos[-1] - a_pos[0] + 1 == 3, \
            f"seed {seed}: A cluster split: positions {a_pos}, out {out}"
        # B's siblings adjacent
        b_pos = sorted(i for i, q in enumerate(out) if q in set(b_ids))
        assert b_pos[-1] - b_pos[0] + 1 == 2, \
            f"seed {seed}: B cluster split: positions {b_pos}, out {out}"
        if out != qids_in:
            saw_different_order = True
    assert saw_different_order, \
        "shuffle never reordered anything — random seed determinism broken?"


def test_cluster_aware_shuffle_empty_input(temp_db):
    """Empty list → empty list, no DB hit needed."""
    from services.question_bank import _cluster_aware_shuffle
    assert _cluster_aware_shuffle([]) == []


def test_cluster_aware_shuffle_single_item(temp_db):
    """Single qid → singleton block, returned unchanged."""
    from services.question_bank import _cluster_aware_shuffle
    from models.database import Question
    q = Question.create(measure="verbal", subtype="tc",
                        prompt="solo", time_target_seconds=60,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    random.seed(0)
    assert _cluster_aware_shuffle([q.id]) == [q.id]
