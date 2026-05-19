#!/usr/bin/env python3
"""Idempotency smoke test for the Phase 1.4 dedup hook.

Drives ``scripts.extract_agieval_math.run`` twice against the same
hand-built input and asserts that the second run inserts ZERO rows
because the dedup service flagged every candidate as a duplicate of
the first run.

Why agieval_math?
-----------------
``extract_agieval_math.py`` already ships a clean test seam
(``_INJECTED_LOADER``) for swapping in synthetic raw rows without
hitting HuggingFace. That keeps this smoke test fully offline and
fast — no network, no LLM, no real ebook needed.

Running
-------
::

    venv/bin/python scripts/test_extract_idempotency.py

The script prints a single-line JSON summary on success, exits non-zero
on failure. It uses an isolated test DB at ``$TMPDIR/dedup_smoke_*/``
so it never touches the real ``data/gre_user.db``.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Project root importable when run as a script.
_THIS = Path(__file__).resolve()
PROJECT_ROOT = _THIS.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Synthetic raw rows ─────────────────────────────────────────────────
#
# Mimic the AGIEval LSAT-LR schema. Three distinct items, each with a
# letter answer and 5 options. The text is contrived so MinHash easily
# fingerprints each one as a unique question.

_FAKE_LSAT_LR_ROWS = [
    {
        "query": "Frangible argument: idiosyncratic vocabulary widget alpha. "
                 "Which of the following best resolves the ambiguity?",
        "options": [
            "the kernel of the matter remains unresolved",
            "the periphery commands the bulk of attention",
            "the speaker has reframed the original premise",
            "the listener disambiguates via context",
            "the argument falls into circular reasoning",
        ],
        "gold": "C",
        "explanation": "The speaker reframes the premise.",
    },
    {
        "query": "Quintessential paradigm shift juxtaposition delta. "
                 "Which conclusion most directly follows?",
        "options": [
            "the paradigm has not shifted",
            "the juxtaposition reveals an inconsistency",
            "the example is purely illustrative",
            "no conclusion follows",
            "the speaker contradicts themselves",
        ],
        "gold": "B",
        "explanation": "The juxtaposition reveals an inconsistency.",
    },
    {
        "query": "Obfuscation epoch sigma echelon. Which inference is best?",
        "options": [
            "obfuscation increases over time",
            "obfuscation decreases over time",
            "obfuscation is independent of time",
            "the question is unanswerable as stated",
            "the inference cannot be drawn",
        ],
        "gold": "D",
        "explanation": "The premise is too sparse to license an inference.",
    },
]


def _inject_loader(source: str):
    """Loader plugged into ``extract_agieval_math._INJECTED_LOADER``."""
    if source.startswith("agieval_lsat_lr"):
        return list(_FAKE_LSAT_LR_ROWS)
    # The pipeline only accepts the LSAT-LR sub-source for our purposes.
    return []


# ── Driver ─────────────────────────────────────────────────────────────


def _run_once(label: str):
    """Single end-to-end invocation. Returns the run summary dict."""
    from scripts import extract_agieval_math
    from services.dedup.dedup_service import reset_dedup_service

    # Force a fresh dedup service per run so the first-run inserts
    # are reflected in the second run's MinHash index. The service
    # caches the index on construction; calling reset_dedup_service
    # before each invocation makes ``find_dup_for`` re-build from the
    # current DB state.
    reset_dedup_service()

    extract_agieval_math._INJECTED_LOADER = _inject_loader
    try:
        summary = extract_agieval_math.run(
            source="agieval_lsat_lr",
            max_items=None,
            reformat=False,
            dry_run=False,
        )
    finally:
        extract_agieval_math._INJECTED_LOADER = None

    return summary


def _bootstrap_isolated_db(workdir: Path) -> None:
    """Point ``config.DB_PATH`` at a fresh tmp file before any model
    import. Must run BEFORE we import ``models.database`` so the
    Peewee bind picks up the override.
    """
    import config  # noqa: F401  (loads .env, defines DATA_DIR)
    fresh_db = workdir / "smoke_user.db"
    config.DB_PATH = fresh_db

    # Re-bind the Peewee database object to point at the new file.
    # ``models.database`` binds at import time, so we either need to
    # not have imported it yet, OR we monkeypatch the SqliteDatabase
    # in place. We choose the second so this script is robust to
    # import order.
    from models import database as db_mod
    db_mod.db.init(str(fresh_db))
    # init_db() runs migrations + creates tables.
    db_mod.init_db()

    # Promote a tiny set of "live" rows so the dedup MinHash index
    # has something to compare against on the FIRST run too. We
    # actually leave the DB empty so first-run inserts ARE the
    # baseline — the second run then sees them as live duplicates
    # IF the inserts upgraded their status. AGIEval inserts land at
    # status='candidate', so we manually promote them between runs.


def _promote_candidates_to_live() -> int:
    """Flip every ``status='candidate'`` row to ``status='live'`` so
    the dedup service's MinHash index (which reads ``status='live'``)
    sees them on the second run.

    Returns the count of promoted rows.
    """
    from models.database import Question
    n = (
        Question
        .update(status="live")
        .where(Question.status == "candidate")
        .execute()
    )
    return n


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="dedup_smoke_"))
    try:
        _bootstrap_isolated_db(workdir)

        first = _run_once("first")
        promoted = _promote_candidates_to_live()
        # Reset the dedup service so its MinHash index re-builds from
        # the now-promoted bank.
        from services.dedup.dedup_service import reset_dedup_service
        reset_dedup_service()
        second = _run_once("second")

        # Assertions
        if first.get("inserted", 0) <= 0:
            print(json.dumps(
                {"ok": False, "reason": "first run inserted nothing",
                 "first": first, "second": second}, indent=2))
            return 1
        if second.get("inserted", 0) != 0:
            print(json.dumps(
                {"ok": False,
                 "reason": "second run inserted >0 rows; dedup hook missed",
                 "first": first, "second": second}, indent=2))
            return 1

        print(json.dumps({
            "ok": True,
            "first_inserted": first.get("inserted"),
            "first_skipped": first.get("skipped"),
            "promoted": promoted,
            "second_inserted": second.get("inserted"),
            "second_skipped": second.get("skipped"),
        }, indent=2))
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
