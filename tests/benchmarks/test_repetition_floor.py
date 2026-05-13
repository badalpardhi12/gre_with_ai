"""
Phase 0 — repetition-floor benchmark harness.

Simulates 20 consecutive full mocks (V1·12 + V2·15 + Q1·12 + Q2·15 = 54 picks
per mock) against the **real seed question bank** and records how quickly
items, figures, and RC passages start repeating.

Why this matters: the research brief (``research/gre-repetitiveness-roadmap``)
showed DI items repeating by mock #3 and a single figure qid served 8× in
10 mocks on the current assembler. Phase 1 (R1–R5) aims to push DI first-
repeat to ≥ mock 12, cap figure qid exposure at ≤ 3× per 20 mocks, and
keep RC passages fresh for ≥ 8 mocks. This harness is the regression
gate: today the target assertions in ``test_post_phase1_targets`` are
expected to fail (marked ``xfail``) — they flip to green after R1–R5 ship.

Key design choices:

* We **do not** use ``conftest.temp_db`` — that fixture builds an empty DB
  and suppresses the seed copy, which would give us nothing to select
  from. Instead we copy the shipped seed DB (~4k live items) to a tmp
  path, rebind ``config.DB_PATH`` at it, and truncate ``Response`` so
  the simulated user starts with no history.

* We exercise ``select_questions_composed`` directly for each of the 4
  sections per mock (same entry point ``ExamSession.build_full_mock``
  uses). Section adaptation is ignored — we always request
  ``difficulty_band="medium"`` — to keep the benchmark deterministic.

* Since we never write Response rows, the ``exclude_user_seen`` dedup
  relies entirely on whatever cross-mock state the assembler persists
  (today: none — R3 will add ``ServedLog``). That's the realistic worst
  case for a fresh user binge-mocking back-to-back.

The benchmark runs in well under 60s on a warm seed DB.
"""
import json
import os
import random
import shutil
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_JSON = Path(__file__).parent / "baseline_2026_05_12.json"

N_MOCKS = 20
V1_COUNT = 12
V2_COUNT = 15
Q1_COUNT = 12
Q2_COUNT = 15

# Deterministic seed so re-runs produce the same baseline. The assembler
# uses ``random.shuffle`` internally for candidate ordering, so fixing
# the seed pins the repetition pattern for regression comparison.
RANDOM_SEED = 20260512


def _figure_bearing_qids(qids, Question, Stimulus):
    """Subset of ``qids`` whose linked stimulus content contains an
    ``<img>``, ``data:image/``, or ``<table>`` marker — same heuristic
    the assembler uses internally in ``_count_figure_bearing``.
    """
    if not qids:
        return set()
    rows = (
        Question
        .select(Question.id, Stimulus.content.alias("c"))
        .join(Stimulus, on=(Stimulus.id == Question.stimulus))
        .where(Question.id.in_(list(qids)))
    )
    out = set()
    for r in rows:
        c = getattr(r, "c", "") or ""
        if "<img" in c or "data:image/" in c or "<table" in c:
            out.add(r.id)
    return out


