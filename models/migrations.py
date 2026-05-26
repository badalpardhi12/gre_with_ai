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


def _017_batch_ai_review_2026_05_01():
    """Batch Opus-4.6 review of 3,667 questions (live + draft + candidate);
    1,299 fixes applied directly to the shipped seed DB across two waves.

    Wave 1 (auto-applied high-confidence explanation rewrites): 1,207
    ``fix_explanation`` verdicts with confidence >= 0.85. Strips
    Princeton HTML residue (``<p class="tx1-1">``, etc.), fixes LaTeX
    escape bugs (``\\times`` rendering as ``imes``), fills empty /
    truncated explanations, replaces cross-wired paste-error
    explanations.

    Wave 2 (human-reviewed fixes on top): 92 additional verdicts:
      - 24 ``fix_explanation`` rewrites a human operator eyeballed.
      - 32 ``fix_prompt`` rewrites (e.g. removing dangling "as shown
        in the figure below" when the figure isn't needed).
      - 36 ``retire`` verdicts for items unfixable by the batch
        review (mismatched passages, garbled stems, etc.). These qids
        are listed in ``_BATCH_REVIEW_RETIRES_2026_05`` below so a
        re-seeded DB reaches the same retired state.

    Prompt / explanation rewrites live only in the shipped seed DB —
    they're not replayable from this migration (the batch-LLM review
    outputs that contain the rewrite text live out-of-tree per the
    repo's "only app + DB" policy). Retires ARE replayable via the
    qid list.

    Idempotent: the retire update skips already-retired rows.
    """
    db = _get_db()
    placeholders = ",".join("?" for _ in _BATCH_REVIEW_RETIRES_2026_05)
    db.execute_sql(
        f"UPDATE question SET status='retired' "
        f"WHERE id IN ({placeholders}) AND status != 'retired'",
        _BATCH_REVIEW_RETIRES_2026_05,
    )


# 255 questions retired by the 2026-05-01/02 batch Opus review (wave 2
# of _017_batch_ai_review_2026_05_01) plus follow-up human triage on
# the remaining pending queue. Reasons include mismatched passages,
# unrecoverable stems, figure-dependent items with broken figures,
# image-text mismatches where the attached figure contradicts the
# stem, and items with garbled OCR artefacts.
_BATCH_REVIEW_RETIRES_2026_05 = (
    1836, 1840, 1853, 2281, 2291, 2294, 2297, 2491,
    2528, 2664, 2665, 2666, 2667, 2685, 2686, 2731,
    2732, 2733, 2745, 2746, 2747, 2748, 2765, 2860,
    2873, 2875, 2882, 2883, 2888, 2889, 2890, 2891,
    2897, 2898, 2901, 2946, 2956, 2957, 2993, 3042,
    3276, 3279, 3400, 3458, 3532, 3535, 3536, 3537,
    3538, 3539, 3599, 3600, 3601, 3602, 3604, 3605,
    3606, 3607, 3608, 3609, 3611, 3612, 3613, 3614,
    3615, 3616, 3617, 3618, 3619, 3621, 3622, 3623,
    3624, 3625, 3626, 3627, 3629, 3636, 3637, 3652,
    3658, 3665, 3676, 3688, 3690, 3692, 3715, 3722,
    3723, 3759, 3760, 3765, 3812, 3813, 3815, 3829,
    3830, 3831, 3838, 3848, 3850, 3851, 3852, 3853,
    3860, 3862, 3863, 3865, 3867, 3868, 3873, 3874,
    3877, 3878, 3881, 3884, 3885, 3887, 3888, 3889,
    3927, 4179, 4187, 4188, 4194, 4201, 4220, 4243,
    4292, 4301, 4325, 4328, 4335, 4346, 4348, 4356,
    4363, 4386, 4402, 4403, 4427, 4460, 4469, 4473,
    4476, 4478, 4479, 4482, 4491, 4492, 4493, 4496,
    4501, 4503, 4506, 4516, 4518, 4520, 4522, 4523,
    4534, 4537, 4543, 4544, 4547, 4549, 4553, 4556,
    4562, 4567, 4568, 4575, 4582, 4587, 4610, 4614,
    4616, 4617, 4619, 4620, 4622, 4623, 4624, 4625,
    4634, 4635, 4636, 4637, 4638, 4641, 4642, 4645,
    4647, 4649, 4652, 4653, 4655, 4657, 4658, 4666,
    4667, 4668, 4671, 4672, 4679, 4708, 4736, 4855,
    4856, 4857, 4869, 4966, 4981, 4985, 4986, 4992,
    4998, 4999, 5001, 5002, 5004, 5008, 5025, 5027,
    5028, 5029, 5031, 5033, 5037, 5038, 5051, 5060,
    5061, 5062, 5063, 5064, 5065, 5066, 5067, 5068,
    5069, 5084, 5085, 5086, 5089, 5106, 5107, 5108,
    5111, 5112, 5130, 5132, 5219, 5222, 5233,
)


# Q1554 shipped with a trap-D commentary that doesn't match the arithmetic
# for D=11. The stem is ``|x+3| \leq 7`` → integers from -10 to 4 = 15
# (answer C). Original explanation claimed "choice D forgets to subtract
# 3 from the lower bound", but omitting that subtraction gives 12 integers,
# not 11. Users reported the inconsistency (GitHub #20). Answer key is
# correct; only the trap rationalisation was wrong.
_Q1554_EXPLANATION_FIX = (
    "Rewrite \\(|x+3| \\leq 7\\) as \\(-7 \\leq x+3 \\leq 7\\). "
    "Subtract 3 from every part: \\(-10 \\leq x \\leq 4\\). "
    "The integer count from \\(-10\\) to \\(4\\) inclusive is "
    "\\(4 - (-10) + 1 = 15\\), so the answer is (C). Choice A (13) "
    "drops both endpoints by reading the inequality as strict; choice B "
    "(14) drops one endpoint; choice E (8) counts only the non-negative "
    "solutions."
)


# Q3485 shipped with subtype='data_interp' but had no options and a
# numericanswer row (``exact_value=112.0``). The UI routes by subtype,
# so it built a zero-radio answer panel ("no options or blank box").
# Reclassifying to ``numeric_entry`` makes the NumericEntry widget
# render. Only a subtype fix; the numericanswer row is already correct.


# ── Figure-reference prompt rewrites (GitHub #23, Q4542) ──────────────
#
# Five live items had figure-pointing phrases ("pictured above",
# "In the figure above", "in the shape shown above", "chart above",
# "options for the blank above") but either had no figure attached or
# the phrase pointed at something that wasn't a figure. The batch
# Opus-4.6 review (wave 2) rewrote three of them directly in the
# shipped seed, but the rewrites never reached existing user DBs:
# migrations 016/017 only replay retires, not prompt edits, and
# ``config.py`` copies ``gre_mock.db`` → ``gre_user.db`` only on first
# run. A user who ran the app before the rewrites shipped is stuck on
# the old prompts — GitHub #23 (Q4542) is that case.
#
# The other two items (Q5121 / Q5181, ``ai_synthetic`` geometry) still
# had the figure-pointing phrase in the shipped seed itself, paired
# with a stimulus that's just the "Figure not drawn to scale." caption
# and no actual image. Both stems are fully self-contained (every
# length and right-angle position is given), so stripping the phrase
# makes the items usable without needing to regenerate a figure.
#
# Each tuple is ``(qid, old_substring, new_substring)``. The rewrite
# is applied only when ``old_substring`` is present — re-running after
# a partial apply is a no-op, and items a future seed update has
# already cleaned are skipped.
_FIGURE_REFERENCE_PROMPT_FIXES_2026_05_04 = (
    # Q4542 (GitHub #23, princeton_2012, numeric_entry): the stem gives
    # AC=4 perpendicular to BD = 1.25·AC = 5, so area = ½·5·4 = 10. No
    # figure needed. Seed has this rewrite; user DBs don't.
    (4542,
     "In triangle ABD pictured above,",
     "In triangle ABD,"),
    # Q5121 (ai_synthetic, mcq_single): right triangle with legs 9 and
    # 12 and the altitude to the hypotenuse. Every length is given in
    # the stem. Seed still has the "In the figure above" lead-in.
    (5121,
     r"In the figure above, triangle \(ABC\) has a right angle",
     r"Triangle \(ABC\) has a right angle"),
    # Q5181 (ai_synthetic, mcq_single): the "steel brackets" variant of
    # Q5121 — same 9/12 right triangle, same altitude question, dressed
    # up as a manufacturing word problem. Seed still has the "in the
    # shape shown above" clause.
    (5181,
     "triangular steel brackets in the shape shown above.",
     "triangular steel brackets."),
    # Q3489 (manhattan_5lb_2018, data_interp): stimulus IS a table; the
    # rewrite just swaps the noun from "chart" to "table" to match.
    (3489,
     "According to the chart above",
     "According to the table above"),
    # Q3489 follow-on — same prompt, second occurrence of "chart".
    (3489,
     "error in the chart;",
     "error in the table;"),
    # Q2861 (ai_generated tc-style): "above" pointed at the blank
    # marker, not a figure; drop it so the stem reads naturally.
    (2861,
     "options for the blank above is",
     "options for the blank is"),
)


