"""Tests for the RC cluster integrity remediation script.

Each test builds a minimal in-memory SQLite DB with just the stimulus +
question tables (the two columns we touch), seeds a focused scenario,
runs the target function, and asserts on the outcome.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from scripts.fix_rc_cluster_integrity import (
    CLUSTER_MARKER_RE,
    dedupe_stimuli,
    find_duplicate_stimuli,
    relink_orphans,
    strip_cluster_marker,
)


STIM_DDL = """
CREATE TABLE stimulus (
    id INTEGER PRIMARY KEY,
    stimulus_type VARCHAR(255) NOT NULL,
    title VARCHAR(255) NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    render_spec TEXT NOT NULL DEFAULT '{}',
    created_at DATETIME NOT NULL
)
"""

QUEST_DDL = """
CREATE TABLE question (
    id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    measure VARCHAR(255) NOT NULL,
    subtype VARCHAR(255) NOT NULL,
    stimulus_id INTEGER,
    prompt TEXT NOT NULL,
    difficulty_target INTEGER NOT NULL DEFAULT 3,
    time_target_seconds INTEGER NOT NULL DEFAULT 60,
    concept_tags TEXT NOT NULL DEFAULT '[]',
    provenance VARCHAR(255) NOT NULL DEFAULT '',
    status VARCHAR(255) NOT NULL DEFAULT 'live',
    explanation TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    topic VARCHAR(255),
    subtopic VARCHAR(255) NOT NULL DEFAULT '',
    question_type VARCHAR(255) NOT NULL DEFAULT '',
    source VARCHAR(255) NOT NULL DEFAULT '',
    quality_score REAL,
    mastery_difficulty REAL,
    FOREIGN KEY (stimulus_id) REFERENCES stimulus(id)
)
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute(STIM_DDL)
    c.execute(QUEST_DDL)
    return c


def _add_stim(conn, sid: int, content: str, stype: str = "passage") -> None:
    conn.execute(
        "INSERT INTO stimulus(id, stimulus_type, title, content, render_spec, created_at) VALUES(?, ?, '', ?, '{}', ?)",
        (sid, stype, content, datetime.utcnow().isoformat()),
    )


def _add_q(
    conn,
    qid: int,
    stim_id,
    prompt: str = "q?",
    subtype: str = "rc_single",
    status: str = "live",
    measure: str = "verbal",
) -> None:
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO question(id, measure, subtype, stimulus_id, prompt, created_at, updated_at, status) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (qid, measure, subtype, stim_id, prompt, now, now, status),
    )


# ---------------------------------------------------------------------------
# Case A — dedupe_stimuli
# ---------------------------------------------------------------------------


class TestDedupeStimuli:
    def test_no_duplicates_is_noop(self, conn):
        _add_stim(conn, 1, "Unique passage A")
        _add_stim(conn, 2, "Unique passage B")
        _add_q(conn, 10, 1)
        _add_q(conn, 11, 2)

        report = dedupe_stimuli(conn)
        assert report.groups_merged == 0
        assert report.stimuli_deleted == 0
        assert report.questions_relinked == 0

    def test_merges_onto_oldest_id(self, conn):
        passage = "Identical passage text."
        _add_stim(conn, 5, passage)
        _add_stim(conn, 7, passage)
        _add_stim(conn, 9, passage)
        _add_q(conn, 100, 5)
        _add_q(conn, 101, 7)
        _add_q(conn, 102, 9)
        _add_q(conn, 103, 9)

        report = dedupe_stimuli(conn)
        assert report.groups_merged == 1
        assert report.stimuli_deleted == 2
        assert report.questions_relinked == 3  # three Qs moved off 7 and 9

        remaining_stim = conn.execute("SELECT id FROM stimulus").fetchall()
        assert {r[0] for r in remaining_stim} == {5}
        links = conn.execute("SELECT id, stimulus_id FROM question ORDER BY id").fetchall()
        assert all(s == 5 for _, s in links)

    def test_dry_run_leaves_db_unchanged(self, conn):
        _add_stim(conn, 1, "same")
        _add_stim(conn, 2, "same")
        _add_q(conn, 11, 2)
        report = dedupe_stimuli(conn, dry_run=True)
        assert report.groups_merged == 1
        assert report.questions_relinked == 1
        # But nothing actually moved
        stim_ids = {r[0] for r in conn.execute("SELECT id FROM stimulus")}
        assert stim_ids == {1, 2}
        link = conn.execute("SELECT stimulus_id FROM question WHERE id=11").fetchone()[0]
        assert link == 2

    def test_find_duplicate_stimuli_sorts_oldest_first(self, conn):
        _add_stim(conn, 99, "x")
        _add_stim(conn, 33, "x")
        _add_stim(conn, 77, "x")
        groups = find_duplicate_stimuli(conn)
        assert groups == [[33, 77, 99]]


# ---------------------------------------------------------------------------
# Case B — relink_orphans (classification only, no false positives)
# ---------------------------------------------------------------------------


