"""
Lightweight on-launch schema migrator.

Each migration is an idempotent (name, callable) pair registered in the
MIGRATIONS list below. `apply_pending_migrations()` runs every entry whose
name is not yet in the SchemaMigration table, inside a single `db.atomic()`
block per migration.

Migrations may issue CREATE INDEX, ALTER TABLE ADD COLUMN, or UPDATE statements
via `db.execute_sql(...)`. Each callable should swallow "duplicate column" and
"index already exists" errors so that a partially-applied migration can be
retried without manual cleanup.

To add a migration:
1. Write a function `def _NNN_short_name(): ...` that uses `db.execute_sql(...)`.
2. Append `("NNN_short_name", _NNN_short_name)` to MIGRATIONS in order.

The applied-migration ledger lives in `SchemaMigration` (created by `init_db`).
"""
from datetime import datetime

from peewee import (
    Model, AutoField, CharField, DateTimeField, OperationalError,
)

from services.log import get_logger

logger = get_logger("migrations")


def _get_db():
    """Lazy import to avoid circular import with models.database."""
    from models.database import db
    return db


class SchemaMigration(Model):
    id = AutoField()
    name = CharField(unique=True)
    applied_at = DateTimeField(default=datetime.now)

    class Meta:
        database = None  # bound at register time

    @classmethod
    def bind_db(cls, db):
        cls._meta.database = db


def _is_benign_schema_error(exc: Exception) -> bool:
    """Treat `duplicate column`, `index already exists`, etc. as success."""
    msg = str(exc).lower()
    return any(s in msg for s in (
        "duplicate column",
        "already exists",
        "no such column",  # ALTER on non-existent column == nothing to do
    ))


# ── Migrations ────────────────────────────────────────────────────────


def _001_numeric_answer_mode():
    """Add NumericAnswer.mode column; backfill from numerator/denominator presence."""
    db = _get_db()
    try:
        db.execute_sql(
            "ALTER TABLE numericanswer ADD COLUMN mode VARCHAR(16) DEFAULT 'auto'"
        )
    except OperationalError as e:
        if not _is_benign_schema_error(e):
            raise
    db.execute_sql(
        "UPDATE numericanswer SET mode='fraction' "
        "WHERE numerator IS NOT NULL AND (mode IS NULL OR mode='auto')"
    )
    db.execute_sql(
        "UPDATE numericanswer SET mode='decimal' "
        "WHERE numerator IS NULL AND (mode IS NULL OR mode='auto')"
    )


def _002_numeric_answer_default_tolerance():
    """Bump existing decimal questions with tolerance=0 to a small default."""
    db = _get_db()
    db.execute_sql(
        "UPDATE numericanswer SET tolerance=0.001 "
        "WHERE (tolerance IS NULL OR tolerance=0) AND exact_value IS NOT NULL"
    )


def _003_flashcard_review_indexes():
    """Add indexes for the heavily-queried due_cards path."""
    db = _get_db()
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_flashcardreview_next_review "
        "ON flashcardreview(next_review_at)",
        "CREATE INDEX IF NOT EXISTS idx_flashcardreview_user_next "
        "ON flashcardreview(user_id, next_review_at)",
    ):
        try:
            db.execute_sql(stmt)
        except OperationalError as e:
            if not _is_benign_schema_error(e):
                raise


def _004_user_stats():
    """Ensure a UserStats row exists for the default local user.

    The CREATE TABLE ran via `db.create_tables` in init_db; this migration
    just guarantees the singleton row so callers can `get_or_create`-free.
    """
    db = _get_db()
    db.execute_sql(
        "INSERT OR IGNORE INTO userstats "
        "(user_id, current_streak, longest_streak, streak_freezes_left, "
        " daily_goal_minutes) "
        "VALUES ('local', 0, 0, 1, 20)"
    )


def _005_onboarding_inferred_complete():
    """Mark existing users (with any Response rows) as already onboarded.

    Brand-new clones get the onboarding wizard; users who upgrade from an
    older version with a populated DB shouldn't be forced through it.
    """
    db = _get_db()
    db.execute_sql(
        "UPDATE userstats "
        "SET onboarding_completed_at = CURRENT_TIMESTAMP "
        "WHERE user_id='local' "
        "  AND onboarding_completed_at IS NULL "
        "  AND EXISTS (SELECT 1 FROM response LIMIT 1)"
    )