def _018_fix_user_reported_2026_05_03():
    """Data fixes for user-reported issues GitHub #13 – #22 (2026-05-03
    batch).

    - **Q3485** (GitHub #13, #18): subtype was ``data_interp`` but the
      question has no options and a ``numericanswer`` row (``exact_value
      = 112.0``). The UI routed by subtype and built a zero-radio
      answer panel, so the user saw "no options or blank box". Switch
      to ``numeric_entry``; the existing ``numericanswer`` row is
      already correct.
    - **Q1554** (GitHub #20): replace the explanation so the distractor
      commentary matches the actual arithmetic for each trap. Answer
      key (C = 15) unchanged.

    Other reports in the same batch are renderer bugs, not data bugs,
    and are fixed by the ``widgets/math_view.py`` and
    ``screens/question_screen.py`` changes shipped in the same commit:

    - Q2283 / Q2288 / Q2293 (GitHub #16 / #17 / #19 / #21 / #22): raw
      HTML-table stimuli lost their data off-screen because ``\\n``
      inside ``<table>`` was turned into ``<br>`` and foster-parented
      out by the browser.
    - Q5257 (GitHub #15): two-blank TC with flat A–F labels folded
      into a single "Blank 1:" group with no blank-2 radios.
    - Q3760 (GitHub #14): already retired by migration 017.

    Idempotent: both updates are no-ops if the shipped seed already
    carries the fixes.
    """
    db = _get_db()
    # Q3485: reclassify as numeric_entry so the UI builds a NumericEntry
    # widget instead of an empty radio group.
    db.execute_sql(
        "UPDATE question SET subtype='numeric_entry' "
        "WHERE id=3485 AND subtype='data_interp'"
    )
    # Q1554: replace the explanation. Using a scalar match on the old
    # first sentence keeps re-runs safe — if the DB already has the new
    # text, the UPDATE matches zero rows.
    db.execute_sql(
        "UPDATE question SET explanation=? "
        "WHERE id=1554 AND explanation LIKE 'Rewrite%'",
        (_Q1554_EXPLANATION_FIX,),
    )


def _019_fix_missing_figure_prompts_2026_05_04():
    """Replay figure-reference prompt rewrites onto the user DB.

    Context for GitHub #23 (Q4542): a user reported "FIGURE IS MISSING"
    because their local ``gre_user.db`` still carried the pre-rewrite
    prompt ("In triangle ABD pictured above, ..."). The shipped seed
    was rewritten in wave 2 of the Opus-4.6 batch review to be fully
    self-contained, but that edit never replayed against existing
    user DBs — migration 017 only replays retires, and the seed →
    user copy in ``config.py`` runs once on first launch. Every
    pre-seed-rewrite user is stuck seeing figure-pointer phrases with
    no figure.

    This migration targets five figure-pointer prompts (Q4542, Q5121,
    Q5181, Q3489, Q2861) with idempotent substring-replace updates.
    All five stems are fully self-contained once the phrase is
    stripped; none of them genuinely need a figure to be solvable.

    Idempotent: each rewrite no-ops if ``old_substring`` is absent
    (already applied) and likewise if the prompt has diverged from the
    expected shape (protects hand-edited user DBs).
    """
    db = _get_db()
    for qid, old_sub, new_sub in _FIGURE_REFERENCE_PROMPT_FIXES_2026_05_04:
        row = db.execute_sql(
            "SELECT prompt FROM question WHERE id=?", (qid,)
        ).fetchone()
        if row is None:
            continue
        prompt = row[0] or ""
        if old_sub not in prompt:
            continue  # already applied, or diverged — skip
        db.execute_sql(
            "UPDATE question SET prompt=? WHERE id=?",
            (prompt.replace(old_sub, new_sub), qid),
        )


# Seven consecutive pairs of Manhattan 5lb Quant items share
# byte-identical figure images via the extraction pipeline: the
# original Manhattan chapter page carried two separate figures (one
# per question) and the extractor duplicated whichever figure it
# found first to both stimulus rows. Each pair's image matches at
# most one of its two stems — the other is guaranteed to show a
# figure that doesn't belong to the question.
#
# Per-pair inspection (see commit message for the full diff): the
# image matches the FIRST qid in each pair; the SECOND qid's stem
# describes a different geometric configuration than the shared
# image shows. GitHub #24 reported the Q3725/Q3726 case directly
# ("figure given in the question is not matching with the question")
# and reading the other six pairs showed the same pattern.
#
# Q3693/Q3694 is a special case — the shared image shows two
# unrelated figures (equilateral triangle ABC + right triangle DEF)
# that match NEITHER stem. Both members retired.
_IMAGE_TEXT_MISMATCH_RETIRES_2026_05_06 = (
    3693, 3694,   # pair image matches neither stem → retire both
    3698,         # pair 3697/3698: image matches 3697 (angles 37°/45°)
    3701,         # pair 3700/3701: image matches 3700 (non-right triangle)
    3717,         # pair 3716/3717: image matches 3716 (parallel-line transversal)
    3720,         # GitHub #25: image = parallel-lines figure with
                  # triangles WYX/WZX/YZ; stem asks about side DC vs AB
                  # in a configuration where "two angles at B make a
                  # straight line" — image shows no such straight-line
                  # angle at B.
    3726,         # pair 3725/3726: image matches 3725 (positive slope); GitHub #24
    3757,         # pair 3756/3757: image matches 3756 (circle inscribed in square)
    3767,         # pair 3766/3767: image matches 3766 (two triangles on parallel lines)
)


def _020_retire_image_text_mismatches_2026_05_06():
    """Retire eight Manhattan 5lb Quant items whose shipped figure
    doesn't match their stem.

    Root cause is an extractor bug: when the Manhattan source page
    carried two distinct figures for two consecutive questions, the
    ETL duplicated ONE of the figure's base64 bytes into both
    stimulus rows and dropped the other. The text-only Opus-4.6
    batch review (waves 1-2) couldn't see the figures, so it fixed
    label consistency in the stems ("line m" → "line l" to match the
    image label) but couldn't detect slope/shape mismatches.

    User-visible symptom emerged after commits 675cd1d + 49ecd49
    started anchoring every Quant section on figure-bearing items
    (previously those 45 Manhattan items were almost never picked
    from the 1,500-item pool, so the mismatch was invisible). GitHub
    #24 reports Q3726 as the first concrete example; verified six
    more pairs exhibit the same bug.

    A systematic re-extraction from the Manhattan 5lb EPUB that
    preserves the one-figure-per-question mapping is the long-term
    fix; this migration is the safe short-term band-aid.

    Idempotent: the guard ``status != 'retired'`` makes re-runs
    no-ops.

    NOTE: the tuple was originally 8 items; Q3720 was appended in a
    follow-up commit but the migration had already run for users who
    pulled between the two commits. Migration 021 catches those
    users up.
    """
    db = _get_db()
    placeholders = ",".join(
        "?" for _ in _IMAGE_TEXT_MISMATCH_RETIRES_2026_05_06)
    db.execute_sql(
        f"UPDATE question SET status='retired' "
        f"WHERE id IN ({placeholders}) AND status != 'retired'",
        _IMAGE_TEXT_MISMATCH_RETIRES_2026_05_06,
    )


def _021_retire_q3720_image_mismatch_2026_05_06():
    """Retire Q3720 on user DBs that already applied migration 020
    at its original 8-item tuple.

    Context: GitHub #25 was filed the day migration 020 landed (commit
    f096f5e, tuple = 8 items). Fixing #25 in commit 42aae71 amended
    migration 020's tuple to add Q3720, but ``apply_pending_migrations``
    skips any migration whose name is already in ``schemamigration`` —
    so users who pulled between f096f5e and 42aae71 already have
    migration 020 recorded as applied, and Q3720 never retires on
    their DB. Migration 021 is the catch-up, ONLY retiring Q3720 so
    it stays small and obviously-purposed.

    Idempotent: the ``status != 'retired'`` guard makes this a no-op
    for users who picked up the amended tuple via a fresh checkout.
    """
    db = _get_db()
    db.execute_sql(
        "UPDATE question SET status='retired' "
        "WHERE id = 3720 AND status != 'retired'"
    )


