"""
Cleanup-benchmark harness — Phase 0c, docs/implementation_plan_2026_05_18.md
sections 118-132.

Snapshots dual-DB state (content + telemetry) into a single JSON baseline
that future cleanup phases compare against. The thresholds locked in below
intentionally match the **current** state (2026-05-18) so this test is
GREEN today; subsequent phases (dedup, taxonomy normalization, balance,
correctness) will tighten the same thresholds in-place.

What the snapshot captures (per §122-128):

1. Content-DB row counts faceted by (source, measure, subtype,
   difficulty_target, status).
2. DI clusters — `graph` or `table` stimuli with ≥2 live children, plus
   the per-cluster sibling-count distribution. (Phase 1 R1 widened the
   DI selector to include any quant item whose stimulus is graph/table,
   so the cluster count here is the upstream pool — not the assembler-
   side reachability number.)
3. ``figure_refs`` non-null counts per (measure, subtopic).
4. ``irt_a_estimate`` / ``irt_b_estimate`` non-null counts.
5. Per-source taxonomy distribution (distinct topic / subtopic /
   question_type per source).
6. User-DB telemetry counts (response, session, servedlog, itemrating,
   itemreview, vocabcontextitem, sync_state) — gracefully skipped if a
   table is absent in this DB instance.

Runs in ~1s on a warm DB; the <30s acceptance budget is generous.

Layout note: both ``data/gre_mock.db`` and ``data/gre_user.db`` carry the
``question`` / ``stimulus`` schema (the user DB is bootstrapped from a
copy of the shipped seed at first launch — see ``config._bootstrap_user_db``).
The user DB is the one Peewee reads through ``models.database.db``;
``data/gre_mock.db`` is the read-only seed. We open the seed directly
via raw sqlite3 so the snapshot reflects what Phase-1+ migrations will
land on, while leaving the live Peewee binding untouched.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTENT_DB_PATH = PROJECT_ROOT / "data" / "gre_mock.db"
USER_DB_PATH = PROJECT_ROOT / "data" / "gre_user.db"
BENCHMARK_FILE = Path(__file__).parent / "cleanup_baseline_2026_05_18.json"

# Telemetry tables we care about for the user-DB snapshot. Some are added
# by late migrations and may not exist on every dev DB — we skip absent
# tables instead of failing.
USER_DB_TABLES = (
    "response",
    "session",
    "servedlog",
    "itemrating",
    "itemreview",
    "vocabcontextitem",
    "sync_state",
)


# ── Helpers ──────────────────────────────────────────────────────────


def _connect(path: Path) -> sqlite3.Connection:
    """Open a read-only-ish handle. We use the standard URI form so a
    journal-mode WAL DB stays consumable without locking out the running
    app. Only SELECTs are issued from this harness."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cur.fetchone() is not None


def _facet_counts(
    conn: sqlite3.Connection, columns: Tuple[str, ...]
) -> Dict[str, int]:
    """Return {"col1=v1|col2=v2": count} for the given column tuple.
    NULLs are stringified as ``"<null>"`` so JSON keys stay strings."""
    col_list = ", ".join(columns)
    rows = conn.execute(
        f"SELECT {col_list}, COUNT(*) AS n FROM question GROUP BY {col_list}"
    ).fetchall()
    out: Dict[str, int] = {}
    for r in rows:
        parts = []
        for c in columns:
            v = r[c]
            parts.append(f"{c}={'<null>' if v is None else v}")
        out["|".join(parts)] = int(r["n"])
    return out


# ── Snapshot sections ────────────────────────────────────────────────


