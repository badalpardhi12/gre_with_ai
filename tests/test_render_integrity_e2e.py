"""End-to-end render-integrity checks against the actual data/gre_user.db.

These tests exist because three rendering fixes landed on main but the user's
running app still showed the degraded symptoms (screenshots on 2026-04-28).
The forensic audit (`data/audits/render_forensic_2026_04_28.md`) found the
fixes did land in the DB but either:

  1. the regex was too narrow (caption strip missed `<div>`-wrapped captions),
  2. the data was incomplete (rc_select_passage missing sentence markers and
     options), or
  3. the seed DB (gre_mock.db) lagged the runtime DB (gre_user.db) by 500+
     status changes.

These tests assert the END STATE is correct: no broken live rc_select_passage
rows, the data: URIs survive the sanitizer, known-bad stimuli are demoted, and
the exam assembler only serves `status='live'` rows. They are read-only; they
do NOT modify either DB.

Skipped when the expected DB files are absent (CI / fresh clone before first
run).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
USER_DB = PROJECT_ROOT / "data" / "gre_user.db"
SEED_DB = PROJECT_ROOT / "data" / "gre_mock.db"


def _skip_if_missing(path: Path):
    if not path.exists():
        pytest.skip(f"{path.name} not present; skip render-integrity check")


@pytest.fixture
def user_conn():
    _skip_if_missing(USER_DB)
    conn = sqlite3.connect(str(USER_DB))
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def seed_conn():
    _skip_if_missing(SEED_DB)
    conn = sqlite3.connect(str(SEED_DB))
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------- mispair ---


def test_mispair_qid_3658_is_not_live(user_conn):
    """Cross-shape QC with mismatched figure (options image) must be demoted.

    The mispair audit (commit 1dbf322) demoted this item after Opus 4.7 +
    Sonnet 4.6 both confirmed the embedded image is an answer-grid from a
    different question. Live rotation would expose the bad pairing.
    """
    row = user_conn.execute(
        "SELECT status FROM question WHERE id = 3658"
    ).fetchone()
    assert row is not None, "qid 3658 not found in user DB"
    assert row[0] != "live", (
        f"qid 3658 status={row[0]!r}; mispair audit should have demoted it"
    )


def test_mispair_qid_3658_seed_matches_runtime(user_conn, seed_conn):
    """Seed DB (gre_mock.db) must carry the same status for auditable qids,
    otherwise a fresh install boots into the degraded set."""
    user_status = user_conn.execute(
        "SELECT status FROM question WHERE id = 3658"
    ).fetchone()[0]
    seed_status = seed_conn.execute(
        "SELECT status FROM question WHERE id = 3658"
    ).fetchone()[0]
    assert user_status == seed_status, (
        f"seed/runtime status drift for qid 3658: "
        f"user={user_status!r} seed={seed_status!r}"
    )


# -------------------------------------------------------------- DI cluster ---


def test_stim_1054_content_has_data_uri(user_conn):
    """The Kaplan DI cluster's stimulus must carry inline data: URIs.

    Before the inline fix (commit 1b1e63c), stim 1054 held `src='images/…'`
    references that the wxPython WebView couldn't resolve (blank cluster).
    The inline migration rewrote those into base64 data: URIs.
    """
    row = user_conn.execute(
        "SELECT content FROM stimulus WHERE id = 1054"
    ).fetchone()
    assert row is not None, "stim 1054 not found"
    content = row[0]
    assert "data:image" in content, (
        "stim 1054 does not contain a data: URI — inline migration did not run"
    )
    # No legacy relative paths must remain on the inlined img tags.
    assert not re.search(r'<img[^>]*src=["\']images/', content), (
        "stim 1054 still has src=\"images/…\" — inline migration missed a tag"
    )


def test_stim_1054_data_uri_decodes_to_valid_jpeg(user_conn):
    """Validate that the inlined base64 payload is a real JPEG. A truncated
    or malformed payload would render as the placeholder box the user saw."""
    import base64

    row = user_conn.execute(
        "SELECT content FROM stimulus WHERE id = 1054"
    ).fetchone()
    content = row[0]
    matches = re.findall(r'src="data:image/jpeg;base64,([^"]+)"', content)
    assert matches, "no JPEG data: URIs in stim 1054"
    for payload in matches:
        raw = base64.b64decode(payload, validate=True)
        assert len(raw) > 1024, "decoded JPEG unexpectedly small"
        assert raw[:3] == b"\xff\xd8\xff", (
            f"decoded payload is not a JPEG (magic={raw[:6].hex()})"
        )


def test_stim_1054_survives_html_sanitizer(user_conn):
    """The html sanitizer must pass data: URIs through unchanged — if bleach
    strips them (e.g. protocol not on allow-list), the render fails silently."""
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from widgets.html_sanitizer import safe_html

    content = user_conn.execute(
        "SELECT content FROM stimulus WHERE id = 1054"
    ).fetchone()[0]
    clean = safe_html(content)
    assert "data:image" in clean, (
        "html_sanitizer stripped data: URIs — widgets/html_sanitizer.py needs "
        "`data` in ALLOWED_PROTOCOLS"
    )
    # Img tag count preserved.
    assert clean.count("<img") == content.count("<img")


def test_math_view_template_csp_allows_data_images():
    """The WebView CSP must permit data: URIs for img-src. A mis-parsed CSP
    (e.g. the former multi-line variant WKWebView silently rejected) would
    cause every data: image to render as a placeholder box."""
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from widgets.math_view import HTML_TEMPLATE

    m = re.search(
        r'Content-Security-Policy"\s+content="([^"]+)"',
        HTML_TEMPLATE,
    )
    assert m, "HTML_TEMPLATE missing a CSP meta tag"
    csp = m.group(1)
    # Must be single-line (no literal newlines inside the content attribute).
    assert "\n" not in csp, (
        "CSP contains a newline — WKWebView will ignore everything after it"
    )
    img_src = re.search(r"img-src\s+([^;]+);", csp)
    assert img_src, "no img-src directive in CSP"
    img_tokens = img_src.group(1).split()
    assert "data:" in img_tokens, f"img-src missing data: → {img_tokens!r}"


# ------------------------------------------------------- rc_select_passage ---


def test_no_live_rc_select_passage_missing_options(user_conn):
    """Every live rc_select_passage MUST have at least one QuestionOption,
    otherwise the UI renders no radio buttons and the item is unanswerable."""
    cur = user_conn.execute(
        """
        SELECT q.id
          FROM question q
          LEFT JOIN questionoption o ON o.question_id = q.id
         WHERE q.subtype = 'rc_select_passage'
           AND q.status = 'live'
         GROUP BY q.id
        HAVING COUNT(o.id) = 0
        """
    )
    broken = [row[0] for row in cur.fetchall()]
    assert not broken, (
        f"{len(broken)} live rc_select_passage items have 0 options — "
        f"UI will show passage but no radios: {broken}"
    )


def test_no_live_rc_select_passage_missing_sentence_markers(user_conn):
    """Every live rc_select_passage stimulus MUST contain `<sent id='N'>`
    markers; otherwise the `[N]` sentinels never appear in the passage and
    the radio labels cannot be matched to sentence indices."""
    cur = user_conn.execute(
        """
        SELECT q.id, q.stimulus_id
          FROM question q
          JOIN stimulus s ON s.id = q.stimulus_id
         WHERE q.subtype = 'rc_select_passage'
           AND q.status = 'live'
           AND s.content NOT LIKE '%<sent %'
        """
    )
    broken = [(row[0], row[1]) for row in cur.fetchall()]
    assert not broken, (
        f"{len(broken)} live rc_select_passage stimuli lack <sent id='N'> "
        f"markers — [N] sentinels won't render: {broken}"
    )


# ---------------------------------------------- exam assembler status gate ---


def test_exam_assembler_filters_live_only(user_conn):
    """Build several mock exams via the actual question-bank service; assert
    every selected qid has status='live' in the DB."""
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    # Ensure services.question_bank runs against the runtime DB.
    import config

    assert config.DB_PATH.name == "gre_user.db", (
        f"test fixture points at {config.DB_PATH!r}, not gre_user.db"
    )

    # The `temp_db` fixture in conftest.py monkeypatches `config.DB_PATH` for
    # other tests and evicts `models` / `services` modules from sys.modules
    # when it runs. If such a test ran earlier in the same session, any
    # already-imported `models.database.db` (and all downstream services
    # that captured `from models.database import db`) now point at a
    # tmp_path DB. Force-evict them so our re-import binds to the real
    # runtime DB.
    for prefix in ("models", "services"):
        for mod in [
            m for m in list(sys.modules)
            if m.startswith(prefix + ".") or m == prefix
        ]:
            del sys.modules[mod]

    from models.database import init_db

    init_db()
    from services.question_bank import QuestionBankService

    qbs = QuestionBankService()
    # Exercise the composition path once per measure — seed-stability isn't
    # the contract under test; status-filter correctness is.
    for iteration in range(5):
        qids: list[int] = []
        for measure, count in (("verbal", 15), ("quant", 15)):
            q_ids = qbs.select_questions_composed(
                measure=measure,
                count=count,
                exclude_user_seen=f"test-user-{iteration}",
            )
            qids.extend(q_ids)
        if not qids:
            continue
        placeholders = ",".join("?" * len(qids))
        cur = user_conn.execute(
            f"SELECT id, status FROM question WHERE id IN ({placeholders})",
            qids,
        )
        bad = [(qid, status) for qid, status in cur if status != "live"]
        assert not bad, (
            f"iteration={iteration}: assembler returned non-live qids: {bad}"
        )


# ------------------------------------------------- seed / runtime parity ---


def test_seed_matches_runtime_status(user_conn, seed_conn):
    """Seed DB (gre_mock.db, LFS-tracked) must carry the same status as the
    runtime DB (gre_user.db, gitignored). Drift means a fresh install sees
    different content than an existing user."""
    user_conn.execute(
        f"ATTACH DATABASE '{SEED_DB}' AS seed"
    )
    cur = user_conn.execute(
        """
        SELECT r.id, r.status, s.status
          FROM question r
          JOIN seed.question s ON s.id = r.id
         WHERE r.status != s.status
         LIMIT 10
        """
    )
    drift = cur.fetchall()
    assert not drift, (
        f"{len(drift)} rows with status drift between user/seed DBs; "
        f"first few: {drift}"
    )
