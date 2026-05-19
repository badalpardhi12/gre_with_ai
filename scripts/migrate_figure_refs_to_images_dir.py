#!/usr/bin/env python3
"""Renderer figure_refs unification (Phase 5.1).

The renderer (``widgets/math_view.py:237-239``) restricts its WebView base
URL to ``data/images/`` for security (CSP + path-traversal mitigation).
However, several extractors populate ``Question.figure_refs`` with paths
relative to ``data/extracted/<source>/images/`` — which the renderer cannot
read. This script reconciles the two layouts by COPYING referenced files
into ``data/images/figure_refs_migrated/`` and updating the JSON pointer.

Defaults to ``--dry-run``; pass ``--apply`` to mutate the live DB.

Idempotency
-----------
A path that already resolves under ``data/images/`` is left alone. The
script never deletes the source file, so re-running on a fresh extract
or after a partial run is safe. A second invocation of ``--apply`` is a
no-op (every pointer is already migrated).

Broken-pointer handling
-----------------------
If the relative path doesn't resolve to an on-disk file, the script:
  1. Sets the offending entry's ``figure_refs`` to ``[]`` (preserves the
     row, just clears the broken pointer).
  2. Inserts a ``QuestionFlag`` row with ``reason='missing_figure_file'``
     and the original path serialised in ``note`` as JSON. Phase 6.4
     consumes those rows when deciding what to synthesise.

Path resolution
---------------
``figure_refs`` entries are typically relative — e.g. ``images/X.gif``
without the source prefix. The script tries, in order:
  * absolute or already-rooted under ``data/images/`` -> no-op.
  * ``<repo>/<entry>`` (entry already includes ``data/extracted/...``).
  * ``<repo>/data/extracted/<source_dir>/<entry>`` (most common).

Where ``<source_dir>`` maps from ``Question.source``:
    princeton_2012      -> princeton
    kaplan_2024         -> kaplan
    manhattan_5lb_2018  -> manhattan
    ets_og_3rd          -> ets_og
    *                    -> source itself (best-effort)

Usage
-----
    venv/bin/python scripts/migrate_figure_refs_to_images_dir.py            # dry-run (default)
    venv/bin/python scripts/migrate_figure_refs_to_images_dir.py --apply    # actually mutate
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Make the project root importable when run from anywhere.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import DATA_DIR  # noqa: E402
from models.database import (  # noqa: E402
    db, Question, QuestionFlag, init_db,
)


# ── Constants ────────────────────────────────────────────────────────

IMAGES_DIR = DATA_DIR / "images"
MIGRATED_SUBDIR = IMAGES_DIR / "figure_refs_migrated"
EXTRACTED_DIR = DATA_DIR / "extracted"

# Source -> extracted/<dir> mapping. Falls back to ``source`` itself if
# the source isn't in this table.
SOURCE_TO_DIR = {
    "princeton_2012": "princeton",
    "kaplan_2024": "kaplan",
    "manhattan_5lb_2018": "manhattan",
    "ets_og_3rd": "ets_og",
    "manhattan_di": "manhattan",
}


# ── Resolution helpers ──────────────────────────────────────────────

def _is_already_migrated(entry: str) -> bool:
    """True when the entry's path lies under ``data/images/`` already."""
    if not entry:
        return False
    p = Path(entry)
    # Make absolute relative to repo root for comparison; the renderer
    # reads from data/images, so anything starting with that prefix is fine.
    if p.is_absolute():
        try:
            p.resolve().relative_to(IMAGES_DIR.resolve())
            return True
        except ValueError:
            return False
    # Relative: check if the path starts with `data/images/` or `images/`
    # rooted at data dir.
    parts = p.parts
    if len(parts) >= 2 and parts[0] == "data" and parts[1] == "images":
        return True
    return False


def _candidate_paths(entry: str, source: str) -> List[Path]:
    """All plausible on-disk locations for ``entry`` given ``source``."""
    candidates: List[Path] = []
    p = Path(entry)
    # 1. Absolute path -> use as-is.
    if p.is_absolute():
        candidates.append(p)
        return candidates
    # 2. Already includes ``data/extracted/...`` or similar — try as-is.
    candidates.append(_REPO_ROOT / entry)
    # 3. Map source -> extracted/<dir>/<entry>.
    src_dir = SOURCE_TO_DIR.get(source, source)
    candidates.append(EXTRACTED_DIR / src_dir / entry)
    return candidates


