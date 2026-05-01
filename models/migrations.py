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


def _012_synthetic_provenance_2026_04():
    """Schema scaffolding for the synthetic-question generation pipeline.

    Adds four columns to `question` so the pipeline can record per-item
    audit data (full pipeline blob, SME notes, generation timestamp, run
    correlation id) without dropping anything onto the LLM-free imported
    rows. Existing rows pick up the column defaults via SQLite's ALTER
    TABLE semantics.

    Companion table `syntheticgenerationrun` is created by
    `db.create_tables(ALL_TABLES, safe=True)` in `init_db()`; this
    migration only handles the column-add side.

    Idempotent: each ALTER tolerates a re-run via `_is_benign_schema_error`.
    """
    db = _get_db()
    column_stmts = (
        "ALTER TABLE question ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE question ADD COLUMN review_notes TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE question ADD COLUMN generated_at DATETIME",
        "ALTER TABLE question ADD COLUMN run_id VARCHAR(64) NOT NULL DEFAULT ''",
    )
    for stmt in column_stmts:
        try:
            db.execute_sql(stmt)
        except OperationalError as e:
            if not _is_benign_schema_error(e):
                raise
    # Index on run_id for cheap "show me this batch's items" lookups in
    # the SME review queue. Peewee creates this implicitly for `index=True`
    # fields on fresh tables, but ALTER doesn't trigger that, so we add it
    # explicitly.
    try:
        db.execute_sql(
            "CREATE INDEX IF NOT EXISTS idx_question_run_id ON question(run_id)"
        )
    except OperationalError as e:
        if not _is_benign_schema_error(e):
            raise


def _013_question_lifecycle_2026_05():
    """R5 — full candidate -> pretest -> live lifecycle for synthetic items.

    The Phase-1 synthetic pipeline drops new items at status='candidate'
    instead of the pre-existing 'draft' (which legacy import scripts use
    for hand-edited rows). Once an SME approves the candidate it moves
    to 'pretest' and gets seeded into Quick Drill at low frequency; this
    migration adds the columns the IRT estimator (Phase 2) reads to
    decide when to promote 'pretest' -> 'live'.

    Schema delta:
      - pretest_started_at        DATETIME (NULL = not yet pretesting)
      - pretest_n_responses       INTEGER DEFAULT 0
      - pretest_p_correct         FLOAT
      - pretest_disc_proxy        FLOAT  (point-biserial proxy)
      - irt_b_estimate            FLOAT  (difficulty parameter b)
      - irt_a_estimate            FLOAT  (discrimination parameter a)
      - promotion_at              DATETIME

    Status enum widened to include 'candidate' and 'pretest' alongside
    the existing draft/review/pilot/live/retired set. SQLite stores
    `status` as a free-form CHAR so this is a code-level change in the
    Peewee model + a partial index for the heavy "show me pretest items"
    query.

    Index: idx_question_status_pretest is partial — only rows with
    status='pretest' appear in it, which keeps it tiny for the lifetime
    of the bank.

    Idempotent.
    """
    db = _get_db()
    column_stmts = (
        "ALTER TABLE question ADD COLUMN pretest_started_at DATETIME",
        "ALTER TABLE question ADD COLUMN pretest_n_responses "
        "INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE question ADD COLUMN pretest_p_correct FLOAT",
        "ALTER TABLE question ADD COLUMN pretest_disc_proxy FLOAT",
        "ALTER TABLE question ADD COLUMN irt_b_estimate FLOAT",
        "ALTER TABLE question ADD COLUMN irt_a_estimate FLOAT",
        "ALTER TABLE question ADD COLUMN promotion_at DATETIME",
    )
    for stmt in column_stmts:
        try:
            db.execute_sql(stmt)
        except OperationalError as e:
            if not _is_benign_schema_error(e):
                raise
    # Partial index over only status='pretest' rows. Lookup pattern:
    # "give me the next pretest candidate to slot into Quick Drill" hits
    # this index thousands of times an hour during pretesting.
    try:
        db.execute_sql(
            "CREATE INDEX IF NOT EXISTS idx_question_status_pretest "
            "ON question(status, pretest_n_responses) "
            "WHERE status = 'pretest'"
        )
    except OperationalError as e:
        if not _is_benign_schema_error(e):
            raise


def _014_source_anchor_2026_04():
    """Add ``source_anchor`` column to ``question`` for idempotent extractor
    upserts.

    Princeton + Kaplan extractors stamp each row with the publisher's
    per-item locator (e.g. ``"QST41"``) so a re-run of the extraction
    pipeline doesn't duplicate rows — the upsert key is
    ``(source, source_anchor)``. Legacy rows default to empty string so
    existing behaviour is unchanged.

    Note on overlap with migration 012: migration 012 already added the
    ``review_notes`` column. Princeton's original plan was a single 012
    that added both ``source_anchor`` + ``review_notes``; during
    consolidation we kept synthetic's 012 and moved ``source_anchor``
    into this new 014 to avoid column-collision.

    Idempotent via ``_is_benign_schema_error``.
    """
    db = _get_db()
    try:
        db.execute_sql(
            "ALTER TABLE question ADD COLUMN source_anchor "
            "VARCHAR(255) NOT NULL DEFAULT ''"
        )
    except OperationalError as e:
        if not _is_benign_schema_error(e):
            raise
    # Covering index for upsert lookups by (source, source_anchor).
    try:
        db.execute_sql(
            "CREATE INDEX IF NOT EXISTS idx_question_source_anchor "
            "ON question(source, source_anchor)"
        )
    except OperationalError as e:
        if not _is_benign_schema_error(e):
            raise