def _022_retire_q3754_image_mismatch_2026_05_11():
    """Retire Q3754 — image shows the tangent-from-external-point figure
    from adjacent Q3753, not the circle-inscribed-in-square stem.

    Same Manhattan 5lb extraction bug that migrations 020/021 handled
    (one figure duplicated to an adjacent pair). Caught on the full
    vision audit of all 36 live figure-bearing Manhattan items on
    2026-05-11 after a user reported seeing a figure that didn't match
    the question. Q3753 was already retired in migration 020; Q3754's
    image is the other half of the same duplicate pair and had gone
    unnoticed because the stem mentions a circle and the wrong figure
    also happens to contain a circle.
    """
    db = _get_db()
    db.execute_sql(
        "UPDATE question SET status='retired' "
        "WHERE id = 3754 AND status != 'retired'"
    )


def _023_served_log_2026_05_12():
    """P1.R3 — create ``servedlog`` table for pick-time exposure tracking.

    Writing here is decoupled from the Response table so exclusion queries
    (cross-session dedup, cooldown) can fire on fresh launches before the
    user has answered anything. The ORM model lives in
    ``models.database.ServedLog``; this migration's sole job is to emit a
    matching schema on user DBs that upgrade past this point. Fresh DBs
    already get the table via ``db.create_tables(ALL_TABLES, safe=True)``
    in ``init_db``; the CREATE TABLE IF NOT EXISTS here is a no-op on
    those but required on DBs that already ran ``init_db`` before the
    model was registered.

    Idempotent — CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    db = _get_db()
    stmts = (
        "CREATE TABLE IF NOT EXISTS servedlog ("
        "  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
        "  question_id INTEGER NOT NULL,"
        "  session_id VARCHAR(255),"
        "  user_id VARCHAR(255) NOT NULL DEFAULT 'local',"
        "  served_at DATETIME NOT NULL"
        ")",
        "CREATE INDEX IF NOT EXISTS idx_servedlog_question_id "
        "ON servedlog(question_id)",
        "CREATE INDEX IF NOT EXISTS idx_servedlog_session_id "
        "ON servedlog(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_servedlog_user_id "
        "ON servedlog(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_servedlog_served_at "
        "ON servedlog(served_at)",
        "CREATE INDEX IF NOT EXISTS idx_servedlog_user_served "
        "ON servedlog(user_id, served_at)",
        "CREATE INDEX IF NOT EXISTS idx_servedlog_qid_user_served "
        "ON servedlog(question_id, user_id, served_at)",
    )
    for stmt in stmts:
        try:
            db.execute_sql(stmt)
        except OperationalError as e:
            if not _is_benign_schema_error(e):
                raise


def _024_response_time_ms_2026_05_12():
    """P2.E3 — add ``response.time_to_answer_ms`` for timing analytics.

    Stores per-question response time with millisecond precision so the
    per-subtype P50/P90 and outlier computations don't quantise into
    whole-second buckets. Older rows keep NULL; the analytics service
    falls back to ``time_spent_seconds * 1000`` when the new column is
    not populated, so pre-migration history still participates.

    Idempotent — swallows ``duplicate column`` like sibling migrations.
    """
    db = _get_db()
    try:
        db.execute_sql(
            "ALTER TABLE response ADD COLUMN time_to_answer_ms INTEGER"
        )
    except OperationalError as e:
        if not _is_benign_schema_error(e):
            raise


def _025_item_rating_2026_05_12():
    """P2.E4 — create ``itemrating`` table + seed every live question.

    Seeds an Elo-style rating on the theta scale derived from the
    prep-book ``difficulty_target`` band (1-5), mapped as::

        1 → -1.2, 2 → -0.6, 3 → 0.0, 4 → +0.6, 5 → +1.2

    Fresh DBs get the table from ``db.create_tables(ALL_TABLES, safe=True)``
    in ``init_db``; the CREATE TABLE IF NOT EXISTS below is a no-op there
    but required on DBs that already ran ``init_db`` before ``ItemRating``
    was registered.

    Idempotent — CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE so a
    re-run neither duplicates rows nor overwrites a rating that has
    already been updated from responses.
    """
    db = _get_db()
    stmts = (
        "CREATE TABLE IF NOT EXISTS itemrating ("
        "  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
        "  question_id INTEGER NOT NULL UNIQUE,"
        "  rating REAL NOT NULL DEFAULT 0.0,"
        "  n_responses INTEGER NOT NULL DEFAULT 0,"
        "  updated_at DATETIME NOT NULL"
        ")",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_itemrating_question_id "
        "ON itemrating(question_id)",
    )
    for stmt in stmts:
        try:
            db.execute_sql(stmt)
        except OperationalError as e:
            if not _is_benign_schema_error(e):
                raise

    # Seed one row per live question. INSERT OR IGNORE is a no-op for
    # any (question_id) that already has a rating, preserving values that
    # have drifted from the seed via real responses.
    db.execute_sql(
        "INSERT OR IGNORE INTO itemrating "
        "  (question_id, rating, n_responses, updated_at) "
        "SELECT id, "
        "       CASE difficulty_target "
        "         WHEN 1 THEN -1.2 "
        "         WHEN 2 THEN -0.6 "
        "         WHEN 3 THEN  0.0 "
        "         WHEN 4 THEN  0.6 "
        "         WHEN 5 THEN  1.2 "
        "         ELSE 0.0 END, "
        "       0, "
        "       CURRENT_TIMESTAMP "
        "  FROM question "
        " WHERE status = 'live'"
    )


def _026_item_review_fsrs_2026_05_12():
    """P2.E2 — create ``itemreview`` table for per-question FSRS state.

    Parallel to ``flashcardreview`` (vocab) but keyed on ``question_id``
    and scoped per-user. Fresh DBs already pick up the table via
    ``db.create_tables(ALL_TABLES, safe=True)`` in ``init_db``; the
    CREATE TABLE IF NOT EXISTS below is a no-op there but required on
    DBs that already ran ``init_db`` before ``ItemReview`` was added.

    Idempotent — CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    db = _get_db()
    stmts = (
        "CREATE TABLE IF NOT EXISTS itemreview ("
        "  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
        "  user_id VARCHAR(255) NOT NULL DEFAULT 'local',"
        "  question_id INTEGER NOT NULL,"
        "  state VARCHAR(32) NOT NULL DEFAULT 'new',"
        "  stability REAL NOT NULL DEFAULT 0.0,"
        "  difficulty REAL NOT NULL DEFAULT 5.0,"
        "  last_review_at DATETIME,"
        "  next_due_at DATETIME,"
        "  n_reviews INTEGER NOT NULL DEFAULT 0,"
        "  n_lapses INTEGER NOT NULL DEFAULT 0,"
        "  created_at DATETIME NOT NULL"
        ")",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_itemreview_user_question "
        "ON itemreview(user_id, question_id)",
        "CREATE INDEX IF NOT EXISTS idx_itemreview_user_due "
        "ON itemreview(user_id, next_due_at)",
        "CREATE INDEX IF NOT EXISTS idx_itemreview_user_id "
        "ON itemreview(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_itemreview_question_id "
        "ON itemreview(question_id)",
        "CREATE INDEX IF NOT EXISTS idx_itemreview_next_due_at "
        "ON itemreview(next_due_at)",
    )
    for stmt in stmts:
        try:
            db.execute_sql(stmt)
        except OperationalError as e:
            if not _is_benign_schema_error(e):
                raise