def _snapshot_content_counts(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Section (a) — per-(source, measure, subtype, difficulty_target,
    status) row counts plus convenience totals."""
    totals_by_status: Dict[str, int] = {}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM question GROUP BY status"
    ).fetchall()
    for r in rows:
        totals_by_status[r["status"] or "<null>"] = int(r["n"])

    totals_by_source: Dict[str, int] = {}
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM question GROUP BY source"
    ).fetchall()
    for r in rows:
        totals_by_source[r["source"] or "<null>"] = int(r["n"])

    totals_by_measure: Dict[str, int] = {}
    rows = conn.execute(
        "SELECT measure, COUNT(*) AS n FROM question GROUP BY measure"
    ).fetchall()
    for r in rows:
        totals_by_measure[r["measure"] or "<null>"] = int(r["n"])

    totals_by_subtype: Dict[str, int] = {}
    rows = conn.execute(
        "SELECT subtype, COUNT(*) AS n FROM question GROUP BY subtype"
    ).fetchall()
    for r in rows:
        totals_by_subtype[r["subtype"] or "<null>"] = int(r["n"])

    totals_by_difficulty: Dict[str, int] = {}
    rows = conn.execute(
        "SELECT difficulty_target, COUNT(*) AS n FROM question "
        "GROUP BY difficulty_target ORDER BY difficulty_target"
    ).fetchall()
    for r in rows:
        key = "<null>" if r["difficulty_target"] is None else str(r["difficulty_target"])
        totals_by_difficulty[key] = int(r["n"])

    total = int(
        conn.execute("SELECT COUNT(*) AS n FROM question").fetchone()["n"]
    )

    return {
        "totals": {
            "all": total,
            "live": totals_by_status.get("live", 0),
            "draft": totals_by_status.get("draft", 0),
            "candidate": totals_by_status.get("candidate", 0),
            "review": totals_by_status.get("review", 0),
            "pretest": totals_by_status.get("pretest", 0),
            "pilot": totals_by_status.get("pilot", 0),
            "retired": totals_by_status.get("retired", 0),
        },
        "by_status": totals_by_status,
        "by_source": totals_by_source,
        "by_measure": totals_by_measure,
        "by_subtype": totals_by_subtype,
        "by_difficulty_target": totals_by_difficulty,
        # Full faceted cube — useful for diffing across phases. Keys look
        # like "source=kaplan_2024|measure=quant|subtype=qc|difficulty_target=3|status=live".
        "by_full_facet": _facet_counts(
            conn,
            ("source", "measure", "subtype", "difficulty_target", "status"),
        ),
    }


def _snapshot_di_clusters(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Section (b) — DI clusters = graph/table stimuli with ≥2 live
    children. Per-cluster sibling-count distribution doubles as a
    "how flat is the cluster pool?" signal Phase 1 needs to flatten."""
    rows = conn.execute(
        """
        SELECT s.id AS stim_id, s.stimulus_type, COUNT(q.id) AS n_live
        FROM stimulus s
        JOIN question q ON q.stimulus_id = s.id AND q.status = 'live'
        WHERE s.stimulus_type IN ('graph', 'table')
        GROUP BY s.id
        HAVING COUNT(q.id) >= 2
        ORDER BY n_live DESC, s.id
        """
    ).fetchall()

    clusters: List[Dict[str, Any]] = []
    by_type: Dict[str, int] = {"graph": 0, "table": 0}
    sibling_counts: List[int] = []
    for r in rows:
        n_live = int(r["n_live"])
        clusters.append(
            {
                "stim_id": int(r["stim_id"]),
                "stimulus_type": r["stimulus_type"],
                "n_live": n_live,
            }
        )
        by_type[r["stimulus_type"]] = by_type.get(r["stimulus_type"], 0) + 1
        sibling_counts.append(n_live)

    distribution = dict(Counter(sibling_counts))
    # Stringify keys so JSON output is deterministic and key-order-stable.
    distribution_serialized = {str(k): v for k, v in sorted(distribution.items())}

    # Also expose how many graph/table stim exist regardless of children
    # — that's the upper bound on cluster recovery work in Phase 1.
    total_graph_table_stim = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM stimulus "
            "WHERE stimulus_type IN ('graph', 'table')"
        ).fetchone()["n"]
    )

    # Stim with zero or one live child — these are the "orphans" Phase 1
    # cluster-rebuilding will need to either resurrect or retire.
    orphan_stim = int(
        conn.execute(
            """
            SELECT COUNT(*) AS n FROM stimulus s
            WHERE s.stimulus_type IN ('graph', 'table')
              AND (
                  SELECT COUNT(*) FROM question q
                  WHERE q.stimulus_id = s.id AND q.status = 'live'
              ) < 2
            """
        ).fetchone()["n"]
    )

    return {
        "total_clusters": len(clusters),
        "clusters_by_type": by_type,
        "sibling_count_distribution": distribution_serialized,
        "clusters_with_3plus_live_siblings": sum(
            1 for c in clusters if c["n_live"] >= 3
        ),
        "clusters_with_4plus_live_siblings": sum(
            1 for c in clusters if c["n_live"] >= 4
        ),
        "total_graph_table_stim": total_graph_table_stim,
        "orphan_graph_table_stim": orphan_stim,
        "clusters": clusters,
    }