def _015_question_figure_refs_2026_04():
    """Add ``question.figure_refs`` (JSON-serialized list of relative image
    paths) so vision-reviewable items can carry their attached figure(s)
    on the Question row itself.

    Most Princeton items are text-only; the ~105 rows flagged
    ``needs_vision=True`` by the consolidator need a persisted pointer at
    the DB level so the vision review pipeline can locate the image
    without going back through the extractor JSON every time.

    Idempotent — ALTER ADD COLUMN fails benignly if the column exists.
    """
    db = _get_db()
    try:
        db.execute_sql(
            "ALTER TABLE question ADD COLUMN figure_refs TEXT NOT NULL DEFAULT '[]'"
        )
    except OperationalError as e:
        if not _is_benign_schema_error(e):
            raise


# ── User-reported issue batch 2026-05-01 (GitHub #3–#9) ────────────────
#
# 9 Princeton-2012 data_interp items whose stimulus_type is ``graph``
# but carry no figure_refs, no render_spec, and no inlined image —
# literally unanswerable in the app (no chart ever shown). Users reported
# #6 and #7; a DB sweep found the other seven in the same bucket (all
# four-question Princeton graph clusters where the graph asset was
# never extracted).
_UNRENDERABLE_PRINCETON_GRAPH_QIDS_2026_05 = (
    4627, 4628, 4639, 4646, 4648, 4650, 4656, 4674, 4675,
)


# 3 Manhattan-5lb items whose LaTeX prompt has adjacent exponents with
# an implicit (printed-book) multiplication that KaTeX renders as a
# visual gap ("If 125^{14} 48^8 is written out..."). Readers can't tell
# whether the two terms multiply or concatenate. User reported #3 for
# qid 3267; sweep found the same pattern in qid 3253 and qid 3260.
_MANHATTAN_MISSING_TIMES_FIXES_2026_05 = (
    # (qid, original prompt substring, replacement substring)
    (3253,
     r"\(\frac{20^{-5} 5^{10} 8^6}{10^8 25^{-2}} = ?\)",
     r"\(\frac{20^{-5} \times 5^{10} \times 8^6}{10^8 \times 25^{-2}} = ?\)"),
    (3260,
     r"\(\frac{2^{-4} 3^{-20}}{4^{-1} 9^{-6}} =\)",
     r"\(\frac{2^{-4} \times 3^{-20}}{4^{-1} \times 9^{-6}} =\)"),
    (3267,
     r"\(125^{14} 48^8\)",
     r"\(125^{14} \times 48^8\)"),
)


def _016_fix_user_reported_2026_05():
    """Retire 9 Princeton DI items with no extractable graph and insert
    explicit ``\\times`` into 3 Manhattan exponent prompts.

    Idempotent: retiring already-retired rows is a no-op; the prompt
    rewrite is skipped when the replacement substring is already present
    (so re-running after a partial-apply recovery is safe).
    """
    db = _get_db()
    placeholders = ",".join("?" for _ in _UNRENDERABLE_PRINCETON_GRAPH_QIDS_2026_05)
    db.execute_sql(
        f"UPDATE question SET status='retired' "
        f"WHERE id IN ({placeholders}) AND status != 'retired'",
        _UNRENDERABLE_PRINCETON_GRAPH_QIDS_2026_05,
    )
    for qid, old_sub, new_sub in _MANHATTAN_MISSING_TIMES_FIXES_2026_05:
        row = db.execute_sql(
            "SELECT prompt FROM question WHERE id=?", (qid,)
        ).fetchone()
        if row is None:
            continue
        prompt = row[0] or ""
        if new_sub in prompt:
            continue  # already fixed
        if old_sub not in prompt:
            continue  # prompt diverged from the shipped seed; skip
        db.execute_sql(
            "UPDATE question SET prompt=? WHERE id=?",
            (prompt.replace(old_sub, new_sub), qid),
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
    ("012_synthetic_provenance_2026_04",
     _012_synthetic_provenance_2026_04),
    ("013_question_lifecycle_2026_05",
     _013_question_lifecycle_2026_05),
    ("014_source_anchor_2026_04",
     _014_source_anchor_2026_04),
    ("015_question_figure_refs_2026_04",
     _015_question_figure_refs_2026_04),
    ("016_fix_user_reported_2026_05",
     _016_fix_user_reported_2026_05),
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
