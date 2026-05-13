"""
R3 — ServedLog write-side + read-side integration tests.

Covers:
  * Write-through at pick time: ``select_questions_composed`` writes one
    ServedLog row per picked qid when ``exclude_user_seen`` is set, even
    when the user has zero Response rows.
  * Read-side union: a second call within the dedup window excludes the
    first call's picks from the candidate pool without any Response row
    existing.
  * Failure-tolerance: write errors degrade to WARN and never block
    selection.

A separate integration check runs the 20-mock simulation against the
real seed DB and verifies the Phase-1 R3 acceptance metrics — the same
thresholds the benchmark gate enforces. Kept in this file (instead of
``tests/benchmarks/``) so it runs on every `pytest tests/` without
needing the benchmark marker.
"""
from __future__ import annotations

import random
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


# ── Unit tests on temp_db ─────────────────────────────────────────────


def _seed_small_bank(n_tc=20, n_se=20):
    from models.database import Question
    ids = []
    for i in range(n_tc):
        q = Question.create(measure="verbal", subtype="tc",
                            prompt=f"TC-{i}", time_target_seconds=60,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        ids.append(q.id)
    for i in range(n_se):
        q = Question.create(measure="verbal", subtype="se",
                            prompt=f"SE-{i}", time_target_seconds=60,
                            concept_tags="[]", explanation="",
                            difficulty_target=3, status="live")
        ids.append(q.id)
    return ids


def test_servedlog_written_at_pick_time(temp_db):
    """Every picked qid lands in ServedLog even with zero Response rows."""
    from models.database import ServedLog, Response
    from services.question_bank import QuestionBankService

    _seed_small_bank()
    qb = QuestionBankService()

    assert Response.select().count() == 0
    assert ServedLog.select().count() == 0

    picks = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    assert len(picks) == 12

    rows = list(ServedLog.select())
    assert len(rows) == len(picks), (
        f"expected one ServedLog row per pick ({len(picks)}), "
        f"got {len(rows)}")
    assert {r.question_id for r in rows} == set(picks)
    # user_id + served_at populated.
    for r in rows:
        assert r.user_id == "local"
        assert r.served_at is not None


def test_servedlog_excludes_prior_picks_without_responses(temp_db):
    """Second call excludes the first call's picks purely via ServedLog.

    No Response rows exist at any point, so this exercises the R3
    read-side union specifically.
    """
    from models.database import Response, ServedLog
    from services.question_bank import QuestionBankService

    _seed_small_bank(n_tc=30, n_se=30)
    qb = QuestionBankService()

    first = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    assert len(first) == 12
    assert Response.select().count() == 0
    assert ServedLog.select().count() == 12

    second = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    assert len(second) == 12
    # Because the pool has 60 verbal items and recent_seen_days is 30
    # by default, the first 12 must be filtered out of the second call.
    overlap = set(first) & set(second)
    assert not overlap, (
        f"ServedLog dedup failed: second call re-served {sorted(overlap)} "
        f"even with an empty Response table")


def test_servedlog_skipped_when_exclude_user_seen_none(temp_db):
    """When the caller doesn't pass ``exclude_user_seen`` the assembler
    is in stateless mode (topic drill, etc.) and must not pollute
    ServedLog — dedup isn't requested.
    """
    from models.database import ServedLog
    from services.question_bank import QuestionBankService

    _seed_small_bank()
    qb = QuestionBankService()
    picks = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
    )
    assert len(picks) > 0
    assert ServedLog.select().count() == 0


def test_servedlog_write_failure_is_swallowed(temp_db, monkeypatch):
    """A DB-layer write failure (locked DB, missing column, etc.) must
    not propagate — selection returns normally with a WARN logged."""
    from models.database import ServedLog
    from services import question_bank as qb_mod

    _seed_small_bank()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(ServedLog, "insert_many", _boom)
    qb = qb_mod.QuestionBankService()
    picks = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    assert len(picks) == 12  # selection succeeded despite insert failure


def test_get_recently_seen_ids_unions_servedlog(temp_db):
    """``get_recently_seen_ids`` must surface ServedLog qids even when
    the Response table is empty."""
    from datetime import datetime
    from models.database import ServedLog, Response
    from services.question_bank import get_recently_seen_ids

    # Seed 3 ServedLog rows directly (no Response rows).
    assert Response.select().count() == 0
    ServedLog.create(question_id=9001, user_id="local",
                     served_at=datetime.now())
    ServedLog.create(question_id=9002, user_id="local",
                     served_at=datetime.now())
    ServedLog.create(question_id=9003, user_id="other",
                     served_at=datetime.now())

    seen_local = get_recently_seen_ids(days_back=14, user_id="local")
    assert 9001 in seen_local and 9002 in seen_local
    assert 9003 not in seen_local  # different user


# ── 20-mock integration check against the real seed DB ───────────────


PROJECT_ROOT = Path(__file__).resolve().parents[1]
N_MOCKS = 20
V1 = 12
V2 = 15
Q1 = 12
Q2 = 15
BENCH_SEED = 20260512


def _seed_db_available():
    return (PROJECT_ROOT / "data" / "gre_mock.db").exists()


@pytest.mark.skipif(not _seed_db_available(),
                    reason="shipped seed DB missing")
