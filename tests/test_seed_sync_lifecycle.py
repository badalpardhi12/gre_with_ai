"""
Full-lifecycle tests for ``services/seed_sync.py``.

These complement ``tests/test_seed_sync.py`` (which pins the
table-by-table reconcile contract) by exercising the end-to-end data
pipeline that runs on every launch:

  FRESH INSTALL  — no user DB → bootstrap copy → reconcile yields a user
                   DB whose reference content matches the seed.
  UPDATE         — an old user DB carrying user-state + a newer seed →
                   migrations + reconcile land the new content while
                   preserving user state.
  IDEMPOTENCE    — a second reconcile against an unchanged seed is a
                   pure metadata no-op.
  ATOMICITY      — a forced mid-reconcile failure rolls back cleanly.
  CHANGE DETECT  — the same-size/same-mtime content edit (the bug the
                   content signature fixes) now propagates.

Everything is isolated in tmp dirs over small synthetic SQLite DBs,
except ``test_real_seed_reconcile_is_readonly_and_covers_tables`` which
reconciles a *copy* of the real ``data/gre_mock.db`` into a fresh user
DB (read-only against the shipped seed).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest


# ── Synthetic schema ────────────────────────────────────────────────────
# Mirrors the shipped schema closely enough to exercise every reconcile
# code path: question (per-user cols), the wipe-and-replace reference
# tables, the UPSERT-by-pk reference tables (with their inbound user-state
# FKs), and a couple of user-state tables that must never be touched.

_SCHEMA = {
    "question": """
        CREATE TABLE question (
          id INTEGER PRIMARY KEY,
          measure TEXT NOT NULL DEFAULT 'quant',
          subtype TEXT NOT NULL DEFAULT 'mcq_single',
          stimulus_id INTEGER,
          prompt TEXT NOT NULL,
          explanation TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'live',
          provenance_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL DEFAULT '2026-01-01 00:00:00',
          pretest_n_responses INTEGER NOT NULL DEFAULT 0,
          pretest_p_correct REAL,
          irt_b_estimate REAL
        )
    """,
    "questionoption": """
        CREATE TABLE questionoption (
          id INTEGER PRIMARY KEY,
          question_id INTEGER NOT NULL,
          option_label TEXT NOT NULL,
          option_text TEXT NOT NULL,
          is_correct INTEGER NOT NULL
        )
    """,
    "numericanswer": """
        CREATE TABLE numericanswer (
          id INTEGER PRIMARY KEY,
          question_id INTEGER NOT NULL,
          exact_value REAL,
          tolerance REAL NOT NULL DEFAULT 0
        )
    """,
    "stimulus": """
        CREATE TABLE stimulus (
          id INTEGER PRIMARY KEY,
          stimulus_type TEXT NOT NULL DEFAULT 'graph',
          title TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL
        )
    """,
    "lesson": """
        CREATE TABLE lesson (
          id INTEGER PRIMARY KEY,
          subtopic TEXT NOT NULL,
          measure TEXT NOT NULL DEFAULT 'quant',
          title TEXT NOT NULL,
          body_html TEXT NOT NULL DEFAULT ''
        )
    """,
    "vocabroot": """
        CREATE TABLE vocabroot (
          id INTEGER PRIMARY KEY,
          root TEXT NOT NULL,
          language TEXT NOT NULL DEFAULT 'latin',
          meaning TEXT NOT NULL
        )
    """,
    "awaprompt": """
        CREATE TABLE awaprompt (
          id INTEGER PRIMARY KEY,
          prompt_text TEXT NOT NULL,
          instructions TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL DEFAULT 'ets'
        )
    """,
    "vocabword": """
        CREATE TABLE vocabword (
          id INTEGER PRIMARY KEY,
          word TEXT NOT NULL UNIQUE,
          definition TEXT NOT NULL DEFAULT '',
          difficulty INTEGER NOT NULL DEFAULT 3
        )
    """,
    # ── user-state tables (must never be wiped/clobbered) ──
    "response": """
        CREATE TABLE response (
          id INTEGER PRIMARY KEY,
          question_id INTEGER NOT NULL,
          is_correct INTEGER NOT NULL,
          created_at TEXT NOT NULL DEFAULT '2026-01-01 00:00:00'
        )
    """,
    "session": """
        CREATE TABLE session (
          id INTEGER PRIMARY KEY,
          test_type TEXT NOT NULL DEFAULT 'full_mock',
          state TEXT NOT NULL DEFAULT 'completed'
        )
    """,
    # awasubmission carries an inbound FK to awaprompt — its existence is
    # WHY awaprompt is UPSERTed (not wiped). We don't declare an enforced
    # FK (the raw reconcile runs with foreign_keys off) but we assert the
    # referenced prompt id survives.
    "awasubmission": """
        CREATE TABLE awasubmission (
          id INTEGER PRIMARY KEY,
          prompt_id INTEGER NOT NULL,
          essay_text TEXT NOT NULL DEFAULT ''
        )
    """,
    # flashcardreview carries an inbound FK to vocabword — WHY vocabword
    # is UPSERTed (not wiped).
    "flashcardreview": """
        CREATE TABLE flashcardreview (
          id INTEGER PRIMARY KEY,
          word_id INTEGER NOT NULL,
          review_count INTEGER NOT NULL DEFAULT 0
        )
    """,
}


def _make_db(path: Path, extra_question_cols=None) -> None:
    conn = sqlite3.connect(str(path))
    schema = dict(_SCHEMA)
    if extra_question_cols:
        # Inject extra column declarations into the question DDL to model
        # a seed/user schema drift.
        ddl = schema["question"].rstrip().rstrip(")")
        ddl += ",\n" + ",\n".join(extra_question_cols) + "\n)"
        schema["question"] = ddl
    for sql in schema.values():
        conn.executescript(sql)
    conn.commit()
    conn.close()


def _seed_baseline(path: Path) -> None:
    """Populate a DB with a baseline set of reference rows."""
    c = sqlite3.connect(str(path))
    c.execute("INSERT INTO stimulus (id, content) VALUES (1, 'passage v1')")
    c.execute("INSERT INTO question (id, prompt, explanation, stimulus_id) "
              "VALUES (10, 'Q10 prompt', 'Q10 expl', 1)")
    c.execute("INSERT INTO question (id, prompt, explanation) "
              "VALUES (11, 'Q11 prompt', 'Q11 expl')")
    for lab, txt, ok in [("A", "right", 1), ("B", "wrong", 0)]:
        c.execute("INSERT INTO questionoption (question_id, option_label, "
                  "option_text, is_correct) VALUES (11, ?, ?, ?)", (lab, txt, ok))
    c.execute("INSERT INTO numericanswer (question_id, exact_value) VALUES (10, 42.0)")
    c.execute("INSERT INTO lesson (id, subtopic, title, body_html) "
              "VALUES (1, 'algebra', 'Algebra', 'lesson v1')")
    c.execute("INSERT INTO vocabroot (id, root, meaning) VALUES (1, 'bene', 'good')")
    c.execute("INSERT INTO awaprompt (id, prompt_text) VALUES (1, 'AWA prompt v1')")
    c.execute("INSERT INTO vocabword (id, word, definition, difficulty) "
              "VALUES (1, 'ephemeral', 'short-lived', 3)")
    c.commit()
    c.close()


def _read(path, sql, params=()):
    c = sqlite3.connect(str(path))
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def _one(path, sql, params=()):
    rows = _read(path, sql, params)
    return rows[0][0] if rows else None


# ─────────────────────────────────────────────────────────────────────────
# FRESH INSTALL
# ─────────────────────────────────────────────────────────────────────────

def test_fresh_install_bootstrap_then_reconcile(tmp_path, monkeypatch):
    """No user DB → bootstrap copies seed → reconcile leaves reference
    content matching the seed (and a stored content signature)."""
    seed = tmp_path / "gre_mock.db"
    user = tmp_path / "gre_user.db"
    _make_db(seed)
    _seed_baseline(seed)

    # Point config at our tmp paths and run the real bootstrap.
    monkeypatch.setattr("config.SEED_DB_PATH", seed)
    monkeypatch.setattr("config.DB_PATH", user)
    import config
    assert not user.exists()
    config._bootstrap_user_db()
    assert user.exists(), "bootstrap should have copied the seed"

    # Reconcile (the init_db tail). Should be a no-op-equivalent here
    # since the copy is byte-identical, but it must store a signature so
    # the next launch fast-skips.
    from services.seed_sync import reconcile_if_stale, _get_last_sync_sig
    reconcile_if_stale(seed, user)

    # Reference content present and matches the seed.
    assert _one(user, "SELECT prompt FROM question WHERE id=10") == "Q10 prompt"
    assert _one(user, "SELECT content FROM stimulus WHERE id=1") == "passage v1"
    assert _one(user, "SELECT body_html FROM lesson WHERE id=1") == "lesson v1"
    assert _one(user, "SELECT prompt_text FROM awaprompt WHERE id=1") == "AWA prompt v1"
    assert _one(user, "SELECT definition FROM vocabword WHERE id=1") == "short-lived"
    assert _one(user, "SELECT COUNT(*) FROM questionoption WHERE question_id=11") == 2

    # Signature stored and current-format.
    c = sqlite3.connect(str(user))
    try:
        sig = _get_last_sync_sig(c)
    finally:
        c.close()
    assert sig and sig.startswith("sha256:")


# ─────────────────────────────────────────────────────────────────────────
# UPDATE: old user DB + newer seed
# ─────────────────────────────────────────────────────────────────────────

def test_update_propagates_content_and_preserves_user_state(tmp_path):
    """A newer seed (rewritten prompt, repaired options, new qid, changed
    AWA prompt, new vocab) reconciled onto an old user DB lands the new
    content AND preserves user-state rows + per-user question columns +
    migration-owned status."""
    seed = tmp_path / "gre_mock.db"
    user = tmp_path / "gre_user.db"
    _make_db(seed)
    _make_db(user)

    # Seed baseline → copy to user → then diverge them.
    _seed_baseline(seed)
    _seed_baseline(user)

    # --- user accumulates state on their side ---
    cu = sqlite3.connect(str(user))
    # per-user question columns
    cu.execute("UPDATE question SET pretest_n_responses=7, pretest_p_correct=0.55, "
               "irt_b_estimate=1.4, created_at='2020-01-01 00:00:00' WHERE id=10")
    # a response + session (pure user-state)
    cu.execute("INSERT INTO response (question_id, is_correct) VALUES (10, 1)")
    cu.execute("INSERT INTO session (id, test_type) VALUES (1, 'full_mock')")
    # an AWA submission referencing awaprompt 1, and an SRS review referencing
    # vocabword 1 — these inbound FKs are why those tables are UPSERTed.
    cu.execute("INSERT INTO awasubmission (prompt_id, essay_text) VALUES (1, 'my essay')")
    cu.execute("INSERT INTO flashcardreview (word_id, review_count) VALUES (1, 9)")
    # simulate a migration-owned retirement on the user DB (status is user-owned)
    cu.execute("UPDATE question SET status='retired' WHERE id=11")
    cu.commit()
    cu.close()

    # --- a migration also ran on the seed at build time, but the tracked
    # seed status LAGS (live) — reconcile must NOT flip the user's
    # retired row back to live. Mirror the documented contract: seed has
    # status='live' for qid 11. ---

    # --- author edits the seed (the git-pull scenario) ---
    cs = sqlite3.connect(str(seed))
    cs.execute("UPDATE question SET prompt='Q10 REWRITTEN', explanation='new expl' WHERE id=10")
    # repaired option set for q11: now A/B/C with C correct
    cs.execute("DELETE FROM questionoption WHERE question_id=11")
    for lab, txt, ok in [("A", "n1", 0), ("B", "n2", 0), ("C", "n3", 1)]:
        cs.execute("INSERT INTO questionoption (question_id, option_label, "
                   "option_text, is_correct) VALUES (11, ?, ?, ?)", (lab, txt, ok))
    # a brand-new question
    cs.execute("INSERT INTO question (id, prompt, explanation) VALUES (12, 'BRAND NEW Q', 'e')")
    # changed stimulus + lesson content
    cs.execute("UPDATE stimulus SET content='passage v2' WHERE id=1")
    cs.execute("UPDATE lesson SET body_html='lesson v2' WHERE id=1")
    # changed AWA prompt text (UPSERT must update it without breaking the FK)
    cs.execute("UPDATE awaprompt SET prompt_text='AWA prompt v2' WHERE id=1")
    # new vocab word (UPSERT insert)
    cs.execute("INSERT INTO vocabword (id, word, definition) VALUES (2, 'laconic', 'terse')")
    # seed's per-user columns differ (defaults) — must NOT clobber user's
    cs.execute("UPDATE question SET pretest_n_responses=0, pretest_p_correct=NULL, "
               "irt_b_estimate=NULL, created_at='2026-06-01 00:00:00' WHERE id=10")
    cs.commit()
    cs.close()

    # Force a re-sync (mtime bump so the signature differs even before hashing)
    os.utime(seed, (time.time() + 100, time.time() + 100))

    from services.seed_sync import reconcile_if_stale
    stats = reconcile_if_stale(seed, user)
    assert "question_updated" in stats, stats

    # --- content propagated ---
    assert _one(user, "SELECT prompt FROM question WHERE id=10") == "Q10 REWRITTEN"
    assert _one(user, "SELECT explanation FROM question WHERE id=10") == "new expl"
    assert _one(user, "SELECT prompt FROM question WHERE id=12") == "BRAND NEW Q"  # new qid
    opts = sorted(_read(user, "SELECT option_label, option_text, is_correct "
                              "FROM questionoption WHERE question_id=11"))
    assert opts == [("A", "n1", 0), ("B", "n2", 0), ("C", "n3", 1)]  # repaired options
    assert _one(user, "SELECT content FROM stimulus WHERE id=1") == "passage v2"
    assert _one(user, "SELECT body_html FROM lesson WHERE id=1") == "lesson v2"
    assert _one(user, "SELECT prompt_text FROM awaprompt WHERE id=1") == "AWA prompt v2"
    assert _one(user, "SELECT definition FROM vocabword WHERE id=2") == "terse"  # new vocab

    # --- migration-owned retirement preserved (NOT flipped back to live) ---
    assert _one(user, "SELECT status FROM question WHERE id=11") == "retired"

    # --- per-user question columns preserved ---
    row = _read(user, "SELECT pretest_n_responses, pretest_p_correct, irt_b_estimate, "
                      "created_at FROM question WHERE id=10")[0]
    assert row == (7, 0.55, 1.4, "2020-01-01 00:00:00")

    # --- user-state rows untouched ---
    assert _one(user, "SELECT COUNT(*) FROM response WHERE question_id=10") == 1
    assert _one(user, "SELECT COUNT(*) FROM session") == 1
    # the inbound FKs still resolve (UPSERT preserved pk 1 on both)
    assert _one(user, "SELECT essay_text FROM awasubmission WHERE prompt_id=1") == "my essay"
    assert _one(user, "SELECT review_count FROM flashcardreview WHERE word_id=1") == 9
    assert _one(user, "SELECT COUNT(*) FROM awaprompt WHERE id=1") == 1  # not orphaned
    assert _one(user, "SELECT COUNT(*) FROM vocabword WHERE id=1") == 1  # not orphaned


# ─────────────────────────────────────────────────────────────────────────
# IDEMPOTENCE
# ─────────────────────────────────────────────────────────────────────────

def test_idempotent_second_reconcile_is_skip(tmp_path):
    seed = tmp_path / "gre_mock.db"
    user = tmp_path / "gre_user.db"
    _make_db(seed); _make_db(user)
    _seed_baseline(seed); _seed_baseline(user)

    from services.seed_sync import reconcile_if_stale
    first = reconcile_if_stale(seed, user)
    assert "question_updated" in first
    second = reconcile_if_stale(seed, user)
    assert second == {"skipped": "fingerprint_match"}

    # And nothing changed on a third call either.
    before = _read(user, "SELECT id, prompt FROM question ORDER BY id")
    reconcile_if_stale(seed, user)
    after = _read(user, "SELECT id, prompt FROM question ORDER BY id")
    assert before == after


# ─────────────────────────────────────────────────────────────────────────
# ATOMICITY
# ─────────────────────────────────────────────────────────────────────────

def test_atomic_rollback_on_midreconcile_failure(tmp_path, monkeypatch):
    seed = tmp_path / "gre_mock.db"
    user = tmp_path / "gre_user.db"
    _make_db(seed); _make_db(user)
    _seed_baseline(seed); _seed_baseline(user)

    # Diverge the seed so a reconcile would normally change the user DB.
    cs = sqlite3.connect(str(seed))
    cs.execute("UPDATE question SET prompt='SHOULD NOT LAND' WHERE id=10")
    cs.commit(); cs.close()
    os.utime(seed, (time.time() + 100, time.time() + 100))

    import services.seed_sync as mod
    orig = mod._upsert_table_by_pk
    def boom(seed_conn, user_conn, table, pk="id"):
        if table == "vocabword":
            raise RuntimeError("simulated crash mid-upsert")
        return orig(seed_conn, user_conn, table, pk)
    monkeypatch.setattr(mod, "_upsert_table_by_pk", boom)

    with pytest.raises(RuntimeError):
        mod.reconcile_if_stale(seed, user)

    # Everything rolled back: the question UPDATE that ran before the
    # crash must NOT have committed.
    assert _one(user, "SELECT prompt FROM question WHERE id=10") == "Q10 prompt"
    # And the signature was NOT advanced, so the next launch retries.
    from services.seed_sync import _get_last_sync_sig
    c = sqlite3.connect(str(user))
    try:
        sig = _get_last_sync_sig(c)
    finally:
        c.close()
    assert sig is None or not _sig_matches(sig, seed)


def _sig_matches(sig, seed):
    from services.seed_sync import _signatures_match
    return _signatures_match(sig, seed)


# ─────────────────────────────────────────────────────────────────────────
# CHANGE DETECTION: the same-size / same-mtime bug is fixed
# ─────────────────────────────────────────────────────────────────────────

def test_same_size_same_mtime_content_change_propagates(tmp_path):
    """The reproduced bug: a content edit that preserves both byte-size
    and mtime used to false-match the (mtime, size) fingerprint and skip
    the reconcile. The sha256 signature must catch it."""
    seed = tmp_path / "gre_mock.db"
    user = tmp_path / "gre_user.db"
    _make_db(seed); _make_db(user)
    _seed_baseline(seed); _seed_baseline(user)

    from services.seed_sync import reconcile_if_stale
    reconcile_if_stale(seed, user)  # establish signature

    mtime_before = os.stat(seed).st_mtime_ns
    size_before = os.stat(seed).st_size

    # Same-length in-place edit (10 chars → 10 chars), reset mtime.
    cs = sqlite3.connect(str(seed))
    cs.execute("UPDATE question SET prompt='XXXXXXXXXX' WHERE id=10")
    cs.commit(); cs.close()
    os.utime(seed, ns=(mtime_before, mtime_before))

    assert os.stat(seed).st_size == size_before, "precondition: size unchanged"
    assert os.stat(seed).st_mtime_ns == mtime_before, "precondition: mtime unchanged"

    stats = reconcile_if_stale(seed, user)
    assert "question_updated" in stats, (
        "content change with same size+mtime must NOT be skipped: %s" % stats)
    assert _one(user, "SELECT prompt FROM question WHERE id=10") == "XXXXXXXXXX"


def test_legacy_fingerprint_value_forces_one_reconcile(tmp_path):
    """A stale ``mtime:size`` value (the old format) must force exactly
    one reconcile rather than crashing or matching."""
    seed = tmp_path / "gre_mock.db"
    user = tmp_path / "gre_user.db"
    _make_db(seed); _make_db(user)
    _seed_baseline(seed); _seed_baseline(user)

    # Plant a legacy-format value directly.
    c = sqlite3.connect(str(user))
    c.execute("CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO sync_state (key, value) VALUES ('seed_fingerprint', '123456789:24576')")
    c.commit(); c.close()

    # Diverge the seed content so we can prove the reconcile actually ran.
    cs = sqlite3.connect(str(seed))
    cs.execute("UPDATE question SET prompt='AFTER LEGACY MIGRATION' WHERE id=10")
    cs.commit(); cs.close()

    from services.seed_sync import reconcile_if_stale
    stats = reconcile_if_stale(seed, user)
    assert "question_updated" in stats
    assert _one(user, "SELECT prompt FROM question WHERE id=10") == "AFTER LEGACY MIGRATION"

    # Now the signature is current-format → next call skips.
    assert reconcile_if_stale(seed, user) == {"skipped": "fingerprint_match"}


# ─────────────────────────────────────────────────────────────────────────
# SCHEMA-DRIFT TOLERANCE
# ─────────────────────────────────────────────────────────────────────────

def test_seed_has_extra_column_user_lacks(tmp_path):
    """Seed ships a question column the user DB hasn't migrated to. The
    reconcile must succeed and sync the common columns instead of
    aborting with 'no such column'."""
    seed = tmp_path / "gre_mock.db"
    user = tmp_path / "gre_user.db"
    # Seed has an extra column; user does not.
    _make_db(seed, extra_question_cols=["brand_new_col TEXT NOT NULL DEFAULT 'x'"])
    _make_db(user)
    _seed_baseline(seed); _seed_baseline(user)

    cs = sqlite3.connect(str(seed))
    cs.execute("UPDATE question SET prompt='drifted prompt', brand_new_col='ignored' WHERE id=10")
    cs.commit(); cs.close()
    os.utime(seed, (time.time() + 100, time.time() + 100))

    from services.seed_sync import reconcile_if_stale
    stats = reconcile_if_stale(seed, user)
    assert "question_updated" in stats
    # Common column synced; the seed-only column simply wasn't applied.
    assert _one(user, "SELECT prompt FROM question WHERE id=10") == "drifted prompt"
    user_cols = [r[1] for r in _read(user, "PRAGMA table_info(question)")]
    assert "brand_new_col" not in user_cols


def test_user_has_extra_column_seed_lacks(tmp_path):
    """User DB has a question column the seed doesn't. Reconcile must
    leave that user-only column untouched and still sync shared columns."""
    seed = tmp_path / "gre_mock.db"
    user = tmp_path / "gre_user.db"
    _make_db(seed)
    _make_db(user, extra_question_cols=["user_only_col TEXT NOT NULL DEFAULT 'keepme'"])
    _seed_baseline(seed); _seed_baseline(user)

    cs = sqlite3.connect(str(seed))
    cs.execute("UPDATE question SET prompt='shared edit' WHERE id=10")
    cs.commit(); cs.close()
    os.utime(seed, (time.time() + 100, time.time() + 100))

    from services.seed_sync import reconcile_if_stale
    reconcile_if_stale(seed, user)
    assert _one(user, "SELECT prompt FROM question WHERE id=10") == "shared edit"
    assert _one(user, "SELECT user_only_col FROM question WHERE id=10") == "keepme"


# ─────────────────────────────────────────────────────────────────────────
# REAL SEED (read-only)
# ─────────────────────────────────────────────────────────────────────────

def test_real_seed_reconcile_is_readonly_and_covers_tables(tmp_path):
    """Reconcile a COPY of the shipped seed into a fresh user DB. The real
    seed file must be untouched, and every covered reference table must
    end up populated on the user side."""
    real_seed = Path(__file__).resolve().parent.parent / "data" / "gre_mock.db"
    if not real_seed.exists():
        pytest.skip("shipped seed not present")

    seed_copy = tmp_path / "gre_mock.db"
    user = tmp_path / "gre_user.db"
    shutil.copy2(str(real_seed), str(seed_copy))
    shutil.copy2(str(real_seed), str(user))  # simulate fresh-install copy

    real_mtime = os.stat(real_seed).st_mtime_ns
    real_size = os.stat(real_seed).st_size

    from services.seed_sync import reconcile_if_stale, reconcile_reference_data_from_seed
    # Force a full reconcile against the copy regardless of signature.
    stats = reconcile_reference_data_from_seed(seed_copy, user)

    # The REAL shipped seed must be byte-for-byte and metadata-unchanged.
    assert os.stat(real_seed).st_mtime_ns == real_mtime
    assert os.stat(real_seed).st_size == real_size

    # Every covered reference table populated on the user side, and the
    # counts match the seed copy (reference content faithfully mirrored).
    for table in ("question", "questionoption", "numericanswer", "stimulus",
                  "lesson", "vocabroot", "awaprompt", "vocabword"):
        seed_n = _one(seed_copy, "SELECT COUNT(*) FROM %s" % table)
        user_n = _one(user, "SELECT COUNT(*) FROM %s" % table)
        assert user_n == seed_n and user_n > 0, (table, seed_n, user_n)

    # Spot-check a few qids/options round-trip identically.
    sample_qids = [r[0] for r in _read(seed_copy,
                   "SELECT id FROM question ORDER BY id LIMIT 5")]
    for qid in sample_qids:
        sp = _one(seed_copy, "SELECT prompt FROM question WHERE id=?", (qid,))
        up = _one(user, "SELECT prompt FROM question WHERE id=?", (qid,))
        assert sp == up, qid

    # Idempotence on the real-shaped data: signature stored → skip.
    assert reconcile_if_stale(seed_copy, user) == {"skipped": "fingerprint_match"}