class TestRelinkOrphans:
    def test_quant_mis_tagged_as_rc_is_classified_misclassified(self, conn):
        _add_q(conn, 1, None, prompt=r"If \(x^2 = y^2\), which of the following must be true?", subtype="rc_multi")
        report = relink_orphans(conn)
        assert report.candidates_examined == 1
        assert report.misclassified == 1
        assert report.relinked == 0
        assert report.left_as_orphan == 0

    def test_sentence_completion_mis_tagged_as_rc_single_is_classified_misclassified(self, conn):
        _add_q(conn, 1, None, prompt="Mary was _________ at the meeting.", subtype="rc_single")
        report = relink_orphans(conn)
        assert report.misclassified == 1
        assert report.relinked == 0

    def test_genuine_rc_orphan_without_match_is_preserved(self, conn):
        _add_q(
            conn, 1, None,
            prompt="Based on the passage, the author most likely means which of the following?",
            subtype="rc_single",
        )
        report = relink_orphans(conn)
        # "looks_rc" is true — no deterministic target → leave as orphan, don't invent.
        assert report.left_as_orphan == 1
        assert report.relinked == 0

    def test_draft_orphans_are_ignored(self, conn):
        _add_q(conn, 1, None, prompt="passage refers to the author", subtype="rc_single", status="draft")
        report = relink_orphans(conn)
        assert report.candidates_examined == 0


# ---------------------------------------------------------------------------
# Case C — strip_cluster_marker
# ---------------------------------------------------------------------------


class TestStripClusterMarker:
    def test_intact_cluster_is_preserved(self, conn):
        content = "<b>Questions 8-10 are based on the passage below.</b>\nThe passage body..."
        _add_stim(conn, 1, content)
        _add_q(conn, 10, 1)
        _add_q(conn, 11, 1)
        _add_q(conn, 12, 1)
        report = strip_cluster_marker(conn)
        assert report.stripped == 0
        assert report.preserved == 1
        saved = conn.execute("SELECT content FROM stimulus WHERE id=1").fetchone()[0]
        assert saved == content

    def test_solo_live_item_with_3way_marker_is_stripped(self, conn):
        content = "<b>Questions 8-10 are based on the passage below.</b>\nThe passage body..."
        _add_stim(conn, 1, content)
        _add_q(conn, 10, 1, status="live")
        _add_q(conn, 11, 1, status="draft")
        _add_q(conn, 12, 1, status="draft")
        report = strip_cluster_marker(conn)
        assert report.stripped == 1
        new_content = conn.execute("SELECT content FROM stimulus WHERE id=1").fetchone()[0]
        assert "Questions" not in new_content.split("\n")[0]
        assert "The passage body" in new_content

    def test_em_dash_and_and_variants_both_match(self, conn):
        for body, sid in [
            ("<b>Questions 1–2 are based on the passage below.</b>\nText A.", 1),
            ("<b>Questions 1 and 2 are based on the passage below.</b>\nText B.", 2),
            ("Questions 15 and 16 are based on the following passage.\nText C.", 3),
        ]:
            _add_stim(conn, sid, body)
            _add_q(conn, sid * 10, sid)
        report = strip_cluster_marker(conn)
        assert report.stripped == 3

    def test_no_marker_is_noop(self, conn):
        _add_stim(conn, 1, "An ordinary passage with no cluster header.")
        _add_q(conn, 10, 1)
        report = strip_cluster_marker(conn)
        assert report.stripped == 0

    def test_regex_matches_various_forms(self):
        samples = [
            "<b>Questions 1 and 2 are based on the passage below.</b>\n",
            "Questions 4–6 are based on the passage below.",
            "Questions 7-10 are based on the passage below.",
            "Question 1 is based on the passage below.",
            "Questions 15 and 16 are based on the following passage.",
        ]
        for s in samples:
            m = CLUSTER_MARKER_RE.search(s)
            assert m is not None, f"Did not match: {s!r}"


# ---------------------------------------------------------------------------
# Integration: duplicates + orphans + solo-marker on one fixture DB
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_full_pipeline_on_mixed_fixture(self, conn):
        # Two duplicate-content stimuli that share a Q.
        _add_stim(conn, 1, "Dup passage A")
        _add_stim(conn, 2, "Dup passage A")
        _add_q(conn, 100, 1)
        _add_q(conn, 101, 2)

        # A passage with a solo live question but a "3-question" cluster header.
        _add_stim(
            conn, 3,
            "<b>Questions 8-10 are based on the passage below.</b>\nThe passage body.",
        )
        _add_q(conn, 200, 3, status="live")
        _add_q(conn, 201, 3, status="draft")
        _add_q(conn, 202, 3, status="draft")

        # An orphan RC that can't be matched deterministically.
        _add_q(
            conn, 300, None,
            prompt="According to the passage, the author is most concerned with which of the following?",
            subtype="rc_single",
        )

        dedupe = dedupe_stimuli(conn)
        assert dedupe.stimuli_deleted == 1
        assert dedupe.questions_relinked == 1

        orphans = relink_orphans(conn)
        assert orphans.left_as_orphan == 1
        assert orphans.relinked == 0

        strip = strip_cluster_marker(conn)
        assert strip.stripped == 1

        # End state: stim 2 gone, stim 3 marker gone, orphan 300 still orphan.
        stim_ids = {r[0] for r in conn.execute("SELECT id FROM stimulus")}
        assert stim_ids == {1, 3}

        stim3_content = conn.execute("SELECT content FROM stimulus WHERE id=3").fetchone()[0]
        assert "Questions" not in stim3_content.split("\n")[0]

        orphan_link = conn.execute("SELECT stimulus_id FROM question WHERE id=300").fetchone()[0]
        assert orphan_link is None