def test_r3_20mock_repetition_floor():
    """20-mock simulation on real seed: DI first-repeat >= 12, RC
    passage first-repeat >= 8, figure_singleton hot max <= 3.

    This is the same gate the ``tests/benchmarks/test_repetition_floor.py``
    test hits, duplicated here so R3 has a self-contained acceptance
    check that doesn't depend on the benchmark harness.
    """
    import config
    orig_db_path = config.DB_PATH
    orig_seed_path = config.SEED_DB_PATH
    tmpdir = Path(tempfile.mkdtemp(prefix="gre_r3_"))
    seed_src = PROJECT_ROOT / "data" / "gre_mock.db"
    db_copy = tmpdir / "r3.db"
    shutil.copy2(str(seed_src), str(db_copy))

    try:
        config.DB_PATH = db_copy
        config.SEED_DB_PATH = tmpdir / "no_seed.db"
        for prefix in ("models", "services"):
            for mod in [m for m in list(sys.modules)
                        if m.startswith(prefix + ".") or m == prefix]:
                del sys.modules[mod]

        from models.database import (
            db, init_db, Question, Stimulus, Response, ServedLog,
        )
        init_db()
        Response.delete().execute()
        ServedLog.delete().execute()

        from services.question_bank import QuestionBankService
        random.seed(BENCH_SEED)
        qb = QuestionBankService()
        user_id = "r3_bench_user"

        di_qid_count = {}         # qid -> times picked (DI)
        fig_qid_count = {}        # qid -> times picked (figure singleton)
        rc_passage_count = {}     # stim_id -> times picked
        di_first_seen = {}
        fig_first_seen = {}
        rc_passage_first_seen = {}
        first_repeat = {"di": None, "figure_singleton": None,
                        "rc_passage": None}

        SECTIONS = [
            ("V1", "verbal", V1),
            ("V2", "verbal", V2),
            ("Q1", "quant", Q1),
            ("Q2", "quant", Q2),
        ]

        for mock_idx in range(1, N_MOCKS + 1):
            in_mock = set()
            # Track passage/DI stims seen in THIS mock so multi-sibling
            # clusters count as one exposure of their parent passage,
            # not one per child qid.
            mock_rc_stims = set()
            mock_di_stims = set()
            for _, measure, count in SECTIONS:
                qids = qb.select_questions_composed(
                    measure=measure, count=count, difficulty_band="medium",
                    exclude_ids=list(in_mock),
                    exclude_user_seen=user_id,
                )
                if not qids:
                    continue
                rows = list(
                    Question.select(Question.id, Question.subtype,
                                    Question.stimulus, Question.measure)
                    .where(Question.id.in_(qids))
                )
                stim_ids = [r.stimulus_id for r in rows
                            if r.stimulus_id is not None]
                stim_type_map = {}
                if stim_ids:
                    sr = (Stimulus.select(Stimulus.id, Stimulus.stimulus_type,
                                          Stimulus.content)
                          .where(Stimulus.id.in_(stim_ids)))
                    stim_type_map = {s.id: (s.stimulus_type, s.content or "")
                                     for s in sr}
                for r in rows:
                    qid = r.id
                    in_mock.add(qid)
                    stim_type, stim_content = stim_type_map.get(
                        r.stimulus_id, (None, ""))
                    is_di = (
                        r.subtype == "data_interp"
                        or (r.measure == "quant"
                            and stim_type in ("graph", "table", "chart"))
                    )
                    is_figure_content = bool(
                        stim_content and (
                            "<img" in stim_content
                            or "data:image/" in stim_content
                            or "<table" in stim_content)
                    )
                    is_rc = r.subtype in ("rc_single", "rc_multi",
                                          "rc_select_passage")
                    if is_di:
                        di_qid_count[qid] = di_qid_count.get(qid, 0) + 1
                        if qid in di_first_seen and first_repeat["di"] is None:
                            first_repeat["di"] = mock_idx
                        di_first_seen.setdefault(qid, mock_idx)
                    elif is_figure_content and r.measure == "quant":
                        fig_qid_count[qid] = fig_qid_count.get(qid, 0) + 1
                        if (qid in fig_first_seen
                                and first_repeat["figure_singleton"] is None):
                            first_repeat["figure_singleton"] = mock_idx
                        fig_first_seen.setdefault(qid, mock_idx)
                    if is_rc and r.stimulus_id is not None:
                        s = r.stimulus_id
                        # Count a passage only once per mock regardless of
                        # how many of its children the cluster pulled in.
                        if s in mock_rc_stims:
                            continue
                        mock_rc_stims.add(s)
                        rc_passage_count[s] = rc_passage_count.get(s, 0) + 1
                        if (s in rc_passage_first_seen
                                and first_repeat["rc_passage"] is None):
                            first_repeat["rc_passage"] = mock_idx
                        rc_passage_first_seen.setdefault(s, mock_idx)

        # Never-repeated sentinel: treat as "after the window".
        for k in list(first_repeat):
            if first_repeat[k] is None:
                first_repeat[k] = N_MOCKS + 1

        fig_max = max(fig_qid_count.values(), default=0)

        if not db.is_closed():
            db.close()

        # Acceptance assertions (R3 final thresholds).
        assert first_repeat["di"] >= 12, (
            f"DI repeat floor: first repeat mock={first_repeat['di']}, "
            f"want >=12 (di_counts_top={sorted(di_qid_count.values(), reverse=True)[:10]})")
        assert first_repeat["rc_passage"] >= 8, (
            f"RC passage repeat floor: first repeat mock="
            f"{first_repeat['rc_passage']}, want >=8")
        assert fig_max <= 3, (
            f"figure singleton hot cap: max={fig_max}, "
            f"want <=3 (counts={fig_qid_count})")
    finally:
        config.DB_PATH = orig_db_path
        config.SEED_DB_PATH = orig_seed_path
        for prefix in ("models", "services"):
            for mod in [m for m in list(sys.modules)
                        if m.startswith(prefix + ".") or m == prefix]:
                del sys.modules[mod]
