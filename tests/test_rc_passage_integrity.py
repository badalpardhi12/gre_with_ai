"""Unit + integration tests for the RC passage integrity auditor."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime

import pytest

# Make the repo root importable.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts.audit_rc_passage_integrity import (  # noqa: E402
    HeuristicResult,
    apply_repair,
    count_paragraphs,
    decide_repair,
    heuristic_audit,
    strip_html,
    strip_trailing_stray_caption,
    SourceIndex,
)


# ──────────────────────────────────────────────────────────────────
# Heuristic unit tests
# ──────────────────────────────────────────────────────────────────


GOOD_SHORT_ARGUMENT = (
    "The school board has responded to the new school lunch guidelines by "
    "replacing fries with fruit in a standard meal option. However, the "
    "guidelines specifically require that vegetables, not fruits, be included "
    "in every meal."
)

GOOD_LONG_PASSAGE = (
    "<p>Scientists have long debated the origin of life on Earth. One "
    "prominent hypothesis holds that life arose in hydrothermal vents at "
    "the bottom of the ocean.</p><p>A second hypothesis, however, posits "
    "that organic molecules first formed in shallow tidal pools exposed to "
    "ultraviolet radiation.</p>"
)

BAD_MID_SENTENCE = (
    "<p>Scientists have long debated the origin of life on Earth. One "
    "prominent hypothesis holds that life arose in hydrothermal vents at "
    "the bottom"
)

BAD_GAP_MARKER = (
    "<p>Ancient astronomers tracked the motion of celestial bodies. [...] "
    "Their observations eventually led to the heliocentric model.</p>"
)

BAD_DATA_FRAGMENT = "Set S {5, 10, 15}"

DECORATIVE_LABEL_TAIL = (
    "<div>Real prose that forms a complete sentence and ends properly.</div>"
    "<div style=\"text-align: center; padding: 8px;\">"
    "<p style=\"text-align:center; font-style:italic; color:#a0a0a0;\">"
    "passage</p></div>"
)

STRAY_CAPTION_TAIL = (
    "<p>The passage ends like this, with a proper terminal sentence.</p>\n"
    "<i>Loi 101</i>"
)


def test_strip_html_drops_decorative_label():
    plain = strip_html(DECORATIVE_LABEL_TAIL)
    assert not plain.endswith("passage")
    assert plain.endswith("properly.")


def test_count_paragraphs_handles_html_and_text():
    assert count_paragraphs(GOOD_LONG_PASSAGE) == 2
    assert count_paragraphs(GOOD_SHORT_ARGUMENT) == 1
    assert count_paragraphs("") == 0
    assert count_paragraphs("para one\n\npara two\n\npara three") == 3


def test_heuristic_good_short_argument_passes():
    r = heuristic_audit(1, GOOD_SHORT_ARGUMENT)
    assert r.suspicion < 2, (r.suspicion, r.notes)
    assert not r.ends_abruptly
    assert not r.too_short


def test_heuristic_good_long_passage_passes():
    r = heuristic_audit(2, GOOD_LONG_PASSAGE)
    assert r.suspicion < 2, (r.suspicion, r.notes)


def test_heuristic_decorative_label_does_not_trip_abrupt_end():
    r = heuristic_audit(3, DECORATIVE_LABEL_TAIL)
    assert not r.ends_abruptly, r.notes


def test_heuristic_mid_sentence_cutoff_flagged():
    r = heuristic_audit(4, BAD_MID_SENTENCE)
    assert r.ends_abruptly
    assert r.suspicion >= 2


def test_heuristic_gap_marker_flagged():
    r = heuristic_audit(5, BAD_GAP_MARKER)
    assert r.gap_marker
    assert r.suspicion >= 3


def test_heuristic_data_fragment_flagged():
    r = heuristic_audit(6, BAD_DATA_FRAGMENT)
    assert r.too_short
    assert r.ends_abruptly
    assert r.suspicion >= 2


def test_heuristic_stray_trailing_caption_flagged():
    r = heuristic_audit(7, STRAY_CAPTION_TAIL)
    # A trailing <i>Loi 101</i> after </p> leaves the tail without a
    # terminal punctuation mark, so the detector flags it.
    assert r.ends_abruptly


def test_heuristic_empty_content_not_exploded():
    r = heuristic_audit(8, "")
    # Empty content is suspicious but the detector must not crash.
    assert isinstance(r, HeuristicResult)


def test_heuristic_starts_lowercase_flagged():
    r = heuristic_audit(
        9, "scientists discovered a new species. It was unusual.")
    assert r.starts_abruptly


def test_heuristic_clamps_suspicion_to_five():
    # Construct a pathological stimulus that trips every signal.
    bad = (
        "orphan start ... and then [omitted] and then "
        "[omitted] more text that cuts off mid"
    )
    r = heuristic_audit(10, bad)
    assert r.suspicion == 5


# ──────────────────────────────────────────────────────────────────
# Stray caption stripper
# ──────────────────────────────────────────────────────────────────


def test_strip_trailing_stray_caption_removes_italic_orphan():
    cleaned, changed = strip_trailing_stray_caption(STRAY_CAPTION_TAIL)
    assert changed
    assert cleaned.endswith("</p>")
    assert "Loi 101" not in cleaned


def test_strip_trailing_stray_caption_preserves_inline_italics():
    # <i> inside a paragraph is NOT an orphan — we must not strip it.
    content = (
        "<p>The Québec Charter of the French Language, known as "
        "<i>Loi 101</i>, was passed in 1977.</p>"
    )
    cleaned, changed = strip_trailing_stray_caption(content)
    assert not changed
    assert cleaned == content


def test_strip_trailing_stray_caption_handles_multiple_orphans():
    content = (
        "<p>Passage body ends here properly.</p>\n"
        "<i>Species A</i>\n<i>Species B</i>"
    )
    cleaned, changed = strip_trailing_stray_caption(content)
    assert changed
    assert "Species" not in cleaned


# ──────────────────────────────────────────────────────────────────
# Routing / integration: decide_repair
# ──────────────────────────────────────────────────────────────────


def test_decide_repair_honours_llm_complete_flag():
    assert decide_repair(
        {"llm_review": {"complete": False,
                        "estimated_missing_content": "significant"}}
    )
    assert decide_repair(
        {"llm_review": {"complete": True,
                        "estimated_missing_content": "small"}}
    )  # "small" alone triggers
    assert not decide_repair(
        {"llm_review": {"complete": True,
                        "estimated_missing_content": "none"}}
    )
    # No LLM review → don't act (heuristic alone is not ground truth).
    assert not decide_repair({})


# ──────────────────────────────────────────────────────────────────
# End-to-end repair/retire on an in-memory DB
# ──────────────────────────────────────────────────────────────────


SCHEMA_SQL = """
CREATE TABLE stimulus (
    id INTEGER PRIMARY KEY,
    stimulus_type VARCHAR(255) NOT NULL,
    title VARCHAR(255) NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    render_spec TEXT NOT NULL DEFAULT '{}',
    created_at DATETIME NOT NULL
);
CREATE TABLE question (
    id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    measure VARCHAR(255) NOT NULL,
    subtype VARCHAR(255) NOT NULL,
    stimulus_id INTEGER,
    prompt TEXT NOT NULL DEFAULT '',
    difficulty_target INTEGER NOT NULL DEFAULT 3,
    time_target_seconds INTEGER NOT NULL DEFAULT 90,
    concept_tags TEXT NOT NULL DEFAULT '',
    provenance VARCHAR(255) NOT NULL DEFAULT '',
    status VARCHAR(255) NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    topic VARCHAR(255),
    subtopic VARCHAR(255) NOT NULL DEFAULT '',
    question_type VARCHAR(255) NOT NULL DEFAULT '',
    source VARCHAR(255) NOT NULL DEFAULT '',
    source_anchor VARCHAR(255) NOT NULL DEFAULT ''
);
"""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_SQL)
    now = datetime.utcnow().isoformat()
    # Two passages — one repairable via stray-caption strip, one irreparable.
    conn.execute(
        "INSERT INTO stimulus (id, stimulus_type, title, content, "
        "render_spec, created_at) VALUES (1, 'passage', '', ?, '{}', ?)",
        (STRAY_CAPTION_TAIL, now),
    )
    conn.execute(
        "INSERT INTO stimulus (id, stimulus_type, title, content, "
        "render_spec, created_at) VALUES (2, 'passage', '', ?, '{}', ?)",
        (BAD_DATA_FRAGMENT, now),
    )
    conn.execute(
        "INSERT INTO question (id, measure, subtype, stimulus_id, status, "
        "source, source_anchor) VALUES (101, 'verbal', 'rc_single', 1, "
        "'live', 'kaplan_2024', 'auto-kaplan-x')"
    )
    conn.execute(
        "INSERT INTO question (id, measure, subtype, stimulus_id, status, "
        "source, source_anchor) VALUES (102, 'verbal', 'rc_multi', 2, "
        "'live', 'manhattan_5lb_2018', '')"
    )
    conn.commit()
    conn.close()
    return str(path)


def test_apply_repair_strips_stray_caption_and_retires_fragment(db):
    audit = [
        {
            "stim_id": 1,
            "suspicion": 2,
            "source_hint": {"source": "kaplan_2024",
                            "anchor": "auto-kaplan-x"},
            "llm_review": {"complete": False,
                           "estimated_missing_content": "small"},
        },
        {
            "stim_id": 2,
            "suspicion": 4,
            "source_hint": {"source": "manhattan_5lb_2018", "anchor": ""},
            "llm_review": {"complete": False,
                           "estimated_missing_content": "significant"},
        },
    ]
    # Empty source index — no external replacements available.
    sources = SourceIndex()
    sources._loaded = True  # skip loading real JSONs

    results = apply_repair(audit, sources=sources, db_paths=[db])
    by_id = {r.stim_id: r for r in results}
    assert by_id[1].action == "repaired"
    assert "stray_caption" in by_id[1].reason
    assert by_id[2].action == "retired"
    assert by_id[2].questions_retired == 1

    conn = sqlite3.connect(db)
    content1 = conn.execute(
        "SELECT content FROM stimulus WHERE id=1"
    ).fetchone()[0]
    assert "Loi 101" not in content1
    spec2 = conn.execute(
        "SELECT render_spec FROM stimulus WHERE id=2"
    ).fetchone()[0]
    assert json.loads(spec2)["retired_reason"] == "incomplete_passage"
    status2 = conn.execute(
        "SELECT status FROM question WHERE id=102"
    ).fetchone()[0]
    assert status2 == "retired"
    status1 = conn.execute(
        "SELECT status FROM question WHERE id=101"
    ).fetchone()[0]
    # The repaired passage's question stays live.
    assert status1 == "live"
    conn.close()


def test_apply_repair_skips_complete_items(db):
    # LLM says complete → no write should occur.
    audit = [
        {
            "stim_id": 1,
            "suspicion": 2,
            "source_hint": {"source": "kaplan_2024",
                            "anchor": "auto-kaplan-x"},
            "llm_review": {"complete": True,
                           "estimated_missing_content": "none"},
        },
    ]
    sources = SourceIndex()
    sources._loaded = True
    results = apply_repair(audit, sources=sources, db_paths=[db])
    assert results == []
    conn = sqlite3.connect(db)
    status = conn.execute(
        "SELECT status FROM question WHERE id=101"
    ).fetchone()[0]
    assert status == "live"
    conn.close()