# 73 question IDs flagged by `scripts/audit_data_corruption.py` on
# 2026-04-18 as having corrupted answer keys, mismatched explanations,
# or LLM "Wait—let me reconsider" artifacts. Hard-coded so that fresh
# clones reach the same retired state without running
# `scripts/retire_corrupted_questions.py` (which carries the same list).
_CORRUPT_QIDS_2026_04 = (
    8, 169, 264, 432, 433, 434, 441, 442, 443, 461, 462, 469,
    627, 633, 691, 722, 751, 864, 871, 908, 1029, 1152, 1177, 1191,
    1206, 1269, 1270, 1278, 1279, 1280, 1282, 1285, 1288, 1291, 1292, 1293,
    1295, 1300, 1301, 1305, 1306, 1310, 1314, 1316, 1319, 1321, 1324, 1326,
    1327, 1328, 1330, 1334, 1337, 1348, 1350, 1355, 1356, 1357, 1361, 1364,
    1370, 1371, 1372, 1373, 1374, 1375, 1377, 1379, 1380, 1381, 2253, 2255,
    2418,
)


def _006_retire_corrupted_2026_04():
    """Retire 73 questions with answer-key/explanation corruption.

    Idempotent: rows already at status='retired' stay retired. Missing
    IDs are silently skipped — a fresh clone with the shipped DB hits
    every ID, but a re-extracted DB might not contain them all.
    """
    db = _get_db()
    placeholders = ",".join("?" for _ in _CORRUPT_QIDS_2026_04)
    db.execute_sql(
        f"UPDATE question SET status='retired' "
        f"WHERE id IN ({placeholders}) AND status != 'retired'",
        _CORRUPT_QIDS_2026_04,
    )


# Second batch found 2026-04-18 by structural audit (user-reported
# screenshots). 1 TC mis-keyed (qid 604, "Loki") + 111 QC questions
# whose prompts lack the "Quantity A:" / "Quantity B:" labels (so the
# rendered question is structurally unanswerable) + 1 quant DI question
# whose chart wasn't extracted (qid 948).
_INCOMPLETE_QIDS_2026_04 = (
    604, 678, 679, 681, 682, 683, 684, 685, 686, 687, 688, 689, 690,
    692, 693, 694, 695, 696, 698, 699, 700, 701, 702, 703, 704, 705,
    706, 707, 708, 709, 710, 711, 712, 713, 714, 715, 716, 717, 719,
    720, 721, 723, 724, 725, 726, 727, 729, 730, 731, 732, 733, 734,
    735, 736, 737, 738, 739, 740, 741, 742, 743, 744, 745, 746, 747,
    748, 749, 750, 752, 753, 754, 755, 756, 757, 758, 760, 761, 762,
    764, 765, 766, 767, 768, 770, 771, 772, 773, 774, 775, 776, 777,
    778, 779, 780, 781, 782, 783, 785, 786, 787, 788, 789, 790, 792,
    793, 794, 795, 797, 798, 799, 800, 948, 2214,
)


def _007_retire_incomplete_2026_04():
    """Retire 113 questions that are structurally incomplete or
    mis-keyed (separate bug class from migration 006).

    Idempotent. Same skip-if-missing semantics as 006.
    """
    db = _get_db()
    placeholders = ",".join("?" for _ in _INCOMPLETE_QIDS_2026_04)
    db.execute_sql(
        f"UPDATE question SET status='retired' "
        f"WHERE id IN ({placeholders}) AND status != 'retired'",
        _INCOMPLETE_QIDS_2026_04,
    )


# Third batch found 2026-04-18 — 22 RC questions whose option E text
# leaked past its boundary and absorbed the "Questions N-M refer to the
# following passage. <passage…>" marker that belonged to the *next*
# question set. The leaked option is unreadable and the next question
# set was extracted without its passage.
_OPTION_LEAK_QIDS_2026_04 = (
    810, 812, 813, 816, 817, 820, 824, 825, 829, 831, 837, 838, 840,
    1022, 1025, 1029, 1034, 1037, 1039, 1043, 1050, 1056,
)


def _008_retire_option_leak_2026_04():
    """Retire 22 RC questions with passage-marker leakage in option E.

    Idempotent.
    """
    db = _get_db()
    placeholders = ",".join("?" for _ in _OPTION_LEAK_QIDS_2026_04)
    db.execute_sql(
        f"UPDATE question SET status='retired' "
        f"WHERE id IN ({placeholders}) AND status != 'retired'",
        _OPTION_LEAK_QIDS_2026_04,
    )