def _simulate_mocks():
    """Run the 20-mock simulation against the real seed DB and return a
    list of picks per mock.

    Each pick is a dict:
        {
            "mock_idx": int (1-indexed),
            "qid": int,
            "stimulus_id": Optional[int],
            "subtype": str,
            "measure": str,
            "section": "V1" | "V2" | "Q1" | "Q2",
            "is_figure": bool,
            "is_di": bool,
            "is_rc": bool,
        }

    Restores ``config.DB_PATH`` / ``SEED_DB_PATH`` and evicts cached
    model/service modules on exit so later tests in the same session
    see an unmodified environment (mirrors ``conftest.temp_db``'s
    cleanup contract).
    """
    # Copy the shipped seed DB to a scratch file so we never mutate the
    # real one. The seed lives at ``data/gre_mock.db``; runtime normally
    # uses ``data/gre_user.db``. We point DB_PATH at a throwaway copy so
    # any writes (none, here — we just read) stay isolated.
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="gre_bench_"))
    seed_src = PROJECT_ROOT / "data" / "gre_mock.db"
    assert seed_src.exists(), f"real seed DB not found at {seed_src}"
    db_copy = tmpdir / "bench.db"
    shutil.copy2(str(seed_src), str(db_copy))

    import config
    orig_db_path = config.DB_PATH
    orig_seed_path = config.SEED_DB_PATH

    try:
        # Evict any already-loaded models/services so they rebind to our
        # copy. Mirrors the pattern in conftest.temp_db.
        config.DB_PATH = db_copy
        config.SEED_DB_PATH = tmpdir / "no_seed.db"   # skip reconcile
        for prefix in ("models", "services"):
            for mod in [m for m in list(sys.modules)
                        if m.startswith(prefix + ".") or m == prefix]:
                del sys.modules[mod]

        from models.database import (
            db, init_db, Question, Stimulus, Response,
        )
        init_db()

        # Nuke any inherited Response rows so the simulated user starts
        # fresh. (The shipped seed can carry author-testing responses.)
        Response.delete().execute()

        from services.question_bank import QuestionBankService

        random.seed(RANDOM_SEED)
        qb = QuestionBankService()

        picks = []
        user_id = "bench_user"

        # Per-mock: hit V1, V2, Q1, Q2 in that order; exclude in-mock picks
        # so the same qid can't land in both sections of the same mock.
        SECTIONS = [
            ("V1", "verbal", V1_COUNT),
            ("V2", "verbal", V2_COUNT),
            ("Q1", "quant", Q1_COUNT),
            ("Q2", "quant", Q2_COUNT),
        ]

        for mock_idx in range(1, N_MOCKS + 1):
            in_mock_seen = set()
            for section_label, measure, count in SECTIONS:
                qids = qb.select_questions_composed(
                    measure=measure,
                    count=count,
                    difficulty_band="medium",
                    exclude_ids=list(in_mock_seen),
                    exclude_user_seen=user_id,
                )
                # Enrich: fetch stimulus_id + subtype + stimulus type in
                # one query.  After Phase 1 R1 the DI block is composed
                # from items whose *stimulus* is graph/table/chart,
                # regardless of subtype label (real DI children ship as
                # qc / mcq_single / numeric_entry just as often as
                # data_interp). The ``is_di`` classifier must match the
                # selector's definition, otherwise the metric will not
                # reflect the spread the selector is now producing.
                if not qids:
                    continue
                rows = (
                    Question
                    .select(Question.id, Question.subtype, Question.stimulus,
                            Question.measure)
                    .where(Question.id.in_(qids))
                )
                # Second pass: map stimulus_id -> stimulus_type for the
                # qids we picked, so the ``is_di`` classifier can include
                # items whose stimulus is graph/table/chart (post-R1 DI
                # pool is wider than ``subtype == 'data_interp'``).
                stim_ids = [r.stimulus_id for r in rows
                            if r.stimulus_id is not None]
                stim_type_map = {}
                if stim_ids:
                    stim_rows = (Stimulus.select(Stimulus.id,
                                                 Stimulus.stimulus_type)
                                 .where(Stimulus.id.in_(stim_ids)))
                    stim_type_map = {s.id: s.stimulus_type for s in stim_rows}
                meta = {
                    r.id: (r.subtype, r.stimulus_id, r.measure,
                           stim_type_map.get(r.stimulus_id))
                    for r in rows
                }
                fig_set = _figure_bearing_qids(qids, Question, Stimulus)

                for qid in qids:
                    subtype, stim_id, m, stim_type = meta.get(
                        qid, (None, None, measure, None))
                    # DI = explicit ``data_interp`` subtype OR any quant
                    # item whose stimulus is graph/table/chart (the
                    # broader post-R1 DI pool).
                    is_di = (
                        subtype == "data_interp"
                        or (m == "quant"
                            and stim_type in ("graph", "table", "chart"))
                    )
                    is_rc = subtype in ("rc_single", "rc_multi",
                                        "rc_select_passage")
                    is_figure = qid in fig_set
                    picks.append({
                        "mock_idx": mock_idx,
                        "qid": qid,
                        "stimulus_id": stim_id,
                        "subtype": subtype,
                        "measure": m or measure,
                        "section": section_label,
                        "is_figure": is_figure,
                        "is_di": is_di,
                        "is_rc": is_rc,
                    })
                    in_mock_seen.add(qid)

        if not db.is_closed():
            db.close()

        return picks
    finally:
        # Restore config so tests that follow us in the same pytest
        # session (notably test_render_integrity_e2e, which asserts
        # ``config.DB_PATH.name == 'gre_user.db'``) see the original
        # paths. Also evict cached model/service modules so they
        # re-bind against ``gre_user.db`` on next import.
        config.DB_PATH = orig_db_path
        config.SEED_DB_PATH = orig_seed_path
        for prefix in ("models", "services"):
            for mod in [m for m in list(sys.modules)
                        if m.startswith(prefix + ".") or m == prefix]:
                del sys.modules[mod]