def _resolve_existing(entry: str, source: str) -> Optional[Path]:
    """Return the first candidate that exists on disk, else None."""
    for candidate in _candidate_paths(entry, source):
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _migrated_target(entry: str, source: str) -> Path:
    """Compute the canonical destination under ``data/images/figure_refs_migrated/``.

    The filename is ``<source>_<basename>`` so two sources never collide.
    """
    basename = Path(entry).name
    safe_source = source or "unknown"
    return MIGRATED_SUBDIR / f"{safe_source}_{basename}"


def _relative_repo_path(absolute: Path) -> str:
    """Express *absolute* as a repo-relative POSIX path (for storage)."""
    try:
        return absolute.resolve().relative_to(_REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return absolute.as_posix()


# ── Main migration ─────────────────────────────────────────────────

def _flag_missing(question: Question, missing_paths: List[str]) -> None:
    """Record a ``QuestionFlag`` row with ``reason='missing_figure_file'``.

    Idempotent per (question, user='migration'): updates the note if
    a flag for the same reason already exists.
    """
    payload = json.dumps({"missing_paths": missing_paths,
                          "source_system": "migrate_figure_refs"})
    existing = QuestionFlag.get_or_none(
        QuestionFlag.question == question,
        QuestionFlag.user_id == "migration",
        QuestionFlag.reason == "missing_figure_file",
    )
    if existing is not None:
        if existing.note != payload:
            existing.note = payload
            existing.save()
        return
    QuestionFlag.create(
        question=question,
        user_id="migration",
        reason="missing_figure_file",
        note=payload,
    )


def migrate(apply: bool) -> Tuple[int, int, int]:
    """Run the migration pass.

    Returns ``(migrated_files, broken_pointers_flagged, already_migrated_skipped)``.
    """
    init_db()

    migrated_files = 0
    broken_pointers_flagged = 0
    already_migrated_skipped = 0

    # Only scan live items per the spec ("every live Question row"). Items
    # that aren't live can be migrated lazily if/when they are promoted.
    rows = list(
        Question.select()
        .where(
            (Question.figure_refs.is_null(False))
            & (Question.figure_refs != "")
            & (Question.figure_refs != "[]")
            & (Question.status == "live")
        )
    )

    if apply:
        MIGRATED_SUBDIR.mkdir(parents=True, exist_ok=True)

    # Wrap the per-row updates in a transaction so a crash mid-loop
    # leaves the DB consistent.
    with db.atomic():
        for q in rows:
            try:
                refs = json.loads(q.figure_refs or "[]")
                if not isinstance(refs, list):
                    refs = []
            except (ValueError, TypeError):
                refs = []

            new_refs: List[str] = []
            missing_for_this_row: List[str] = []
            row_changed = False

            for entry in refs:
                if not isinstance(entry, str) or not entry:
                    continue
                if _is_already_migrated(entry):
                    new_refs.append(entry)
                    already_migrated_skipped += 1
                    continue

                source_path = _resolve_existing(entry, q.source or "")
                if source_path is None:
                    # Broken pointer. Drop the entry and remember for flag.
                    missing_for_this_row.append(entry)
                    row_changed = True
                    continue

                # Plan the copy -> data/images/figure_refs_migrated/<source>_<file>
                dest = _migrated_target(entry, q.source or "")
                if apply:
                    if not dest.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        # copy2 preserves mtime; never deletes source.
                        shutil.copy2(str(source_path), str(dest))
                    new_refs.append(_relative_repo_path(dest))
                    migrated_files += 1
                    row_changed = True
                else:
                    # Dry-run: don't touch disk; still report what we'd do.
                    new_refs.append(_relative_repo_path(dest))
                    migrated_files += 1
                    row_changed = True

            if missing_for_this_row:
                broken_pointers_flagged += len(missing_for_this_row)
                if apply:
                    _flag_missing(q, missing_for_this_row)

            if apply and row_changed:
                q.figure_refs = json.dumps(new_refs)
                q.save()

    return migrated_files, broken_pointers_flagged, already_migrated_skipped


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate Question.figure_refs entries into data/images/.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually mutate the DB and copy files (default: dry-run).",
    )
    args = parser.parse_args(argv)

    if not args.apply:
        print("[dry-run] No DB writes, no file copies. Pass --apply to commit.")

    migrated, broken, skipped = migrate(apply=args.apply)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(
        f"[{mode}] migrated_files={migrated}, "
        f"broken_pointers_flagged={broken}, "
        f"already_migrated_skipped={skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