# Fourth batch (2026-04-19): quant questions that name a labeled
# geometric figure (e.g. "Triangle BCD") then reference a separate
# segment whose endpoints aren't in the figure ("AB = 1" — but A is
# never defined). These are unanswerable without a diagram. Detected
# by the new "geometry_needs_figure" rule in
# scripts/audit_data_corruption.py.
_GEOMETRY_NO_FIGURE_QIDS_2026_04 = (
    638,
)


def _009_retire_geometry_no_figure_2026_04():
    """Retire quant geometry questions with undefined points.

    Idempotent. Same skip-if-missing semantics as 006-008.
    """
    db = _get_db()
    placeholders = ",".join("?" for _ in _GEOMETRY_NO_FIGURE_QIDS_2026_04)
    db.execute_sql(
        f"UPDATE question SET status='retired' "
        f"WHERE id IN ({placeholders}) AND status != 'retired'",
        _GEOMETRY_NO_FIGURE_QIDS_2026_04,
    )


# Fifth batch (2026-04-21): RC questions whose `stimulus_id` points at
# the wrong passage — the question stem references one topic
# (Matisse/Picasso, invisible/guerrilla theater) but the linked
# stimulus is about something completely different (Marie Antoinette,
# quantum mechanics). Surfaced by the tightened "Explanation-from-other"
# detector in scripts/audit_data_corruption.py: with the false-positive
# heuristics filtered out, these stand out as quoting prose that exists
# nowhere in the prompt, options, or attached stimulus.
#   QID 2684          — Matisse/Picasso → Marie Antoinette stim
#   QIDs 2759-2762    — invisible/guerrilla theater → quantum mechanics stim
# The source passages aren't present in any stimulus row, so re-linking
# isn't an option. Retire until/unless the original Manhattan passages
# get re-extracted.
_ORPHAN_RC_STIM_QIDS_2026_04 = (
    2684, 2759, 2760, 2761, 2762,
)


def _010_retire_orphan_rc_stim_2026_04():
    """Retire RC questions linked to the wrong stimulus.

    Idempotent. Same skip-if-missing semantics as 006-009.
    """
    db = _get_db()
    placeholders = ",".join("?" for _ in _ORPHAN_RC_STIM_QIDS_2026_04)
    db.execute_sql(
        f"UPDATE question SET status='retired' "
        f"WHERE id IN ({placeholders}) AND status != 'retired'",
        _ORPHAN_RC_STIM_QIDS_2026_04,
    )


def _011_retire_legacy_quant_imports_2026_04():
    """Retire 460 live + 129 already-retired quant rows whose source is
    'imported' (Kaplan / Princeton / seed-data leftovers). Manhattan
    quant import lands fresh under source='manhattan_5lb_2018' starting
    in this same release. ai_generated rows are untouched. Idempotent.
    """
    db = _get_db()
    db.execute_sql(
        "UPDATE question SET status='retired' "
        "WHERE measure='quant' AND source='imported' AND status != 'retired'"
    )


MIGRATIONS = [
    ("001_numeric_answer_mode", _001_numeric_answer_mode),
    ("002_numeric_answer_default_tolerance", _002_numeric_answer_default_tolerance),
    ("003_flashcard_review_indexes", _003_flashcard_review_indexes),
    ("004_user_stats", _004_user_stats),
    ("005_onboarding_inferred_complete", _005_onboarding_inferred_complete),
    ("006_retire_corrupted_2026_04", _006_retire_corrupted_2026_04),
    ("007_retire_incomplete_2026_04", _007_retire_incomplete_2026_04),
    ("008_retire_option_leak_2026_04", _008_retire_option_leak_2026_04),
    ("009_retire_geometry_no_figure_2026_04",
     _009_retire_geometry_no_figure_2026_04),
    ("010_retire_orphan_rc_stim_2026_04",
     _010_retire_orphan_rc_stim_2026_04),
    ("011_retire_legacy_quant_imports_2026_04",
     _011_retire_legacy_quant_imports_2026_04),
]


def apply_pending_migrations():
    """Run every unapplied migration in registration order."""
    db = _get_db()
    SchemaMigration.bind_db(db)
    db.create_tables([SchemaMigration], safe=True)

    applied = {m.name for m in SchemaMigration.select(SchemaMigration.name)}
    for name, func in MIGRATIONS:
        if name in applied:
            continue
        try:
            with db.atomic():
                func()
                SchemaMigration.create(name=name)
            logger.info("applied migration %s", name)
        except Exception:
            logger.exception("migration %s failed", name)
            raise