def _compute_metrics(picks):
    """Derive the 3 metric groups the plan specifies.

    Passages / DI stims appear multiple times in a single mock naturally
    (cluster-atomic sibling pulls), so we dedupe by (stim_id, mock_idx)
    before recording exposure — a "repeat" is cross-mock only. RC passage
    counts and DI qid counts are likewise per-mock-exposure, not per-qid,
    so a 3-Q passage exposed once counts as 1, not 3.
    """
    # First repeat by bucket
    di_seen_at = {}       # qid -> first mock_idx
    rc_passage_seen_at = {}  # stimulus_id -> first mock_idx
    figure_singleton_seen_at = {}  # qid -> first mock_idx

    first_repeat = {
        "di": None,
        "figure_singleton": None,
        "rc_passage": None,
    }

    all_picks_count = {}           # qid -> int
    figure_qid_count = {}          # qid -> int  (non-DI figure singletons)
    di_qid_count = {}              # qid -> int
    rc_passage_count = {}          # stim_id -> int (per-mock exposures)

    # Per-mock dedup sets so cluster-atomic children count once per mock.
    mock_di_seen = {}              # mock_idx -> set(qid)
    mock_fig_seen = {}             # mock_idx -> set(qid)
    mock_rc_seen = {}              # mock_idx -> set(stim_id)

    for p in picks:
        qid = p["qid"]
        all_picks_count[qid] = all_picks_count.get(qid, 0) + 1
        mock = p["mock_idx"]

        if p["is_di"]:
            di_set = mock_di_seen.setdefault(mock, set())
            if qid not in di_set:
                di_set.add(qid)
                di_qid_count[qid] = di_qid_count.get(qid, 0) + 1
                if qid in di_seen_at and first_repeat["di"] is None:
                    first_repeat["di"] = mock
                di_seen_at.setdefault(qid, mock)

        elif p["is_figure"] and p["measure"] == "quant":
            # figure-bearing non-DI quant (geometry diagrams, etc.)
            fig_set = mock_fig_seen.setdefault(mock, set())
            if qid not in fig_set:
                fig_set.add(qid)
                figure_qid_count[qid] = figure_qid_count.get(qid, 0) + 1
                if (qid in figure_singleton_seen_at
                        and first_repeat["figure_singleton"] is None):
                    first_repeat["figure_singleton"] = mock
                figure_singleton_seen_at.setdefault(qid, mock)

        if p["is_rc"] and p["stimulus_id"] is not None:
            stim = p["stimulus_id"]
            rc_set = mock_rc_seen.setdefault(mock, set())
            if stim in rc_set:
                continue
            rc_set.add(stim)
            rc_passage_count[stim] = rc_passage_count.get(stim, 0) + 1
            if stim in rc_passage_seen_at and first_repeat["rc_passage"] is None:
                first_repeat["rc_passage"] = mock
            rc_passage_seen_at.setdefault(stim, mock)

    # If no repeat observed, sentinel to ``N_MOCKS + 1`` so downstream
    # assertions ("≥ 12") treat "never repeated" as the best outcome.
    for k in list(first_repeat.keys()):
        if first_repeat[k] is None:
            first_repeat[k] = N_MOCKS + 1

    hot_items = {
        "all": {str(q): c for q, c in all_picks_count.items() if c >= 3},
        "di": {str(q): c for q, c in di_qid_count.items() if c >= 3},
        "figure_singleton": {str(q): c for q, c in figure_qid_count.items()
                             if c >= 3},
        "rc_passage": {str(s): c for s, c in rc_passage_count.items()
                       if c >= 3},
    }

    unique = {
        "di": len(di_qid_count),
        "figure_singleton": len(figure_qid_count),
        "rc_passage": len(rc_passage_count),
        "all_items": len(all_picks_count),
    }

    return {
        "meta": {
            "n_mocks": N_MOCKS,
            "random_seed": RANDOM_SEED,
            "total_picks": len(picks),
        },
        "first_repeat_mock_by_bucket": first_repeat,
        "hot_items": hot_items,
        "unique_items_by_bucket": unique,
    }


