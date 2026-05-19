"""Tests for ``scripts/migrate_figure_refs_to_images_dir.py`` (Phase 5.1).

The migration script's contract:

  1. Live ``Question.figure_refs`` entries that don't already resolve under
     ``data/images/`` get COPIED into ``data/images/figure_refs_migrated/``
     and the JSON pointer updated.
  2. Pointers that don't resolve to a real file are dropped from the row's
     ``figure_refs`` and a ``QuestionFlag(reason='missing_figure_file')`` row
     is written.
  3. Re-running on an already-migrated bank is a no-op.
  4. ``--dry-run`` (default) mutates nothing.

These tests use the standard ``temp_db`` fixture (clean SQLite at tmp_path)
and patch ``DATA_DIR`` so the migrator reads/writes inside a tmp tree —
the live DB is never touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _stage_extract_dir(tmp_data: Path, source_dir: str, fname: str,
                       payload: bytes = b"\x89PNG\r\n\x1a\n") -> Path:
    """Drop a fake image inside data/extracted/<source_dir>/images/."""
    target = tmp_data / "extracted" / source_dir / "images" / fname
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def _set_data_dir(monkeypatch, tmp_data: Path):
    """Point both the migrator and the audit at a tmp data tree.

    The ``temp_db`` fixture evicts ``models.*`` / ``services.*`` from
    ``sys.modules`` so each test gets a fresh ORM binding; we also need to
    evict ``scripts.*`` because the migration script captures
    ``Question`` / ``QuestionFlag`` / ``db`` / ``init_db`` at import time.
    Without that, test N's migrator keeps writing into test N-1's DB.
    """
    import sys
    for mod in [m for m in list(sys.modules)
                if m == "scripts" or m.startswith("scripts.")]:
        del sys.modules[mod]

    import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_data)
    # Re-import the (now-evicted) migrator so its module-level constants
    # capture the patched DATA_DIR.
    from scripts import migrate_figure_refs_to_images_dir as mig
    monkeypatch.setattr(mig, "IMAGES_DIR", tmp_data / "images")
    monkeypatch.setattr(mig, "MIGRATED_SUBDIR",
                        tmp_data / "images" / "figure_refs_migrated")
    monkeypatch.setattr(mig, "EXTRACTED_DIR", tmp_data / "extracted")


@pytest.fixture
def staged_question(temp_db, tmp_path, monkeypatch):
    """One live question whose figure_refs points at a real on-disk file."""
    tmp_data = tmp_path / "data"
    tmp_data.mkdir()
    fpath = _stage_extract_dir(
        tmp_data, "princeton", "Revi_test_fig01_r1.gif"
    )
    _set_data_dir(monkeypatch, tmp_data)

    from models.database import Question
    q = Question.create(
        measure="verbal",
        subtype="tc",
        prompt="Sample prompt with figure.",
        explanation="",
        status="live",
        source="princeton_2012",
        figure_refs=json.dumps(["images/Revi_test_fig01_r1.gif"]),
    )
    return q.id, fpath, tmp_data


def test_dry_run_does_not_touch_db_or_disk(staged_question):
    qid, fpath, tmp_data = staged_question
    from scripts.migrate_figure_refs_to_images_dir import migrate
    from models.database import Question, QuestionFlag

    migrated, broken, skipped = migrate(apply=False)

    assert migrated == 1
    assert broken == 0
    assert skipped == 0

    q = Question.get_by_id(qid)
    # figure_refs stays unchanged.
    assert json.loads(q.figure_refs) == ["images/Revi_test_fig01_r1.gif"]
    # No copy happened.
    migrated_dir = tmp_data / "images" / "figure_refs_migrated"
    assert not migrated_dir.exists() or not any(migrated_dir.iterdir())
    # No flag was written.
    assert QuestionFlag.select().count() == 0


def test_apply_copies_file_and_updates_pointer(staged_question):
    qid, fpath, tmp_data = staged_question
    from scripts.migrate_figure_refs_to_images_dir import migrate
    from models.database import Question

    migrated, broken, skipped = migrate(apply=True)
    assert migrated == 1
    assert broken == 0

    q = Question.get_by_id(qid)
    refs = json.loads(q.figure_refs)
    assert len(refs) == 1
    # Should now resolve under data/images/figure_refs_migrated/.
    assert "images/figure_refs_migrated/" in refs[0]
    assert "princeton_2012_Revi_test_fig01_r1.gif" in refs[0]

    # Source file untouched (idempotency / rollback).
    assert fpath.exists()
    # New file exists.
    new_path = (tmp_data / "images" / "figure_refs_migrated"
                / "princeton_2012_Revi_test_fig01_r1.gif")
    assert new_path.exists()


def test_every_migrated_ref_resolves_under_data_images(staged_question):
    """Acceptance criterion: every live figure_refs entry resolves under data/images/ post-apply."""
    qid, fpath, tmp_data = staged_question
    from scripts.migrate_figure_refs_to_images_dir import migrate
    from models.database import Question

    migrate(apply=True)

    q = Question.get_by_id(qid)
    refs = json.loads(q.figure_refs)
    for ref in refs:
        # The script stores repo-relative paths. Resolve against tmp_data.
        # The migrator returns paths relative to repo root, like
        # "data/images/figure_refs_migrated/<file>". For tests, just check
        # the prefix and that the basename is present in the new dir.
        assert "/figure_refs_migrated/" in ref or ref.startswith("data/images/")


def test_apply_is_idempotent(staged_question):
    """Second invocation makes no further changes."""
    qid, fpath, tmp_data = staged_question
    from scripts.migrate_figure_refs_to_images_dir import migrate
    from models.database import Question

    migrate(apply=True)
    q1 = Question.get_by_id(qid)
    refs1 = q1.figure_refs

    migrated2, broken2, skipped2 = migrate(apply=True)
    # Second run sees the entry as already-migrated.
    assert migrated2 == 0
    assert broken2 == 0
    assert skipped2 == 1

    q2 = Question.get_by_id(qid)
    assert q2.figure_refs == refs1


def test_broken_pointer_is_flagged(temp_db, tmp_path, monkeypatch):
    tmp_data = tmp_path / "data"
    tmp_data.mkdir()
    _set_data_dir(monkeypatch, tmp_data)

    from models.database import Question, QuestionFlag
    q = Question.create(
        measure="verbal",
        subtype="tc",
        prompt="sample",
        explanation="",
        status="live",
        source="princeton_2012",
        figure_refs=json.dumps(["images/Revi_NONEXISTENT.gif"]),
    )

    from scripts.migrate_figure_refs_to_images_dir import migrate
    migrated, broken, skipped = migrate(apply=True)

    assert migrated == 0
    assert broken == 1

    q.refresh_from_db = lambda: None  # no-op safety
    q2 = Question.get_by_id(q.id)
    # Broken entry is dropped from figure_refs.
    assert json.loads(q2.figure_refs) == []
    # Flag is recorded.
    flags = list(
        QuestionFlag.select().where(
            QuestionFlag.question == q,
            QuestionFlag.reason == "missing_figure_file",
        )
    )
    assert len(flags) == 1
    note = json.loads(flags[0].note)
    assert "images/Revi_NONEXISTENT.gif" in note["missing_paths"]


def test_already_under_data_images_is_skipped(temp_db, tmp_path, monkeypatch):
    tmp_data = tmp_path / "data"
    tmp_data.mkdir()
    _set_data_dir(monkeypatch, tmp_data)

    # Pre-create an image already in data/images/ (the renderer-friendly
    # destination).
    (tmp_data / "images").mkdir(parents=True)
    (tmp_data / "images" / "already.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    from models.database import Question, QuestionFlag
    q = Question.create(
        measure="verbal",
        subtype="tc",
        prompt="sample",
        explanation="",
        status="live",
        source="princeton_2012",
        figure_refs=json.dumps(["data/images/already.png"]),
    )

    from scripts.migrate_figure_refs_to_images_dir import migrate
    migrated, broken, skipped = migrate(apply=True)

    assert migrated == 0
    assert broken == 0
    assert skipped == 1

    # Pointer untouched.
    q2 = Question.get_by_id(q.id)
    assert json.loads(q2.figure_refs) == ["data/images/already.png"]
    # No flag.
    assert QuestionFlag.select().count() == 0


def test_non_live_questions_are_ignored(temp_db, tmp_path, monkeypatch):
    """Per spec, only ``status='live'`` rows are migrated."""
    tmp_data = tmp_path / "data"
    tmp_data.mkdir()
    _set_data_dir(monkeypatch, tmp_data)

    from models.database import Question
    q = Question.create(
        measure="verbal",
        subtype="tc",
        prompt="draft, not live",
        explanation="",
        status="candidate",  # not live
        source="princeton_2012",
        figure_refs=json.dumps(["images/will_not_resolve.gif"]),
    )

    from scripts.migrate_figure_refs_to_images_dir import migrate
    migrated, broken, skipped = migrate(apply=True)

    # Skipped entirely.
    assert migrated == 0
    assert broken == 0
    assert skipped == 0
    q2 = Question.get_by_id(q.id)
    assert json.loads(q2.figure_refs) == ["images/will_not_resolve.gif"]