def _snapshot_figure_refs(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Section (c) — figure_refs non-null counts per (measure, subtopic).

    "Non-null" here means populated AND not the default empty list
    (``"[]"``); a row with ``figure_refs = "[]"`` is just a placeholder
    and shouldn't count toward image-bearing coverage."""
    rows = conn.execute(
        """
        SELECT measure, subtopic, COUNT(*) AS n
        FROM question
        WHERE figure_refs IS NOT NULL
          AND figure_refs != ''
          AND figure_refs != '[]'
        GROUP BY measure, subtopic
        ORDER BY n DESC
        """
    ).fetchall()

    by_measure_subtopic: Dict[str, int] = {}
    by_measure: Dict[str, int] = {}
    total = 0
    for r in rows:
        m = r["measure"] or "<null>"
        st = r["subtopic"] or "<empty>"
        by_measure_subtopic[f"{m}|{st}"] = int(r["n"])
        by_measure[m] = by_measure.get(m, 0) + int(r["n"])
        total += int(r["n"])

    return {
        "total_with_figure_refs": total,
        "by_measure": by_measure,
        "by_measure_subtopic": by_measure_subtopic,
    }


def _snapshot_irt(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Section (d) — irt_a_estimate / irt_b_estimate non-null counts.
    On 2026-05-18 these are zero across the board; once Phase 2
    pretesting lands they'll start filling."""
    n_a = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM question WHERE irt_a_estimate IS NOT NULL"
        ).fetchone()["n"]
    )
    n_b = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM question WHERE irt_b_estimate IS NOT NULL"
        ).fetchone()["n"]
    )
    n_either = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM question "
            "WHERE irt_a_estimate IS NOT NULL OR irt_b_estimate IS NOT NULL"
        ).fetchone()["n"]
    )
    n_both = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM question "
            "WHERE irt_a_estimate IS NOT NULL AND irt_b_estimate IS NOT NULL"
        ).fetchone()["n"]
    )
    return {
        "irt_a_non_null": n_a,
        "irt_b_non_null": n_b,
        "irt_either_non_null": n_either,
        "irt_both_non_null": n_both,
    }


