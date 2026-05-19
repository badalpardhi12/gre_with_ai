"""Renderer-smoke test — the encoding-sweep acceptance criterion.

Phase 3.3 of the cleanup plan. Loads a sample of real live questions
out of ``data/gre_user.db`` and pushes their content through the
renderer's pure-Python normalisation pipeline (the path executed
*before* the WebView gets the string). The full ``MathView`` panel
needs wxPython, but the math/HTML preprocessing — which is where
encoding hazards manifest — is headless.

A passing test means: every sampled item's prompt, explanation, and
options can be rewritten by ``_markdown_tables_to_html``,
``_normalise_plain_math``, ``_newlines_to_html``, and ``safe_html``
without the rewriters raising. (They're regex-driven and shouldn't
raise; this guards against a future change that adds an exception
path.)

Sampling: 20 random live items from the user DB, deterministic via a
fixed seed so failures reproduce. If no live DB is available the test
is skipped (this lets pre-commit on an empty checkout still pass).
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

# Skip the whole module if we can't open the user DB — pre-commit
# environments without seeded data shouldn't blow up.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# We import from the live module path. ``widgets.math_view`` defines
# its helpers at module top, so they're available even without
# wxPython (the wx-dependent ``MathView`` class lives behind a guard).
from widgets.math_view import (  # noqa: E402
    _markdown_tables_to_html,
    _newlines_to_html,
    _normalise_plain_math,
)
from widgets.html_sanitizer import safe_html  # noqa: E402
from services.sanitize import sanitize_html  # noqa: E402


def _live_questions_available() -> bool:
    """Return ``True`` only when a populated user DB is on disk."""
    try:
        from config import DB_PATH
        return DB_PATH.exists() and DB_PATH.stat().st_size > 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _live_questions_available(),
    reason="user DB not seeded; renderer smoke-test needs live questions.",
)


def _push_through_renderer(html_body: str) -> str:
    """Run the same chain ``MathView.set_content`` runs, minus the
    wx WebView. Returns the final HTML string fed to the WebView."""
    normalised = _markdown_tables_to_html(html_body or "")
    normalised = _normalise_plain_math(normalised)
    normalised = _newlines_to_html(normalised)
    sanitized = safe_html(normalised)
    return sanitized


# ── Tests ────────────────────────────────────────────────────────────


def _sample_live_questions(n: int = 20):
    """Yield ``n`` random live ``Question`` rows for the smoke test.

    Uses a fixed seed so a regression always reproduces on the same
    items.
    """
    from models.database import Question, init_db
    init_db()
    rng = random.Random(20260518)
    qids = list(
        Question.select(Question.id)
        .where(Question.status == "live")
        .order_by(Question.id)
        .tuples()
    )
    qids = [q[0] for q in qids]
    sample = rng.sample(qids, min(n, len(qids)))
    for qid in sample:
        yield Question.get_by_id(qid)


def test_renderer_smoke_random_20():
    """Twenty random live questions all push through the renderer
    pipeline without raising."""
    rendered = 0
    for q in _sample_live_questions(20):
        out = _push_through_renderer(q.prompt or "")
        assert isinstance(out, str)
        if q.explanation:
            out_e = _push_through_renderer(q.explanation)
            assert isinstance(out_e, str)
        if q.stimulus_id:
            out_s = _push_through_renderer(q.stimulus.content or "")
            assert isinstance(out_s, str)
        for opt in q.options:
            out_o = _push_through_renderer(opt.option_text or "")
            assert isinstance(out_o, str)
        rendered += 1
    assert rendered >= 1, "smoke test sampled zero questions — check fixture"


def test_renderer_smoke_known_problematic():
    """A handful of payloads that previously broke the renderer or
    were hand-crafted to exercise edge cases. All must round-trip."""
    payloads = [
        # GitHub #8 — bare LaTeX option text.
        r"\(\frac{1}{6}\)",
        # GitHub #16 — markdown table inside a stimulus.
        "header1 | header2 |\n|---|---|\n| a | b |",
        # Mixed inline math + display.
        r"$$x^2 + y^2$$ where \(x \in \mathbb{R}\)",
        # Plain ASCII math (sqrt + caret) the normaliser rewrites.
        "Compute sqrt(3) + 2^4 = answer",
        # Empty prompt.
        "",
        # Newline-heavy QC prompt.
        "Quantity A: \\(\\frac{0.6}{0.04}\\)\nQuantity B: \\(\\frac{0.15}{0.01}\\)",
        # Bare ``<`` in math comparison prose.
        "Since \\(0.4545... > 0.45\\), Quantity A is greater.",
        # MCQ-style option with letter prefix.
        "(A) The first choice is correct.",
    ]
    for p in payloads:
        out = _push_through_renderer(p)
        assert isinstance(out, str)


def test_sanitize_html_lenient_does_not_raise_on_known_problems():
    """services.sanitize.sanitize_html(mode='lenient') must absorb the
    same hazards rather than propagating them."""
    payloads = [
        "$$$math$$$",  # triple dollar
        r"a \(\frac{1}{2}\) and $\frac{3}{4}$",  # mixed delimiters
        "\\(\\frac{1}{2}\xa0\\)",  # NBSP in math
        "\\(\\frac{a}{b‘}\\)",  # smart quote in math
    ]
    for p in payloads:
        out = sanitize_html(p, mode="lenient")
        assert isinstance(out, str)
