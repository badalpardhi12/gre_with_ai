"""
Regression tests for the broader misclassification audit (2026-04-28).

- The specific qid identified from the screenshot ("|0.1x - 3| >= 1") must be
  quant/mcq_multi in both gre_user.db and gre_mock.db.
- No live question should still be classified as measure='awa' with a
  quant-style subtype (mcq_single/mcq_multi/qc/numeric_entry).
- Unit-level: the deterministic pre-filter and classifier-output handling.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

USER_DB = REPO_ROOT / "data" / "gre_user.db"
MOCK_DB = REPO_ROOT / "data" / "gre_mock.db"

SCREENSHOT_QID = 3011


def _lookup(db_path: Path, qid: int):
    if not db_path.exists():
        pytest.skip(f"{db_path} missing — skip DB-dependent assertion")
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT id, measure, subtype, status FROM question WHERE id=?",
            (qid,),
        ).fetchone()
    finally:
        conn.close()
    return row


def test_screenshot_item_is_quant_in_user_db():
    row = _lookup(USER_DB, SCREENSHOT_QID)
    assert row is not None, f"qid {SCREENSHOT_QID} missing from gre_user.db"
    _, measure, subtype, status = row
    if status != "live":
        pytest.skip(f"qid {SCREENSHOT_QID} is {status} in gre_user.db")
    assert measure == "quant", (
        f"qid {SCREENSHOT_QID} still classified as measure={measure!r} in user.db"
    )
    assert subtype == "mcq_multi", (
        f"qid {SCREENSHOT_QID} subtype={subtype!r} expected mcq_multi in user.db"
    )


def test_screenshot_item_is_quant_in_mock_db():
    row = _lookup(MOCK_DB, SCREENSHOT_QID)
    assert row is not None, f"qid {SCREENSHOT_QID} missing from gre_mock.db"
    _, measure, subtype, status = row
    if status != "live":
        pytest.skip(f"qid {SCREENSHOT_QID} is {status} in gre_mock.db")
    assert measure == "quant", (
        f"qid {SCREENSHOT_QID} still classified as measure={measure!r} in mock.db — "
        "the screenshot bug persists for fresh installs"
    )
    assert subtype == "mcq_multi"


@pytest.mark.parametrize("db_path", [USER_DB, MOCK_DB])
def test_no_live_awa_with_quant_subtype(db_path):
    if not db_path.exists():
        pytest.skip(f"{db_path} missing")
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, subtype FROM question "
            "WHERE status='live' AND measure='awa' "
            "AND subtype IN ('mcq_single','mcq_multi','qc','numeric_entry','data_interp')"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [], (
        f"{db_path.name} still has live awa/quant-subtype items: {rows[:5]}"
    )


# ── Unit tests on the audit script's helpers ────────────────────────────────

def test_deterministic_skip_rc_passage():
    from scripts.misclassify_broader_audit import deterministic_skip
    item = {
        "id": 1, "measure": "verbal", "subtype": "rc_single",
        "prompt": "According to the passage...", "stimulus_type": "passage",
        "options": [{}, {}, {}, {}, {}],
    }
    # With sample_rate=0.0 we always skip
    skipped, reason = deterministic_skip(item, sample_rate=0.0)
    assert skipped is True
    assert "rc" in reason


def test_deterministic_skip_qc_markers():
    from scripts.misclassify_broader_audit import deterministic_skip
    item = {
        "id": 2, "measure": "quant", "subtype": "qc",
        "prompt": "Quantity A: x.\nQuantity B: 5.", "stimulus_type": None,
        "options": [],
    }
    skipped, reason = deterministic_skip(item, sample_rate=0.0)
    assert skipped is True
    assert "qc" in reason


def test_deterministic_skip_se_six_options():
    from scripts.misclassify_broader_audit import deterministic_skip
    item = {
        "id": 3, "measure": "verbal", "subtype": "se",
        "prompt": "Choose two words...", "stimulus_type": None,
        "options": [{} for _ in range(6)],
    }
    skipped, reason = deterministic_skip(item, sample_rate=0.0)
    assert skipped is True


def test_deterministic_flags_awa_with_quant_subtype():
    from scripts.misclassify_broader_audit import deterministic_skip
    item = {
        "id": 4, "measure": "awa", "subtype": "mcq_single",
        "prompt": "2+2=?", "stimulus_type": None, "options": [{}] * 5,
    }
    skipped, reason = deterministic_skip(item, sample_rate=0.0)
    assert skipped is False
    assert "awa" in reason


def test_classifier_handles_malformed_json(monkeypatch):
    """If Opus returns garbage twice, classify_with_opus records an error."""
    from scripts.misclassify_broader_audit import classify_with_opus

    class FakeClient:
        def __init__(self):
            self.n = 0
        def call_anthropic(self, **kw):
            self.n += 1
            return "not json"

    item = {
        "id": 1, "measure": "quant", "subtype": "mcq_single", "source": "x",
        "prompt": "foo?", "stimulus_type": None, "stimulus_excerpt": None,
        "options": [{"label": "A", "text": "1", "is_correct": True}],
    }
    out = classify_with_opus(FakeClient(), item)
    assert out is None or out.get("measure") is None


def test_classifier_roundtrips_good_json():
    from scripts.misclassify_broader_audit import classify_with_opus

    class FakeClient:
        def call_anthropic(self, **kw):
            return json.dumps({
                "correct_measure": "quant", "correct_subtype": "mcq_multi",
                "confidence": "high", "reasoning": "multi-select quant",
            })

    item = {
        "id": 1, "measure": "verbal", "subtype": "rc_multi", "source": "x",
        "prompt": "If |0.1x-3|>=1, indicate all that apply.",
        "stimulus_type": None, "stimulus_excerpt": None,
        "options": [{"label": "A", "text": "10", "is_correct": True}],
    }
    out = classify_with_opus(FakeClient(), item)
    assert out["measure"] == "quant"
    assert out["subtype"] == "mcq_multi"
    assert out["confidence"] == "high"


def test_apply_to_both_dbs_preserves_status(tmp_path, monkeypatch):
    """Mock two DBs, update a row, confirm status unchanged and measure flipped."""
    user = tmp_path / "u.db"
    mock_db = tmp_path / "m.db"
    for db in (user, mock_db):
        c = sqlite3.connect(str(db))
        c.executescript("""
        CREATE TABLE question (
            id INTEGER PRIMARY KEY, measure TEXT, subtype TEXT,
            status TEXT, updated_at TEXT
        );
        INSERT INTO question (id, measure, subtype, status, updated_at)
        VALUES (42, 'verbal', 'rc_multi', 'live', '2020-01-01');
        """)
        c.commit()
        c.close()

    from scripts import misclassify_broader_audit as mod
    monkeypatch.setattr(mod, "USER_DB", user)
    monkeypatch.setattr(mod, "MOCK_DB", mock_db)
    updated = mod.apply_to_both_dbs(42, "quant", "mcq_multi")
    assert len(updated) == 2

    for db in (user, mock_db):
        c = sqlite3.connect(str(db))
        row = c.execute("SELECT measure, subtype, status FROM question WHERE id=42").fetchone()
        c.close()
        assert row == ("quant", "mcq_multi", "live"), (
            f"{db.name}: status or fields wrong: {row}"
        )
