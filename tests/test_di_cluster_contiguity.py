"""Regression test: Data-Interpretation questions sharing one chart/table must
stay CONSECUTIVE in an assembled section (the stimulus renders once).

The bug (reported 2026-06-01): ai_synthetic_v2 DI sets carry their real
subtypes (mcq_single/numeric_entry) sharing one graph/table stimulus, but
_cluster_group only clustered CLUSTER_SUBTYPES, so the shuffle treated the DI
questions as singletons and scattered them across the section.
"""
import random
import sqlite3

import pytest


def _di_member_map():
    """qid -> stimulus_id for live quant questions on a multi-question
    graph/table/chart stimulus (real DI sets)."""
    con = sqlite3.connect("data/gre_user.db")
    rows = con.execute(
        "SELECT q.id, q.stimulus_id FROM question q JOIN stimulus s "
        "ON q.stimulus_id=s.id WHERE q.status='live' AND q.measure='quant' "
        "AND s.stimulus_type IN ('graph','table','chart')").fetchall()
    con.close()
    from collections import Counter
    by_stim = Counter(sid for _qid, sid in rows)
    multi = {sid for sid, n in by_stim.items() if n >= 2}
    return {qid: sid for qid, sid in rows if sid in multi}


def test_cluster_group_keys_by_stimulus_regardless_of_subtype():
    from services.question_bank import _cluster_group

    class Q:
        def __init__(self, id, subtype, stimulus_id):
            self.id, self.subtype, self.stimulus_id = id, subtype, stimulus_id
    # two DI questions sharing a chart but with DIFFERENT subtypes -> same key
    a = Q(1, "mcq_single", 999)
    b = Q(2, "numeric_entry", 999)
    assert _cluster_group(a) == _cluster_group(b) == ("stim", 999)
    # no stimulus -> singleton keyed by id
    assert _cluster_group(Q(3, "mcq_single", None)) == ("q", 3)


def test_di_questions_are_consecutive_in_assembled_quant_sections():
    if not __import__("os").path.exists("data/gre_user.db"):
        pytest.skip("user db absent")
    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    di = _di_member_map()
    if not di:
        pytest.skip("no multi-question DI sets in pool")
    saw_di = 0
    for seed in range(40):
        random.seed(seed * 13 + 7)
        ids = qb.select_questions_composed(
            measure="quant", count=15, difficulty_band="medium")
        positions = {}
        for i, qid in enumerate(ids):
            sid = di.get(qid)
            if sid is not None:
                positions.setdefault(sid, []).append(i)
        for sid, idxs in positions.items():
            if len(idxs) > 1:
                saw_di += 1
                assert idxs == list(range(idxs[0], idxs[0] + len(idxs))), (
                    f"DI chart {sid} split across positions {idxs} in {ids}")
    # ensure the assertion actually exercised real DI clusters
    assert saw_di > 0, "no multi-question DI cluster ever appeared to verify"


def test_di_does_not_overstack_section():
    """Only one DI chart per quant section (the controlled cluster)."""
    if not __import__("os").path.exists("data/gre_user.db"):
        pytest.skip("user db absent")
    from services.question_bank import QuestionBankService
    qb = QuestionBankService()
    di = _di_member_map()
    if not di:
        pytest.skip("no multi-question DI sets in pool")
    for seed in range(40):
        random.seed(seed * 13 + 7)
        ids = qb.select_questions_composed(
            measure="quant", count=15, difficulty_band="medium")
        charts = {di[q] for q in ids if q in di}
        assert len(charts) <= 1, f"more than one DI chart in section: {charts}"