# Module-level cache so both tests share one simulation run.
_METRICS_CACHE = {}


def _get_metrics():
    if "metrics" not in _METRICS_CACHE:
        picks = _simulate_mocks()
        _METRICS_CACHE["metrics"] = _compute_metrics(picks)
    return _METRICS_CACHE["metrics"]


def test_baseline_snapshot():
    """Run the 20-mock simulation and persist a snapshot JSON.

    Always passes — this is the recording step, not the gate. The gate
    lives in ``test_post_phase1_targets`` below.

    Preservation rule: the Phase 0 baseline (``baseline_2026_05_12.json``)
    is the *before-fix* artifact and must never be rewritten once Phase
    1 work starts. If the baseline file already exists, this test writes
    the current run's metrics to ``current_snapshot.json`` instead so
    you can diff against the baseline without clobbering it.
    """
    metrics = _get_metrics()
    BASELINE_JSON.parent.mkdir(parents=True, exist_ok=True)
    if BASELINE_JSON.exists():
        # Preserve the pre-R1 baseline; write to a rolling snapshot file.
        snapshot_path = BASELINE_JSON.parent / "current_snapshot.json"
    else:
        snapshot_path = BASELINE_JSON
    with open(snapshot_path, "w") as f:
        json.dump(metrics, f, indent=2, sort_keys=True)
    # Minimal sanity asserts — if these fail the simulation is broken,
    # not the assembler.
    assert metrics["meta"]["total_picks"] > 0
    assert metrics["unique_items_by_bucket"]["all_items"] > 0


def test_post_phase1_targets():
    """Regression gate for Phase 1 (R1–R5).

    These thresholds come from ``docs/implementation_plan_2026_05_12.md``
    Phase 1 acceptance criteria. Originally ``xfail`` on pre-R3 main
    (the DI / figure / RC passage repetition floor was far below target);
    flipped to a strict gate after R3 landed (``ServedLog`` dedup at
    pick time) so any regression that brings the floor back below
    Phase 1's targets turns this test red immediately.
    """
    metrics = _get_metrics()
    first = metrics["first_repeat_mock_by_bucket"]
    hot = metrics["hot_items"]

    assert first["di"] >= 12, (
        f"DI repeat floor: first repeat mock={first['di']}, want >=12")

    assert first["rc_passage"] >= 8, (
        f"RC passage repeat floor: first repeat mock={first['rc_passage']}, "
        "want >=8")

    fig_counts = hot.get("figure_singleton", {})
    max_fig = max(fig_counts.values(), default=0)
    assert max_fig <= 3, (
        f"no figure qid served >3x across {N_MOCKS} mocks; "
        f"observed max={max_fig}, hot={fig_counts}")