def _snapshot_taxonomy(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Section (e) — per-source distinct (topic / subtopic / question_type)
    counts, plus the long-tail combinations themselves so a future phase
    can diff exactly which subtopic labels appeared/disappeared after
    normalization."""
    sources_rows = conn.execute(
        "SELECT DISTINCT source FROM question ORDER BY source"
    ).fetchall()

    per_source: Dict[str, Dict[str, Any]] = {}
    for sr in sources_rows:
        src = sr["source"] or "<null>"
        # Distinct counts.
        n_topic = int(
            conn.execute(
                "SELECT COUNT(DISTINCT topic) AS n FROM question WHERE source IS ?",
                (sr["source"],),
            ).fetchone()["n"]
        )
        n_subtopic = int(
            conn.execute(
                "SELECT COUNT(DISTINCT subtopic) AS n FROM question WHERE source IS ?",
                (sr["source"],),
            ).fetchone()["n"]
        )
        n_qtype = int(
            conn.execute(
                "SELECT COUNT(DISTINCT question_type) AS n FROM question "
                "WHERE source IS ?",
                (sr["source"],),
            ).fetchone()["n"]
        )

        # Distinct topic / subtopic / question_type sets, with row counts
        # for each so a "topic was renamed" diff is interpretable.
        topic_dist: Dict[str, int] = {}
        for r in conn.execute(
            "SELECT topic, COUNT(*) AS n FROM question WHERE source IS ? "
            "GROUP BY topic ORDER BY n DESC",
            (sr["source"],),
        ).fetchall():
            topic_dist[r["topic"] or "<empty>"] = int(r["n"])

        subtopic_dist: Dict[str, int] = {}
        for r in conn.execute(
            "SELECT subtopic, COUNT(*) AS n FROM question WHERE source IS ? "
            "GROUP BY subtopic ORDER BY n DESC",
            (sr["source"],),
        ).fetchall():
            subtopic_dist[r["subtopic"] or "<empty>"] = int(r["n"])

        qtype_dist: Dict[str, int] = {}
        for r in conn.execute(
            "SELECT question_type, COUNT(*) AS n FROM question WHERE source IS ? "
            "GROUP BY question_type ORDER BY n DESC",
            (sr["source"],),
        ).fetchall():
            qtype_dist[r["question_type"] or "<empty>"] = int(r["n"])

        per_source[src] = {
            "n_topics": n_topic,
            "n_subtopics": n_subtopic,
            "n_question_types": n_qtype,
            "topic_dist": topic_dist,
            "subtopic_dist": subtopic_dist,
            "question_type_dist": qtype_dist,
        }

    # Cross-source distinct counts for at-a-glance "is the taxonomy
    # converged?" view.
    n_total_topics = int(
        conn.execute("SELECT COUNT(DISTINCT topic) AS n FROM question").fetchone()["n"]
    )
    n_total_subtopics = int(
        conn.execute(
            "SELECT COUNT(DISTINCT subtopic) AS n FROM question"
        ).fetchone()["n"]
    )
    n_total_qtypes = int(
        conn.execute(
            "SELECT COUNT(DISTINCT question_type) AS n FROM question"
        ).fetchone()["n"]
    )

    return {
        "global": {
            "n_topics": n_total_topics,
            "n_subtopics": n_total_subtopics,
            "n_question_types": n_total_qtypes,
        },
        "per_source": per_source,
    }


def _snapshot_user_db(conn: Optional[sqlite3.Connection]) -> Dict[str, Any]:
    """User-DB telemetry counts. Gracefully degrades if any table is
    missing (e.g. on a freshly-bootstrapped dev DB pre-migration-028)."""
    if conn is None:
        return {"available": False, "reason": "user DB not present"}

    table_counts: Dict[str, Optional[int]] = {}
    for name in USER_DB_TABLES:
        if not _table_exists(conn, name):
            table_counts[name] = None
            continue
        n = int(
            conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
        )
        table_counts[name] = n

    # Also: once the user starts answering questions, the *content* facet
    # cube can drift from the seed (post-Phase-3 correctness fixes will
    # ship retire updates). Stamp the user-DB question counts too so we
    # can diff against the content-DB snapshot above.
    user_q_total: Optional[int] = None
    user_q_live: Optional[int] = None
    if _table_exists(conn, "question"):
        user_q_total = int(
            conn.execute("SELECT COUNT(*) AS n FROM question").fetchone()["n"]
        )
        user_q_live = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM question WHERE status='live'"
            ).fetchone()["n"]
        )

    return {
        "available": True,
        "telemetry_table_counts": table_counts,
        "question_count_total": user_q_total,
        "question_count_live": user_q_live,
    }


# ── The test ────────────────────────────────────────────────────────


def _build_snapshot() -> Dict[str, Any]:
    if not CONTENT_DB_PATH.exists():
        pytest.skip(f"content DB missing at {CONTENT_DB_PATH}")

    content_conn = _connect(CONTENT_DB_PATH)
    user_conn: Optional[sqlite3.Connection] = None
    if USER_DB_PATH.exists():
        user_conn = _connect(USER_DB_PATH)

    try:
        snapshot: Dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "content_db_path": str(CONTENT_DB_PATH.relative_to(PROJECT_ROOT)),
            "user_db_path": (
                str(USER_DB_PATH.relative_to(PROJECT_ROOT))
                if USER_DB_PATH.exists()
                else None
            ),
            "content_db": _snapshot_content_counts(content_conn),
            "di_clusters": _snapshot_di_clusters(content_conn),
            "figure_refs": _snapshot_figure_refs(content_conn),
            "irt_calibration": _snapshot_irt(content_conn),
            "taxonomy": _snapshot_taxonomy(content_conn),
            "user_db": _snapshot_user_db(user_conn),
        }
    finally:
        content_conn.close()
        if user_conn is not None:
            user_conn.close()

    return snapshot


def test_cleanup_baseline_snapshot():
    """Persist a JSON snapshot of dual-DB state and lock in current
    thresholds.

    Threshold philosophy: each ``assert`` here mirrors the **current**
    state with a small tolerance band, so the test passes today and
    catches accidental regressions. When a downstream phase deliberately
    moves a number, the corresponding assertion in this file gets
    updated by that phase's PR (the comment block below tags each
    threshold with the phase that owns its tightening).
    """
    snapshot = _build_snapshot()
    BENCHMARK_FILE.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, default=str)
    )

    # ── Section A: content-DB totals ───────────────────────────────
    # 2026-05-18 baseline: 5251 total, 2599 live, 1856 retired, 761
    # draft, 35 candidate. Phase 3 (correctness) may move retired up
    # by a few hundred; Phase 6 (synthesis) adds candidates and lives.
    content = snapshot["content_db"]
    totals = content["totals"]
    assert totals["all"] >= 5000, f"content_db.totals.all={totals['all']}"
    assert totals["all"] <= 8000, (
        f"content_db.totals.all={totals['all']} too high — Phase 6 "
        "synthesis hasn't shipped yet, expected <=8000"
    )
    assert 2400 <= totals["live"] <= 3500, (
        f"live count out of band: {totals['live']}"
    )
    assert totals["retired"] >= 1500, (
        f"retired={totals['retired']}, expected >=1500"
    )

    # Source coverage — every source we expect to see is present.
    by_source = content["by_source"]
    for required_src in (
        "manhattan_5lb_2018",
        "imported",
        "ai_generated",
        "princeton_2012",
        "ai_synthetic",
        "kaplan_2024",
    ):
        assert required_src in by_source, (
            f"source {required_src!r} missing from snapshot — schema or "
            "data drift"
        )

    # Measure split sanity (no AWA in the question table; AWA lives in
    # AWAPrompt). Verbal+quant should sum to total.
    by_measure = content["by_measure"]
    assert by_measure.get("verbal", 0) > 0
    assert by_measure.get("quant", 0) > 0
    assert (
        by_measure.get("verbal", 0) + by_measure.get("quant", 0) == totals["all"]
    ), "verbal+quant should equal total questions"

    # Subtype coverage — every shipping subtype must appear.
    by_subtype = content["by_subtype"]
    for st in (
        "mcq_single",
        "mcq_multi",
        "qc",
        "tc",
        "se",
        "numeric_entry",
        "rc_single",
        "rc_multi",
        "data_interp",
    ):
        assert by_subtype.get(st, 0) > 0, (
            f"subtype {st!r} missing — section 7 of the plan flags this"
        )

    # Difficulty distribution — every band populated.
    by_diff = content["by_difficulty_target"]
    for band in ("1", "2", "3", "4", "5"):
        assert by_diff.get(band, 0) > 0, f"difficulty_target={band} empty"

    # ── Section B: DI clusters ─────────────────────────────────────
    # 2026-05-18 BASELINE GAP: only 1 graph cluster has ≥2 live siblings.
    # Phase 1 R1 mostly bypasses cluster atomicity by widening the DI
    # selector, but Phase 4 explicitly targets growing the cluster pool.
    # We lock in "no regression below current" — Phase 4 will tighten.
    di = snapshot["di_clusters"]
    assert di["total_clusters"] >= 1, (
        "expected >=1 graph/table cluster with >=2 live children; "
        f"got {di['total_clusters']}"
    )
    assert di["total_graph_table_stim"] >= 200, (
        f"graph+table stim count={di['total_graph_table_stim']}, "
        "expected >=200 (we have ~250 today)"
    )
    # Phase 4 owns this — once cluster rebuilding lands, raise the floor.
    assert di["clusters_with_3plus_live_siblings"] >= 0
    # Sibling-count distribution must be a valid dict of ints.
    assert isinstance(di["sibling_count_distribution"], dict)
    for k, v in di["sibling_count_distribution"].items():
        assert int(k) >= 2, f"distribution key {k!r} should be >=2"
        assert v >= 1

    # ── Section C: figure_refs ─────────────────────────────────────
    # 2026-05-18 BASELINE: 105 figure_refs, all on verbal items with
    # empty subtopic. Phase 5 (image-figure audit) will spread these
    # across quant subtopics too. Lock in "non-zero today".
    figs = snapshot["figure_refs"]
    assert figs["total_with_figure_refs"] >= 50, (
        f"figure_refs total={figs['total_with_figure_refs']}, expected >=50"
    )

    # ── Section D: IRT estimates ───────────────────────────────────
    # 2026-05-18 BASELINE GAP: zero items have IRT estimates yet.
    # Phase 2 (calibration) will push these up. Bound at 0 today.
    irt = snapshot["irt_calibration"]
    assert irt["irt_a_non_null"] >= 0
    assert irt["irt_b_non_null"] >= 0
    assert irt["irt_a_non_null"] <= totals["all"]
    assert irt["irt_b_non_null"] <= totals["all"]

    # ── Section E: per-source taxonomy ─────────────────────────────
    # 2026-05-18 OBSERVATION: manhattan_5lb_2018 and princeton_2012 both
    # ship with topic/subtopic/question_type all collapsed to a single
    # value (1/1/1) — Phase 2 normalizes. Lock in "every required
    # source has at least one taxonomy combo".
    tax = snapshot["taxonomy"]
    assert tax["global"]["n_topics"] >= 10, (
        f"global topics={tax['global']['n_topics']}, expected >=10"
    )
    assert tax["global"]["n_subtopics"] >= 30, (
        f"global subtopics={tax['global']['n_subtopics']}, expected >=30"
    )
    for src in ("manhattan_5lb_2018", "kaplan_2024", "princeton_2012"):
        assert src in tax["per_source"], f"{src} missing from taxonomy section"
        assert tax["per_source"][src]["n_topics"] >= 1
        assert tax["per_source"][src]["n_subtopics"] >= 1

    # ── Section F: user-DB telemetry ───────────────────────────────
    # 2026-05-18: response=48, session=5, servedlog=150, itemrating=2606,
    # itemreview=0, vocabcontextitem=0, sync_state=1.
    # Bound only at "available" since dev users will accrue rows over time.
    user = snapshot["user_db"]
    assert user["available"], "user DB should exist on a normal dev checkout"
    tt = user["telemetry_table_counts"]
    # response / session / servedlog must be integers (table present).
    for required in ("response", "session", "servedlog"):
        assert tt.get(required) is not None, (
            f"required user-DB table {required!r} is absent"
        )
        assert tt[required] >= 0


if __name__ == "__main__":
    # Manual run — `python tests/benchmarks/test_cleanup_baseline.py` —
    # writes the snapshot without invoking pytest. Useful when iterating
    # the snapshot shape mid-investigation.
    snap = _build_snapshot()
    BENCHMARK_FILE.write_text(
        json.dumps(snap, indent=2, sort_keys=True, default=str)
    )
    print(f"Wrote {BENCHMARK_FILE} ({BENCHMARK_FILE.stat().st_size} bytes)")
