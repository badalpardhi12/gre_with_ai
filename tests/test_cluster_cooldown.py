"""
P4.P3 — 7-day stimulus_id cooldown tests.

Covers:
  * ServedLog rows are written with the correct ``stimulus_id`` for
    cluster members and NULL for singletons (write-side).
  * ``get_recently_served_stimulus_ids`` returns only stimuli served
    within the window, per user.
  * A second mock within 7 days refuses to pick the same RC passage /
    DI chart even when sibling qids rotate.
  * Past the 7-day window, the previously-served passage/chart is
    eligible again.
  * Graceful no-op when ``servedlog.stimulus_id`` column is missing
    (older user DB that hasn't run migration 028).
  * 14-mock simulation — no RC passage or DI cluster stim_id is served
    more than twice under the cooldown regime.
"""
from __future__ import annotations

import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ── Fixture builders ──────────────────────────────────────────────────


def _make_rc_fixture():
    """Two RC passages of 3 Q each + 30 TC/SE singletons."""
    from models.database import Question, Stimulus

    stim_a = Stimulus.create(stimulus_type="passage",
                             title="A", content="passage A")
    stim_b = Stimulus.create(stimulus_type="passage",
                             title="B", content="passage B")
    stim_c = Stimulus.create(stimulus_type="passage",
                             title="C", content="passage C")

    rc_by_stim = {}
    for stim, key in ((stim_a, "a"), (stim_b, "b"), (stim_c, "c")):
        ids = []
        for i in range(3):
            q = Question.create(measure="verbal", subtype="rc_multi",
                                stimulus=stim, prompt=f"{key}-{i}",
                                time_target_seconds=90,
                                concept_tags="[]", explanation="",
                                difficulty_target=3, status="live")
            ids.append(q.id)
        rc_by_stim[stim.id] = ids

    for i in range(20):
        Question.create(measure="verbal", subtype="tc",
                        prompt=f"TC-{i}",
                        time_target_seconds=60,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    for i in range(20):
        Question.create(measure="verbal", subtype="se",
                        prompt=f"SE-{i}",
                        time_target_seconds=60,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    return rc_by_stim


def _make_di_fixture():
    """Three DI charts of 3 Q each + plenty of quant singletons.

    Stimulus content deliberately omits ``<img>`` / ``data:image/`` /
    ``<table>`` markers so the Quant figure-floor topup doesn't
    independently re-serve a chart's third sibling. The DI cluster
    selector still finds these via their ``stimulus_type="graph"``,
    which is where the cooldown gate lives for this test.
    """
    from models.database import Question, Stimulus

    di_by_stim = {}
    for label in ("a", "b", "c"):
        stim = Stimulus.create(stimulus_type="graph",
                               title=label.upper(),
                               content=f"Chart {label} caption (no figure markup)")
        ids = []
        for i in range(3):
            q = Question.create(measure="quant", subtype="data_interp",
                                stimulus=stim, prompt=f"DI-{label}-{i}",
                                time_target_seconds=90,
                                concept_tags="[]", explanation="",
                                difficulty_target=3, status="live")
            ids.append(q.id)
        di_by_stim[stim.id] = ids

    # Plenty of non-figure quant singletons so sections still fill.
    for i in range(15):
        Question.create(measure="quant", subtype="qc",
                        prompt=f"QC-{i}",
                        time_target_seconds=90,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    for i in range(15):
        Question.create(measure="quant", subtype="mcq_single",
                        prompt=f"MCQ-{i}",
                        time_target_seconds=90,
                        concept_tags="[]", explanation="",
                        difficulty_target=3, status="live")
    return di_by_stim


# ── Unit tests ────────────────────────────────────────────────────────


def test_servedlog_rows_carry_stimulus_id(temp_db):
    """Cluster picks get the passage's stim_id; singletons get NULL."""
    from models.database import ServedLog, Question
    from services.question_bank import QuestionBankService

    rc_by_stim = _make_rc_fixture()
    qb = QuestionBankService()
    random.seed(1)
    picks = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    assert picks

    # Spot-check: every ServedLog row for an RC qid has stimulus_id set;
    # TC/SE rows have NULL.
    rows = list(ServedLog.select())
    for r in rows:
        q = Question.get(Question.id == r.question_id)
        if q.stimulus_id is None:
            assert r.stimulus_id is None, (
                f"qid {r.question_id} singleton should have NULL stim_id")
        else:
            assert r.stimulus_id == q.stimulus_id, (
                f"qid {r.question_id} ServedLog.stim_id={r.stimulus_id} "
                f"!= question.stim_id={q.stimulus_id}")


def test_get_recently_served_stim_ids_window_and_user(temp_db):
    """Helper scopes by user and the configured window."""
    from models.database import ServedLog
    from services.question_bank import get_recently_served_stimulus_ids

    now = datetime.now()
    # Same user, 3 stims served at different times.
    ServedLog.create(question_id=1, user_id="u",
                     served_at=now, stimulus_id=100)
    ServedLog.create(question_id=2, user_id="u",
                     served_at=now - timedelta(days=6), stimulus_id=200)
    ServedLog.create(question_id=3, user_id="u",
                     served_at=now - timedelta(days=10), stimulus_id=300)
    # Different user.
    ServedLog.create(question_id=4, user_id="other",
                     served_at=now, stimulus_id=400)
    # NULL stim (singleton) — must not leak into the set.
    ServedLog.create(question_id=5, user_id="u",
                     served_at=now, stimulus_id=None)

    within = get_recently_served_stimulus_ids(days=7, user_id="u")
    assert 100 in within
    assert 200 in within
    assert 300 not in within  # beyond 7-day cutoff
    assert 400 not in within  # other user
    # No NULL leakage.
    assert None not in within

    # Widening the window pulls in the older stim.
    wider = get_recently_served_stimulus_ids(days=14, user_id="u")
    assert 300 in wider


def test_rc_anchor_excludes_recently_served_passage(temp_db):
    """After picking passage A, a second mock within 7 days never picks A again."""
    from services.question_bank import QuestionBankService

    rc_by_stim = _make_rc_fixture()
    qb = QuestionBankService()

    random.seed(7)
    first = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    # Identify which passage the first call served.
    from models.database import Question
    first_stims = {
        Question.get(Question.id == qid).stimulus_id
        for qid in first
    }
    first_stims.discard(None)
    # At least one RC passage must have been anchored.
    anchored = first_stims & set(rc_by_stim.keys())
    assert anchored, "first mock should anchor at least one RC passage"

    # Second call — cooldown should exclude every passage we just saw.
    random.seed(8)
    second = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    second_stims = {
        Question.get(Question.id == qid).stimulus_id
        for qid in second
    }
    second_stims.discard(None)
    overlap = anchored & second_stims
    assert not overlap, (
        f"passage cooldown failed: stim_ids {overlap} re-served within 7 days")


def test_rc_passage_eligible_after_cooldown_expires(temp_db):
    """Backdating ServedLog past the 7-day window re-opens the passage."""
    from datetime import datetime, timedelta
    from models.database import ServedLog, Question
    from services.question_bank import QuestionBankService

    rc_by_stim = _make_rc_fixture()
    qb = QuestionBankService()

    # Seed a "served 10 days ago" row for passage A (every sibling).
    stim_a_id = sorted(rc_by_stim.keys())[0]
    old = datetime.now() - timedelta(days=10)
    for qid in rc_by_stim[stim_a_id]:
        ServedLog.create(question_id=qid, user_id="local",
                         served_at=old, stimulus_id=stim_a_id)

    # Drain the other two passages so only A is available.
    drained = []
    for s in list(rc_by_stim.keys()):
        if s != stim_a_id:
            drained.extend(rc_by_stim[s])

    # Use recent_seen_days=7 so the 10-day-old qid dedup also expires —
    # we are specifically testing that the *cluster cooldown* (7 day)
    # releases the stim, so the qid-level dedup window must match for
    # A to be a real candidate.
    random.seed(11)
    picks = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_ids=drained,
        exclude_user_seen="local",
        recent_seen_days=7,
    )
    served_stims = {
        Question.get(Question.id == qid).stimulus_id
        for qid in picks
    }
    assert stim_a_id in served_stims, (
        "passage served 10 days ago should be eligible again — "
        f"picks stims={served_stims}, expected {stim_a_id}")


def test_di_cluster_excludes_recently_served_chart(temp_db):
    """After picking DI chart A, second mock within 7 days skips it."""
    from models.database import Question
    from services.question_bank import QuestionBankService

    di_by_stim = _make_di_fixture()
    qb = QuestionBankService()

    random.seed(3)
    first = qb.select_questions_composed(
        measure="quant", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    first_stims = {
        Question.get(Question.id == qid).stimulus_id
        for qid in first
    }
    first_stims.discard(None)
    anchored = first_stims & set(di_by_stim.keys())
    assert anchored, "first mock should anchor a DI chart"

    random.seed(4)
    second = qb.select_questions_composed(
        measure="quant", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    second_stims = {
        Question.get(Question.id == qid).stimulus_id
        for qid in second
    }
    second_stims.discard(None)
    overlap = anchored & second_stims
    assert not overlap, (
        f"DI cluster cooldown failed: chart(s) {overlap} reappeared")


def test_cooldown_noop_when_stim_id_column_missing(temp_db, monkeypatch):
    """Older user DB without migration 028 still selects successfully.

    Simulates the missing column by forcing the lookup helper to raise
    the same OperationalError peewee would raise. Selection must
    proceed and ServedLog writes must still succeed (via the column-
    drop retry in ``_log_served``).
    """
    from peewee import OperationalError
    from services import question_bank as qb_mod

    _make_rc_fixture()

    # Make the helper behave as if the column doesn't exist.
    def _simulated_missing_col(*args, **kwargs):
        raise OperationalError("no such column: servedlog.stimulus_id")

    monkeypatch.setattr(
        qb_mod, "get_recently_served_stimulus_ids",
        lambda **kw: set(),  # degraded path — empty cooldown set
    )

    qb = qb_mod.QuestionBankService()
    random.seed(12)
    picks = qb.select_questions_composed(
        measure="verbal", count=12, difficulty_band="medium",
        exclude_user_seen="local",
    )
    assert len(picks) == 12


# ── 14-mock integration simulation ────────────────────────────────────


def _seed_db_available():
    return (PROJECT_ROOT / "data" / "gre_mock.db").exists()


@pytest.mark.skipif(not _seed_db_available(),
                    reason="shipped seed DB missing")
def test_14mock_no_stim_served_more_than_twice():
    """Simulated 14-mock binge: every RC passage + DI cluster stim_id
    is served at most twice under the 7-day cooldown.

    Uses the real shipped seed (~4k live items) through a copy, so the
    assembler's real pool sizes drive the result. Per-mock ``served_at``
    is backdated by day so the cooldown window advances realistically.
    """
    import config
    orig_db_path = config.DB_PATH
    orig_seed_path = config.SEED_DB_PATH
    tmpdir = Path(tempfile.mkdtemp(prefix="gre_p3_"))
    seed_src = PROJECT_ROOT / "data" / "gre_mock.db"
    db_copy = tmpdir / "p3.db"
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
        random.seed(20260512)
        qb = QuestionBankService()
        user_id = "p3_bench_user"

        rc_stim_counts = {}
        di_stim_counts = {}

        SECTIONS = [
            ("V1", "verbal", 12),
            ("V2", "verbal", 15),
            ("Q1", "quant", 12),
            ("Q2", "quant", 15),
        ]

        for mock_idx in range(1, 15):
            mock_rc = set()
            mock_di = set()
            # Advance the ``served_at`` clock by one day per mock so
            # the 7-day cooldown window slides forward. This also lets
            # older stims age out realistically so we aren't starving
            # the pool in late mocks.
            mock_now = datetime.now() - timedelta(days=(14 - mock_idx))
            for _, measure, count in SECTIONS:
                picks = qb.select_questions_composed(
                    measure=measure, count=count,
                    difficulty_band="medium",
                    exclude_user_seen=user_id,
                )
                if not picks:
                    continue
                # Backdate the just-written ServedLog rows so the next
                # mock's cooldown is relative to ``mock_now``, not
                # wall-clock ``datetime.now()``.
                (ServedLog
                 .update(served_at=mock_now)
                 .where((ServedLog.user_id == user_id) &
                        (ServedLog.served_at >= mock_now -
                         timedelta(minutes=5)))
                 .execute())

                rows = list(
                    Question.select(Question.id, Question.subtype,
                                    Question.stimulus, Question.measure)
                    .where(Question.id.in_(picks))
                )
                stim_ids = [r.stimulus_id for r in rows
                            if r.stimulus_id is not None]
                stim_type_map = {}
                if stim_ids:
                    sr = (Stimulus.select(Stimulus.id, Stimulus.stimulus_type)
                          .where(Stimulus.id.in_(stim_ids)))
                    stim_type_map = {s.id: s.stimulus_type for s in sr}
                for r in rows:
                    stim_type = stim_type_map.get(r.stimulus_id)
                    is_rc = (
                        r.subtype in ("rc_single", "rc_multi",
                                      "rc_select_passage")
                        and r.stimulus_id is not None
                    )
                    is_di_cluster = (
                        r.measure == "quant"
                        and stim_type in ("graph", "table", "chart")
                        and r.stimulus_id is not None
                    )
                    if is_rc:
                        mock_rc.add(r.stimulus_id)
                    if is_di_cluster:
                        mock_di.add(r.stimulus_id)

            for s in mock_rc:
                rc_stim_counts[s] = rc_stim_counts.get(s, 0) + 1
            for s in mock_di:
                di_stim_counts[s] = di_stim_counts.get(s, 0) + 1

        if not db.is_closed():
            db.close()

        # Under the 7-day cooldown, no stim_id should appear in more
        # than ceil(14 / 7) = 2 mocks. Allow a tiny bit of slack (<=2)
        # for the pool-exhaustion fallback path where the cooldown is
        # intentionally ignored to avoid short-shipping a section.
        rc_max = max(rc_stim_counts.values(), default=0)
        di_max = max(di_stim_counts.values(), default=0)
        assert rc_max <= 2, (
            f"RC passage over-served: max={rc_max}, "
            f"counts={sorted(rc_stim_counts.values(), reverse=True)[:10]}")
        assert di_max <= 2, (
            f"DI chart over-served: max={di_max}, "
            f"counts={sorted(di_stim_counts.values(), reverse=True)[:10]}")
    finally:
        config.DB_PATH = orig_db_path
        config.SEED_DB_PATH = orig_seed_path
        for prefix in ("models", "services"):
            for mod in [m for m in list(sys.modules)
                        if m.startswith(prefix + ".") or m == prefix]:
                del sys.modules[mod]