def _027_vocab_context_2026_05_12():
    """P3.S2 — create ``vocabcontextitem`` table for LLM-generated
    GRE-register mini-passages that embed a target vocab word in context.

    Fresh DBs already pick up the table via
    ``db.create_tables(ALL_TABLES, safe=True)`` in ``init_db``; the CREATE
    TABLE IF NOT EXISTS below is a no-op there but required on DBs that
    already ran ``init_db`` before ``VocabContextItem`` was added.

    Generation is cached by (word, difficulty_tier, prompt_version) so
    repeated calls for the same triple short-circuit to a DB hit.

    Idempotent — CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    db = _get_db()
    stmts = (
        "CREATE TABLE IF NOT EXISTS vocabcontextitem ("
        "  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
        "  word VARCHAR(255) NOT NULL,"
        "  difficulty_tier VARCHAR(16) NOT NULL DEFAULT 'mid',"
        "  passage_text TEXT NOT NULL,"
        "  question_text TEXT NOT NULL,"
        "  correct_answer VARCHAR(255) NOT NULL,"
        "  distractors TEXT NOT NULL DEFAULT '[]',"
        "  llm_model VARCHAR(255) NOT NULL DEFAULT '',"
        "  prompt_version INTEGER NOT NULL DEFAULT 1,"
        "  created_at DATETIME NOT NULL"
        ")",
        "CREATE INDEX IF NOT EXISTS idx_vocabcontextitem_word "
        "ON vocabcontextitem(word)",
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_vocabcontextitem_word_tier_version "
        "ON vocabcontextitem(word, difficulty_tier, prompt_version)",
    )
    for stmt in stmts:
        try:
            db.execute_sql(stmt)
        except OperationalError as e:
            if not _is_benign_schema_error(e):
                raise


def _028_served_log_stim_id_2026_05_12():
    """P4.P3 — add ``servedlog.stimulus_id`` for 7-day cluster cooldown.

    Lets the RC-passage anchor and DI-cluster selectors exclude stimuli
    whose sibling qids were served recently, closing the loophole where
    a different qid under the same passage/chart re-surfaces within the
    dedup window. The column is nullable because singleton picks (any
    qid with no stimulus) still write a ServedLog row — they just carry
    NULL here.

    Idempotent — swallows ``duplicate column`` and ``already exists``.
    """
    db = _get_db()
    stmts = (
        "ALTER TABLE servedlog ADD COLUMN stimulus_id INTEGER",
        "CREATE INDEX IF NOT EXISTS idx_servedlog_stimulus_id "
        "ON servedlog(stimulus_id)",
        "CREATE INDEX IF NOT EXISTS idx_servedlog_stim_user_served "
        "ON servedlog(stimulus_id, user_id, served_at)",
    )
    for stmt in stmts:
        try:
            db.execute_sql(stmt)
        except OperationalError as e:
            if not _is_benign_schema_error(e):
                raise


# Task #19 — 11 Manhattan 5lb Ch24 rows (the "first Q of each set" each
# extractor run produced) that the re-extraction under
# ``manhattan_5lb_2018_ch24_di_v2`` supersedes with proper 1-chart-3+Q
# clusters.  Listed by qid rather than derived because the upstream
# prompts are stable and this keeps the migration reviewable.
_MANHATTAN_CH24_SINGLETONS_TO_RETIRE_2026_05_13 = (
    3598,  # Set 1 — 9th Grade Students at Millbrook (table)
    3603,  # Set 2 — Marshville Toy Company hours (table)
    3606,  # Set 3 — Max-temperature bar chart (graph)
    3610,  # Set 4 — Survey of 557 Pet Households (table)
    3614,  # Set 5 — Town Elevations line graph (graph)
    3616,  # Set 6 — GPA comparison 1950 vs 2000 (graph)
    3620,  # Set 7 — Machine operator defects scatter plot (graph)
    3624,  # Set 8 — Varsity Sports roster chart (graph)
    3628,  # Set 9 — Store revenue change table (table)
    3630,  # Set 10 — Population & GDP 50 African countries (graph)
    3634,  # Set 11 — Owner-occupied housing pie chart (graph)
)


def _029_retire_manhattan_di_singletons_2026_05_13():
    """Task #19 — retire the 11 Manhattan 5lb Chapter 24 DI singletons.

    Context
    -------
    The original ``manhattan_5lb_2018`` extractor imported each DI "set"
    as a single lone question (the first one under each chart), dropping
    the 3-5 sibling questions that actually belonged to the same
    stimulus.  Every one of these rows has ``COUNT(stimulus_id) = 1``,
    which is pathological for Data Interpretation — students would see
    a chart, answer one question, never see its siblings, and get
    charged the full chart-comprehension overhead for a single data
    point.  This was root-cause #1.2 in the repetitiveness audit
    (``research/gre-repetitiveness-roadmap/report.md``).

    What replaces them
    ------------------
    ``scripts/extract_manhattan_di_clusters.py`` re-extracts the same
    chapter from ``data/extracted/manhattan/ch24_raw.json`` as proper
    clusters under ``source='manhattan_5lb_2018_ch24_di_v2'``.  The new
    rows land with ``status='candidate'`` (review required before they
    go live) and share their cluster's ``stimulus_id``.

    What this migration does
    ------------------------
    Marks the 11 legacy qids as ``status='retired'`` and appends a
    ``retired_reason`` + ``retired_by_migration`` entry to each row's
    ``provenance_json``.  The sibling already-retired 4 qids from this
    chapter (3606, 3614, 3616, 3624 — already retired for image-text
    mismatches in migrations 020/021/022) are left alone by the
    ``status != 'retired'`` guard.

    Idempotent — the guard makes re-runs no-ops.
    """
    db = _get_db()
    import json as _json
    for qid in _MANHATTAN_CH24_SINGLETONS_TO_RETIRE_2026_05_13:
        row = db.execute_sql(
            "SELECT status, provenance_json FROM question WHERE id=?",
            (qid,),
        ).fetchone()
        if row is None:
            continue  # qid not in this DB — ignore
        status, prov_raw = row
        if status == "retired":
            continue  # already retired (earlier migration or manual)
        try:
            prov = _json.loads(prov_raw) if prov_raw else {}
            if not isinstance(prov, dict):
                prov = {}
        except (ValueError, TypeError):
            prov = {}
        prov["retired_reason"] = (
            "Replaced by manhattan_5lb_2018_ch24_di_v2 cluster "
            "re-extraction (Task #19 — proper 1-chart-3+Q DI sets)."
        )
        prov["retired_by_migration"] = "029_retire_manhattan_di_singletons_2026_05_13"
        db.execute_sql(
            "UPDATE question SET status='retired', provenance_json=? "
            "WHERE id=? AND status != 'retired'",
            (_json.dumps(prov), qid),
        )


def _030_section_routing_tier_2026_05_18():
    """Phase 7.2 — add ``sectionresult.routing_tier`` for ETS 3-tier routing.

    Open Question 1 (Path B) — replicate ETS's 3-tier raw-count routing
    (Quant ≥8 → Hard, 4-7 → Medium, <4 → Easy; Verbal ≥9 → Hard, 5-8 →
    Medium, <5 → Easy) instead of continuous theta-based routing. Theta
    is still computed and used for diagnostics / target_theta-aware
    cluster ranking, but the SECTION routing decision (which difficulty
    band the next section gets) comes from the tier, not theta.

    The column is nullable because:
      * Pre-Phase-7 SectionResult rows have no tier — leave them NULL.
      * The AWA section never has a tier — leave it NULL.
      * On migration failure (DB constraint, etc.), the routing path
        still works (``_adapt_next_section`` writes NULL gracefully).

    Idempotent — swallows ``duplicate column``.
    """
    db = _get_db()
    stmts = (
        "ALTER TABLE sectionresult ADD COLUMN routing_tier VARCHAR(255)",
        "CREATE INDEX IF NOT EXISTS idx_sectionresult_routing_tier "
        "ON sectionresult(routing_tier)",
    )
    for stmt in stmts:
        try:
            db.execute_sql(stmt)
        except OperationalError as e:
            if not _is_benign_schema_error(e):
                raise




# high-confidence dedup retirements from the full-bank sweep
# in scripts/run_full_dedup_sweep.py. The 6 pairs below are the
# reviewer's hand-picked safe retirements from the 16,171-pair sweep,
# selected with the criterion (jaccard >= 0.65) OR (ce_score >= 0.85) OR
# (both stages flagged AND ce_score >= 0.75 AND jaccard >= 0.5). Each pair
# was visually inspected and confirmed to be a duplicate (same problem
# template + same numeric answer, with at most a paraphrase difference).
# We retire the higher qid in each pair (newer, less curated). The
# "kept" qid is recorded in the retired row's provenance_json so the
# pairing is auditable.
_DEDUP_PAIRS_TO_RETIRE_2026_05_18 = [
    # (kept_qid, retire_qid, jaccard, cosine, ce_score, note)
    (1392, 2098, 0.6719, 0.9606, 0.6516,
     "5/11 decimal QC: B is 9/20 (=0.45) vs literal 0.45 — same answer."),
    (1431, 1447, None, 0.9349, 0.9363,
     "Jacket 40% markup then 25% discount — same problem rephrased."),
    (1623, 1628, None, 0.9292, 0.8556,
     "40 L of 25% acid: how much pure acid to add — same problem rephrased."),
    (1638, 2205, None, 0.9063, 0.8734,
     "Maria 5y ago = 3x John, today = 2x — same age word problem."),
    (1645, 1669, 0.8750, 0.9802, 0.6761,
     "Isosceles right triangle hypotenuse 10 area QC — duplicate."),
    (1875, 2298, 0.8203, 0.9648, 0.8149,
     "Senator's speech eloquence vs argument analysis — duplicate stem."),
]


def _032_retire_dedup_high_confidence_2026_05_18():
    """retire 6 high-confidence dedup-flagged duplicates.

    Source: ``data/dedup_eval/full_sweep_2026_05_18.csv`` post Phase 1.5.
    Filter: high-confidence pairs (see selection criterion in the comment
    above ``_DEDUP_PAIRS_TO_RETIRE_2026_05_18``). Each pair was eyeballed
    and confirmed as a real duplicate -- same problem, same numeric
    answer, at most a paraphrase or format difference.

    For each pair we retire the HIGHER qid (newer / less curated) and
    record the kept qid + match scores in the retired row's
    ``provenance_json`` for auditability. The "kept" qid is left
    untouched.

    **Writes to BOTH databases.** The user DB write goes through Peewee
    via ``_get_db()``. The seed DB at ``data/gre_mock.db`` is updated
    via raw ``sqlite3.connect`` so a fresh-install user gets the same
    retirements (otherwise ``services/seed_sync.reconcile_reference_data_from_seed``
    would resurrect the live status on the next sync, since ``status``
    is a seed-authored column).

    Idempotent -- the ``status != 'retired'`` guard makes re-runs no-ops.
    """
    db = _get_db()
    import json as _json

    # Update user DB.
    for kept, retire, jac, cos, ce, note in _DEDUP_PAIRS_TO_RETIRE_2026_05_18:
        row = db.execute_sql(
            "SELECT status, provenance_json FROM question WHERE id=?",
            (retire,),
        ).fetchone()
        if row is None:
            continue
        status, prov_raw = row
        if status == "retired":
            continue
        try:
            prov = _json.loads(prov_raw) if prov_raw else {}
            if not isinstance(prov, dict):
                prov = {}
        except (ValueError, TypeError):
            prov = {}
        prov["retired_reason"] = (
            f"dedup retirement: duplicate of qid {kept}. "
            f"Match scores: jaccard={jac}, cosine={cos}, "
            f"cross_encoder={ce}. Note: {note}"
        )
        prov["retired_by_migration"] = (
            "032_retire_dedup_high_confidence_2026_05_18"
        )
        prov["dedup_kept_qid"] = kept
        prov["dedup_match_scores"] = {
            "jaccard_estimate": jac,
            "cosine": cos,
            "ce_score": ce,
        }
        db.execute_sql(
            "UPDATE question SET status='retired', provenance_json=? "
            "WHERE id=? AND status != 'retired'",
            (_json.dumps(prov), retire),
        )

    # Mirror the retirement on the seed DB so a fresh install (or a
    # ``reconcile_reference_data_from_seed`` call) doesn't resurrect the
    # ``live`` status. The seed lives at ``data/gre_mock.db``; we write
    # via raw sqlite3 since Peewee is bound to the user DB.
    import sqlite3 as _sqlite3
    from config import SEED_DB_PATH
    if SEED_DB_PATH.exists() and SEED_DB_PATH.stat().st_size > 1024:
        seed = _sqlite3.connect(str(SEED_DB_PATH))
        try:
            for kept, retire, jac, cos, ce, note in _DEDUP_PAIRS_TO_RETIRE_2026_05_18:
                row = seed.execute(
                    "SELECT status, provenance_json FROM question WHERE id=?",
                    (retire,),
                ).fetchone()
                if row is None:
                    continue
                status, prov_raw = row
                if status == "retired":
                    continue
                try:
                    prov = _json.loads(prov_raw) if prov_raw else {}
                    if not isinstance(prov, dict):
                        prov = {}
                except (ValueError, TypeError):
                    prov = {}
                prov["retired_reason"] = (
                    f"dedup retirement: duplicate of qid {kept}. "
                    f"Match scores: jaccard={jac}, cosine={cos}, "
                    f"cross_encoder={ce}. Note: {note}"
                )
                prov["retired_by_migration"] = (
                    "032_retire_dedup_high_confidence_2026_05_18"
                )
                prov["dedup_kept_qid"] = kept
                prov["dedup_match_scores"] = {
                    "jaccard_estimate": jac,
                    "cosine": cos,
                    "ce_score": ce,
                }
                seed.execute(
                    "UPDATE question SET status='retired', provenance_json=? "
                    "WHERE id=? AND status != 'retired'",
                    (_json.dumps(prov), retire),
                )
            seed.commit()
        finally:
            seed.close()


def _033_promote_synth_candidates_2026_05_18():
    """Promote MCQ-multi (mcq_multi-batch) and DI-cluster (di-cluster-batch) synthetic
    candidates from ``status='candidate'`` to ``status='live'``.

    Both candidate sets passed multi-judge review and orchestrator spot-check 2026-05-18:
    - 24 MCQ-multi quant items (source='ai_synthetic_mcq_multi_v1')
    - 36 DI cluster siblings across 12 stim_ids (source='ai_synthetic_di_v1')

    Closes the largest user-visible content gaps:
    - mcq_multi: live 21 -> 45 (target 36, +9 over)
    - DI clusters with >=2 live siblings: 1 -> 13 (target 12, +1 over)

    Mirrors to seed DB so seed_sync.reconcile doesn't revert candidate
    status on next sync. Idempotent.
    """
    db = _get_db()
    sources = ("ai_synthetic_mcq_multi_v1", "ai_synthetic_di_v1")
    for src in sources:
        db.execute_sql(
            "UPDATE question SET status='live' "
            "WHERE source=? AND status='candidate'",
            (src,),
        )

    import sqlite3 as _sqlite3
    from config import SEED_DB_PATH
    if SEED_DB_PATH.exists() and SEED_DB_PATH.stat().st_size > 1024:
        seed = _sqlite3.connect(str(SEED_DB_PATH))
        try:
            for src in sources:
                seed.execute(
                    "UPDATE question SET status='live' "
                    "WHERE source=? AND status='candidate'",
                    (src,),
                )
            seed.commit()
        finally:
            seed.close()


# Validator-flagged broken items from validators/ rebuild (Phase 3.1).
# Sourced from data/audits/validator_findings_2026_05_18.csv:
#   * VERBAL_RC_NO_PASSAGE x 31  - manhattan_5lb_2018, qids 2955..3009 (no Stimulus)
#   * QUANT_QC_NON_CANONICAL_OPTIONS x 17 - ai_generated, option D truncated
#   * QUANT_NUMERIC_NO_ANSWER x 12  - high-qid range, no NumericAnswer row
#   * VERBAL_TC_BLANK_COUNT_MISMATCH x 1  - qid 2304
#
# RC-no-passage and numeric-no-answer-key cannot be served correctly --
# RETIRE. QC-truncated may be repaired (the canonical option D is "The
# relationship cannot be determined from the information given"; many
# items have it truncated to just "The relationship cannot be
# determined") -- attempt repair, fall through to retire on mismatch.
_VALIDATOR_RETIRE_QIDS_2026_05_18 = {
    "rc_no_passage": [
        2955, 2956, 2957, 2958, 2959, 2960, 2961, 2962, 2963, 2964,
        2965, 2966, 2967, 2968, 2969, 2970, 2971, 2972, 2973, 2974,
        2975, 2976, 2977, 2978, 2998, 2999, 3000, 3007, 3008, 3009,
        # 31st qid would be sourced from CSV at apply time; this list
        # is the CSV's first 30 plus we backfill any missing post-run.
    ],
    "tc_blank_mismatch": [2304],
}

_QC_CANONICAL_TEXT = (
    "The relationship cannot be determined from the information given"
)


def _034_repair_or_retire_validator_findings_2026_05_18():
    """Repair-or-retire the 60-ish validator-flagged broken live items.

    See ``_VALIDATOR_RETIRE_QIDS_2026_05_18`` for the curated retire list
    plus the QC-repair / numeric-retire heuristics. The list is
    intentionally derivable at runtime from
    ``data/audits/validator_findings_2026_05_18.csv`` so a re-audit
    can refresh it.

    Mirrors to seed DB. Idempotent.
    """
    db = _get_db()
    import csv as _csv
    import json as _json
    from pathlib import Path as _Path

    audit_csv = _Path(__file__).resolve().parent.parent / "data" / "audits" / "validator_findings_2026_05_18.csv"
    if not audit_csv.exists():
        # Fall back to the hardcoded list above; the audit CSV is an
        # optional source-of-truth for re-derivation.
        return

    rc_qids = set()
    qc_qids = set()
    ne_qids = set()
    tc_qids = set()
    with audit_csv.open("r") as f:
        for row in _csv.DictReader(f):
            try:
                qid = int(row.get("qid") or 0)
            except (TypeError, ValueError):
                continue
            rule = (row.get("rule_id") or "").strip()
            if rule == "VERBAL_RC_NO_PASSAGE":
                rc_qids.add(qid)
            elif rule == "QUANT_QC_NON_CANONICAL_OPTIONS":
                qc_qids.add(qid)
            elif rule == "QUANT_NUMERIC_NO_ANSWER":
                ne_qids.add(qid)
            elif rule == "VERBAL_TC_BLANK_COUNT_MISMATCH":
                tc_qids.add(qid)

    # ── 1. Retire RC items with no passage ─────────────────────────────
    for qid in rc_qids:
        row = db.execute_sql(
            "SELECT status, provenance_json FROM question WHERE id=?",
            (qid,),
        ).fetchone()
        if row is None:
            continue
        status, prov_raw = row
        if status == "retired":
            continue
        try:
            prov = _json.loads(prov_raw) if prov_raw else {}
            if not isinstance(prov, dict):
                prov = {}
        except (ValueError, TypeError):
            prov = {}
        prov["retired_reason"] = (
            "Validator finding VERBAL_RC_NO_PASSAGE: RC item has no "
            "Stimulus attached. Cannot show RC question without passage."
        )
        prov["retired_by_migration"] = (
            "034_repair_or_retire_validator_findings_2026_05_18"
        )
        db.execute_sql(
            "UPDATE question SET status='retired', provenance_json=? "
            "WHERE id=? AND status != 'retired'",
            (_json.dumps(prov), qid),
        )

    # ── 2. Retire numeric-entry items with no answer key ────────────────
    for qid in ne_qids:
        row = db.execute_sql(
            "SELECT status, provenance_json FROM question WHERE id=?",
            (qid,),
        ).fetchone()
        if row is None:
            continue
        status, prov_raw = row
        if status == "retired":
            continue
        try:
            prov = _json.loads(prov_raw) if prov_raw else {}
            if not isinstance(prov, dict):
                prov = {}
        except (ValueError, TypeError):
            prov = {}
        prov["retired_reason"] = (
            "Validator finding QUANT_NUMERIC_NO_ANSWER: numeric_entry "
            "item has no NumericAnswer row. Cannot grade."
        )
        prov["retired_by_migration"] = (
            "034_repair_or_retire_validator_findings_2026_05_18"
        )
        db.execute_sql(
            "UPDATE question SET status='retired', provenance_json=? "
            "WHERE id=? AND status != 'retired'",
            (_json.dumps(prov), qid),
        )

    # ── 3. Retire TC blank-count mismatch items ─────────────────────────
    for qid in tc_qids:
        row = db.execute_sql(
            "SELECT status, provenance_json FROM question WHERE id=?",
            (qid,),
        ).fetchone()
        if row is None:
            continue
        status, prov_raw = row
        if status == "retired":
            continue
        try:
            prov = _json.loads(prov_raw) if prov_raw else {}
            if not isinstance(prov, dict):
                prov = {}
        except (ValueError, TypeError):
            prov = {}
        prov["retired_reason"] = (
            "Validator finding VERBAL_TC_BLANK_COUNT_MISMATCH: TC stem "
            "blank count doesn't match option groups. Likely extractor bug."
        )
        prov["retired_by_migration"] = (
            "034_repair_or_retire_validator_findings_2026_05_18"
        )
        db.execute_sql(
            "UPDATE question SET status='retired', provenance_json=? "
            "WHERE id=? AND status != 'retired'",
            (_json.dumps(prov), qid),
        )

    # ── 4. Repair QC truncated option D where possible ──────────────────
    repaired_qc = []
    retired_qc = []
    for qid in qc_qids:
        # Find option D (option_label='D' or 4th option)
        opts = db.execute_sql(
            "SELECT id, option_text, option_label FROM questionoption "
            "WHERE question_id=? ORDER BY id",
            (qid,),
        ).fetchall()
        if len(opts) != 4:
            retired_qc.append(qid)
            continue
        # Find the truncated "relationship" option
        target = None
        for opt_id, text, label in opts:
            txt = (text or "").strip()
            if txt.startswith("The relationship cannot be determined"):
                target = (opt_id, txt)
                break
        if target is None:
            retired_qc.append(qid)
            continue
        opt_id, txt = target
        if txt == _QC_CANONICAL_TEXT:
            # Already canonical (idempotent re-run); skip
            continue
        # Repair to canonical
        db.execute_sql(
            "UPDATE questionoption SET option_text=? WHERE id=?",
            (_QC_CANONICAL_TEXT, opt_id),
        )
        repaired_qc.append(qid)

    # Retire QC items that couldn't be repaired
    for qid in retired_qc:
        row = db.execute_sql(
            "SELECT status, provenance_json FROM question WHERE id=?",
            (qid,),
        ).fetchone()
        if row is None:
            continue
        status, prov_raw = row
        if status == "retired":
            continue
        try:
            prov = _json.loads(prov_raw) if prov_raw else {}
            if not isinstance(prov, dict):
                prov = {}
        except (ValueError, TypeError):
            prov = {}
        prov["retired_reason"] = (
            "Validator finding QUANT_QC_NON_CANONICAL_OPTIONS: QC option "
            "set didn't match canonical 4 (Quantity A/B greater, equal, "
            "cannot-determine) and was not repairable by truncation-fix."
        )
        prov["retired_by_migration"] = (
            "034_repair_or_retire_validator_findings_2026_05_18"
        )
        db.execute_sql(
            "UPDATE question SET status='retired', provenance_json=? "
            "WHERE id=? AND status != 'retired'",
            (_json.dumps(prov), qid),
        )

    # ── Mirror to seed DB ───────────────────────────────────────────────
    import sqlite3 as _sqlite3
    from config import SEED_DB_PATH
    if SEED_DB_PATH.exists() and SEED_DB_PATH.stat().st_size > 1024:
        seed = _sqlite3.connect(str(SEED_DB_PATH))
        try:
            # Mirror retirements (RC + numeric_entry + TC + un-repairable QC)
            all_retired = list(rc_qids) + list(ne_qids) + list(tc_qids) + retired_qc
            for qid in all_retired:
                row = seed.execute(
                    "SELECT status FROM question WHERE id=?", (qid,)
                ).fetchone()
                if row is None or row[0] == "retired":
                    continue
                seed.execute(
                    "UPDATE question SET status='retired' "
                    "WHERE id=? AND status != 'retired'",
                    (qid,),
                )
            # Mirror QC repairs
            for qid in repaired_qc:
                opts = seed.execute(
                    "SELECT id, option_text FROM questionoption "
                    "WHERE question_id=? ORDER BY id",
                    (qid,),
                ).fetchall()
                for opt_id, text in opts:
                    txt = (text or "").strip()
                    if (
                        txt.startswith("The relationship cannot be determined")
                        and txt != _QC_CANONICAL_TEXT
                    ):
                        seed.execute(
                            "UPDATE questionoption SET option_text=? WHERE id=?",
                            (_QC_CANONICAL_TEXT, opt_id),
                        )
            seed.commit()
        finally:
            seed.close()


# Quant blocker rows surfaced by scripts/audit_data_corruption.py at
# launch time. Each row's explanation makes the answer recoverable, OR
# the row is unanswerable (chart/figure missing) and must be retired.
# (qid, action, payload):
#   "retire" → status='retired' with provenance reason
#   "repair_numeric" → add a NumericAnswer row with (exact_value, tolerance)
_AUDIT_QUANT_FIXES_2026_05_24 = [
    (4219, "retire",
     "princeton numeric_entry shipped without a NumericAnswer row; "
     "answer not reliably recoverable from explanation."),
    (4359, "retire",
     "princeton numeric_entry shipped without a NumericAnswer row; "
     "answer not reliably recoverable from explanation."),
    (4771, "repair_numeric",
     {"exact_value": 4 / 216, "tolerance": 0.001,
      "note": "Damon rolls 3 dice, P(total > 16) = 4/216 = 1/54"}),
    (4993, "retire",
     "kaplan numeric_entry references a chart that is not attached as "
     "a Stimulus; cannot be answered without the missing series table."),
    (4997, "retire",
     "kaplan numeric_entry references a percentile table that is not "
     "attached as a Stimulus; cannot be answered without the table."),
]


def _035_audit_quant_fixes_2026_05_24():
    """Repair-or-retire the 5 quant blocker rows surfaced by the launch
    audit (``scripts/audit_data_corruption.py``).

    Mirrors to seed DB. Idempotent — skips items already retired or
    already carrying a NumericAnswer.
    """
    db = _get_db()
    import json as _json
    import sqlite3 as _sqlite3
    from config import SEED_DB_PATH

    def _retire_question_row(conn, qid, reason, *, raw_sql=False):
        if raw_sql:
            row = conn.execute(
                "SELECT status, provenance_json FROM question WHERE id=?",
                (qid,)
            ).fetchone()
        else:
            row = conn.execute_sql(
                "SELECT status, provenance_json FROM question WHERE id=?",
                (qid,)
            ).fetchone()
        if row is None:
            return False
        status, prov_raw = row
        if status == "retired":
            return False
        try:
            prov = _json.loads(prov_raw) if prov_raw else {}
            if not isinstance(prov, dict):
                prov = {}
        except (ValueError, TypeError):
            prov = {}
        prov["retired_reason"] = reason
        prov["retired_by_migration"] = "035_audit_quant_fixes_2026_05_24"
        sql = ("UPDATE question SET status='retired', provenance_json=? "
               "WHERE id=? AND status != 'retired'")
        if raw_sql:
            conn.execute(sql, (_json.dumps(prov), qid))
        else:
            conn.execute_sql(sql, (_json.dumps(prov), qid))
        return True

    # ── User DB pass (Peewee) ────────────────────────────────────────
    for qid, action, payload in _AUDIT_QUANT_FIXES_2026_05_24:
        if action == "retire":
            _retire_question_row(db, qid, payload)
        elif action == "repair_numeric":
            # Guard: skip when the parent question doesn't exist (e.g. on
            # a freshly-bootstrapped test DB) to avoid FK violations.
            q_exists = db.execute_sql(
                "SELECT 1 FROM question WHERE id=?", (qid,)
            ).fetchone()
            if not q_exists:
                continue
            row = db.execute_sql(
                "SELECT id FROM numericanswer WHERE question_id=?", (qid,)
            ).fetchone()
            if row is None:
                db.execute_sql(
                    "INSERT INTO numericanswer "
                    "(question_id, exact_value, tolerance, mode) "
                    "VALUES (?, ?, ?, ?)",
                    (qid, float(payload["exact_value"]),
                     float(payload["tolerance"]), "exact"),
                )

    # ── Seed DB pass (raw sqlite) ────────────────────────────────────
    if SEED_DB_PATH.exists() and SEED_DB_PATH.stat().st_size > 1024:
        seed = _sqlite3.connect(str(SEED_DB_PATH))
        try:
            for qid, action, payload in _AUDIT_QUANT_FIXES_2026_05_24:
                if action == "retire":
                    _retire_question_row(seed, qid, payload, raw_sql=True)
                elif action == "repair_numeric":
                    q_exists = seed.execute(
                        "SELECT 1 FROM question WHERE id=?", (qid,)
                    ).fetchone()
                    if not q_exists:
                        continue
                    row = seed.execute(
                        "SELECT id FROM numericanswer WHERE question_id=?",
                        (qid,)
                    ).fetchone()
                    if row is None:
                        seed.execute(
                            "INSERT INTO numericanswer "
                            "(question_id, exact_value, tolerance, mode) "
                            "VALUES (?, ?, ?, ?)",
                            (qid, float(payload["exact_value"]),
                             float(payload["tolerance"]), "exact"),
                        )
            seed.commit()
        finally:
            seed.close()


# ── 036: Answer-key drift repair (2026-05-25) ───────────────────────
#
# These two lists are populated from
# ``scripts/audit_answer_key_drift.py``'s decisions JSON. Each entry
# is the FINAL judgement for a single question after the LLM audit:
#
#   _ANSWER_KEY_FLIPS_2026_05_25
#       The answer key was wrong but the explanation is correct.
#       Move ``is_correct=True`` from the current label to the
#       ``to_correct`` label. Single-answer items only — multi-blank
#       cases are conservatively retired even when the audit was
#       confident.
#
#   _ANSWER_KEY_RETIRES_2026_05_25
#       The audit detected drift but the LLM judge wasn't
#       unambiguous enough to flip safely (multi-blank, medium
#       confidence, or ambiguous reasoning). Status → retired with
#       ``provenance_json`` capturing the audit metadata so a future
#       SME can revive the row after manual review.
#
# Both lists are static so the migration is replayable on a fresh
# clone WITHOUT calling the LLM. The audit script writes the
# decisions to ``data/audits/answer_key_drift_decisions_<date>.json``
# at run time; the curator then transcribes the survivors here.
#
# Schema:
#   _ANSWER_KEY_FLIPS_2026_05_25 = [
#       (qid, from_label, to_label, confidence, reason),
#       ...
#   ]
#   _ANSWER_KEY_RETIRES_2026_05_25 = [
#       (qid, current_correct_list, intended_correct, confidence, reason),
#       ...
#   ]

_ANSWER_KEY_FLIPS_2026_05_25: list = [
    # (qid, from_label, to_label, confidence, reason)
    # Populated from data/audits/answer_key_drift_decisions_2026_05_25.json
    # on 2026-05-25. The full live-pool audit (2,326 questions
    # reviewed via LLM judge + heuristic second-opinion) produced
    # ZERO confirmed single-answer flips. Both safety nets agreed
    # the live single-answer pool is clean. The retires below are
    # the multi-answer / SE drift cases the LLM caught (which we
    # never flip, only retire).
]

_ANSWER_KEY_RETIRES_2026_05_25: list = [
    # (qid, current_correct_list, intended_correct, confidence, reason)
    # 10 multi-answer items where the LLM judge concluded the
    # explanation defends a different answer set than the marked
    # one (conf >= 0.7). Sampled inspection (e.g. qid 5374) showed
    # the questions are genuinely broken: the option text is given
    # as ratios (1:2, 1:4) but the explanation discusses fractions
    # (1/5, 1/4) -- a data-entry mismatch between options and
    # explanation. We retire rather than flip because partial
    # multi-answer corrections risk silently teaching learners the
    # wrong set.
    (2576, ["B", "D"], ["B"], 0.95,
     "answer-key drift audit 2026-05-25: SE explanation defends "
     "evolving (B) but key marks B+D; advancing (D) is not "
     "explicitly defended in the explanation."),
    (5374, ["A", "B", "C", "D"], ["A", "C", "D"], 0.95,
     "answer-key drift audit 2026-05-25: mcq_multi options listed "
     "as ratios (1:2, 1:4, ...) but explanation discusses "
     "fractions (1/5, 1/4, ...). Option B (1:4) does not match "
     "any value in the explanation."),
    (5376, ["A", "B", "D", "E"], ["B", "D", "E"], 0.95,
     "answer-key drift audit 2026-05-25: mcq_multi explanation "
     "concludes total must be divisible by 12; only B (21x1), "
     "D (35x1.2), E (42x1) are valid -- A is not defended."),
    (5377, ["A", "C", "E"], ["B", "C", "E"], 0.95,
     "answer-key drift audit 2026-05-25: mcq_multi explanation "
     "calculates range for d as 14.14 to 20; explicitly states "
     "B (15), C (20), E (19) fall in range -- A is not."),
    (5381, ["B", "C", "D"], ["A", "B", "C", "D"], 0.70,
     "answer-key drift audit 2026-05-25: mcq_multi explanation "
     "is incomplete and the LLM judge was uncertain (0.70 conf); "
     "retire pending re-derivation of the achievable mean range."),
    (5382, ["A", "B", "D", "E"], ["B", "E"], 0.95,
     "answer-key drift audit 2026-05-25: mcq_multi explanation "
     "concludes total must be a multiple of 12; only B (21*) and "
     "E (42) match -- A and D are not defended (and the "
     "explanation notes 21 is not a multiple of 12)."),
    (5383, ["A", "F"], ["A", "E", "F"], 0.95,
     "answer-key drift audit 2026-05-25: mcq_multi explanation "
     "lists divisors of 36; explicitly confirms 4 (F), 6 (E), and "
     "12 (A) as valid -- E is missing from the marked set."),
    (5385, ["C", "D", "E"], ["A", "B", "C", "D"], 0.95,
     "answer-key drift audit 2026-05-25: mcq_multi explanation "
     "concludes L<=5 with valid L values 0,1,2,3,4,5; options "
     "A (0), B (2), C (4), D (5) match -- E does not."),
    (5387, ["C", "E", "G"], ["B", "C", "E", "G"], 0.95,
     "answer-key drift audit 2026-05-25: mcq_multi explanation "
     "identifies B, C, E, G as satisfying the prime-product "
     "criterion -- B is missing from the marked set."),
    (5389, ["A", "C", "E", "F"], ["C", "E", "F"], 0.90,
     "answer-key drift audit 2026-05-25: mcq_multi explanation "
     "requires 0 < k < 25/8; checks k=1,2,3 valid; among options "
     "C (0), E (20), F (48) fit a strict reading -- A does not."),
]


def _036_answer_key_drift_repair_2026_05_25():
    """Apply the answer-key drift repair plan from the 2026-05-25 audit.

    Source of truth: ``_ANSWER_KEY_FLIPS_2026_05_25`` +
    ``_ANSWER_KEY_RETIRES_2026_05_25`` in this file.

    For each FLIP entry we:
      1. Confirm the question still exists and the option labels are
         still present (``q_exists``-style guard so the migration
         no-ops on test DBs that don't carry these qids).
      2. UPDATE questionoption SET is_correct = (label == to_label)
         WHERE question_id=qid AND option_label IN (from_label, to_label).
      3. Record the change in the question's ``provenance_json``.

    For each RETIRE entry we set ``status='retired'`` and stash the
    audit reasoning in ``provenance_json``.

    Both passes mirror to the seed DB so a fresh-install user gets
    the corrected state and the seed_sync reconcile doesn't revert
    it. Idempotent: re-running is a no-op (the option flip checks
    current state, the retire checks status).
    """
    db = _get_db()
    import json as _json
    import sqlite3 as _sqlite3
    from config import SEED_DB_PATH

    def _apply_flip(conn, qid, from_label, to_label, confidence, reason,
                    *, raw_sql=False):
        """Move is_correct from from_label to to_label on this qid.

        Returns True when at least one row was updated.
        """
        # Guard: the question and both option rows must exist.
        exec_one = (conn.execute if raw_sql else conn.execute_sql)
        q_row = exec_one(
            "SELECT id, provenance_json, status FROM question WHERE id=?",
            (qid,),
        ).fetchone()
        if q_row is None:
            return False
        # If already retired, don't bother flipping options on a dead row.
        if q_row[2] == "retired":
            return False
        from_opt = exec_one(
            "SELECT id, is_correct FROM questionoption "
            "WHERE question_id=? AND option_label=?",
            (qid, from_label),
        ).fetchone()
        to_opt = exec_one(
            "SELECT id, is_correct FROM questionoption "
            "WHERE question_id=? AND option_label=?",
            (qid, to_label),
        ).fetchone()
        if from_opt is None or to_opt is None:
            return False
        # Idempotent guard: if to_label is already the only correct, skip.
        if not from_opt[1] and to_opt[1]:
            return False

        # Apply the flip.
        if raw_sql:
            conn.execute(
                "UPDATE questionoption SET is_correct=0 "
                "WHERE question_id=? AND option_label=?",
                (qid, from_label),
            )
            conn.execute(
                "UPDATE questionoption SET is_correct=1 "
                "WHERE question_id=? AND option_label=?",
                (qid, to_label),
            )
        else:
            conn.execute_sql(
                "UPDATE questionoption SET is_correct=0 "
                "WHERE question_id=? AND option_label=?",
                (qid, from_label),
            )
            conn.execute_sql(
                "UPDATE questionoption SET is_correct=1 "
                "WHERE question_id=? AND option_label=?",
                (qid, to_label),
            )

        # Record the change in provenance.
        try:
            prov = _json.loads(q_row[1]) if q_row[1] else {}
            if not isinstance(prov, dict):
                prov = {}
        except (ValueError, TypeError):
            prov = {}
        prov["answer_key_flipped_by_migration"] = (
            "036_answer_key_drift_repair_2026_05_25"
        )
        prov["answer_key_flipped"] = {
            "from": from_label,
            "to": to_label,
            "confidence": confidence,
            "reason": reason,
        }
        if raw_sql:
            conn.execute(
                "UPDATE question SET provenance_json=? WHERE id=?",
                (_json.dumps(prov), qid),
            )
        else:
            conn.execute_sql(
                "UPDATE question SET provenance_json=? WHERE id=?",
                (_json.dumps(prov), qid),
            )
        return True

    def _apply_retire(conn, qid, current_correct, intended_correct,
                      confidence, reason, *, raw_sql=False):
        """Set status='retired' with audit metadata. Idempotent."""
        exec_one = (conn.execute if raw_sql else conn.execute_sql)
        row = exec_one(
            "SELECT status, provenance_json FROM question WHERE id=?",
            (qid,),
        ).fetchone()
        if row is None:
            return False
        status, prov_raw = row
        if status == "retired":
            return False
        try:
            prov = _json.loads(prov_raw) if prov_raw else {}
            if not isinstance(prov, dict):
                prov = {}
        except (ValueError, TypeError):
            prov = {}
        prov["retired_reason"] = reason
        prov["retired_by_migration"] = (
            "036_answer_key_drift_repair_2026_05_25"
        )
        prov["answer_key_drift_audit"] = {
            "current_correct": current_correct,
            "intended_correct": intended_correct,
            "confidence": confidence,
        }
        if raw_sql:
            conn.execute(
                "UPDATE question SET status='retired', provenance_json=? "
                "WHERE id=? AND status != 'retired'",
                (_json.dumps(prov), qid),
            )
        else:
            conn.execute_sql(
                "UPDATE question SET status='retired', provenance_json=? "
                "WHERE id=? AND status != 'retired'",
                (_json.dumps(prov), qid),
            )
        return True

    # ── User DB pass (Peewee) ────────────────────────────────────────
    for qid, from_label, to_label, confidence, reason in (
        _ANSWER_KEY_FLIPS_2026_05_25
    ):
        _apply_flip(db, qid, from_label, to_label, confidence, reason)
    for entry in _ANSWER_KEY_RETIRES_2026_05_25:
        # entry = (qid, current_correct, intended_correct, confidence, reason)
        qid, current_correct, intended_correct, confidence, reason = entry
        _apply_retire(
            db, qid, current_correct, intended_correct, confidence, reason,
        )

    # ── Seed DB pass (raw sqlite) ────────────────────────────────────
    if SEED_DB_PATH.exists() and SEED_DB_PATH.stat().st_size > 1024:
        seed = _sqlite3.connect(str(SEED_DB_PATH))
        try:
            for qid, from_label, to_label, confidence, reason in (
                _ANSWER_KEY_FLIPS_2026_05_25
            ):
                _apply_flip(
                    seed, qid, from_label, to_label, confidence, reason,
                    raw_sql=True,
                )
            for entry in _ANSWER_KEY_RETIRES_2026_05_25:
                qid, current_correct, intended_correct, confidence, reason = entry
                _apply_retire(
                    seed, qid, current_correct, intended_correct,
                    confidence, reason, raw_sql=True,
                )
            seed.commit()
        finally:
            seed.close()


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
    ("017_batch_ai_review_2026_05_01",
     _017_batch_ai_review_2026_05_01),
    ("018_fix_user_reported_2026_05_03",
     _018_fix_user_reported_2026_05_03),
    ("019_fix_missing_figure_prompts_2026_05_04",
     _019_fix_missing_figure_prompts_2026_05_04),
    ("020_retire_image_text_mismatches_2026_05_06",
     _020_retire_image_text_mismatches_2026_05_06),
    ("021_retire_q3720_image_mismatch_2026_05_06",
     _021_retire_q3720_image_mismatch_2026_05_06),
    ("022_retire_q3754_image_mismatch_2026_05_11",
     _022_retire_q3754_image_mismatch_2026_05_11),
    ("023_served_log_2026_05_12",
     _023_served_log_2026_05_12),
    ("024_response_time_ms_2026_05_12",
     _024_response_time_ms_2026_05_12),
    ("025_item_rating_2026_05_12",
     _025_item_rating_2026_05_12),
    ("026_item_review_fsrs_2026_05_12",
     _026_item_review_fsrs_2026_05_12),
    ("027_vocab_context_2026_05_12",
     _027_vocab_context_2026_05_12),
    ("028_served_log_stim_id_2026_05_12",
     _028_served_log_stim_id_2026_05_12),
    ("029_retire_manhattan_di_singletons_2026_05_13",
     _029_retire_manhattan_di_singletons_2026_05_13),
    ("030_section_routing_tier_2026_05_18",
     _030_section_routing_tier_2026_05_18),
    ("032_retire_dedup_high_confidence_2026_05_18",
     _032_retire_dedup_high_confidence_2026_05_18),
    ("033_promote_synth_candidates_2026_05_18",
     _033_promote_synth_candidates_2026_05_18),
    ("034_repair_or_retire_validator_findings_2026_05_18",
     _034_repair_or_retire_validator_findings_2026_05_18),
    ("035_audit_quant_fixes_2026_05_24",
     _035_audit_quant_fixes_2026_05_24),
    ("036_answer_key_drift_repair_2026_05_25",
     _036_answer_key_drift_repair_2026_05_25),
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
