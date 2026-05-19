"""
Question bank service — loads, filters, and selects questions from the database.
"""
import random
from datetime import datetime, timedelta

from peewee import fn, OperationalError

from models.database import (
    db, Question, QuestionOption, NumericAnswer, Stimulus,
    AWAPrompt, VocabWord, Response, QuestionFlag, ServedLog,
)
from services.log import get_logger

logger = get_logger("question_bank")


# Subtypes whose questions are grouped into atomic clusters sharing a
# Stimulus. When the assembler picks any one of these, it must also pull
# every sibling under the same stimulus_id so the user always sees the
# full passage/chart alongside its questions.
CLUSTER_SUBTYPES = {
    "rc_single", "rc_multi", "rc_select_passage", "data_interp",
}

# Verbal-only subset of ``CLUSTER_SUBTYPES`` — the RC passage cluster.
# Used by the blueprint smoke test and by callers that need to assert
# passage atomicity without conflating with DI clusters.
CLUSTERED_VERBAL_SUBTYPES = ("rc_single", "rc_multi", "rc_select_passage")

# Source token used by the synthetic-question pipeline. Kept here (rather
# than imported from `services.synthetic`) so the toggle plumbing has no
# dependency on the pipeline package — the bank stays standalone if the
# pipeline isn't shipped.
SYNTHETIC_SOURCE = "ai_synthetic"


def _exclude_synthetic_clause():
    """Return a Peewee `WHERE` fragment that hides synthetic items, or
    `None` if the user has the toggle on (default).

    Re-reads `load_user_prefs` on every call so a Settings-dialog change
    takes effect immediately — the rest of the app already assumes
    `llm_config.json` is hot-reloaded between calls.

    Callers should:

        clause = _exclude_synthetic_clause()
        if clause is not None:
            query = query.where(clause)
    """
    # Local import: config imports must stay lazy because some test
    # fixtures swap `config.DB_PATH` after this module is already loaded.
    from config import load_user_prefs
    if load_user_prefs().get("include_ai_synthetic", True):
        return None
    return Question.source != SYNTHETIC_SOURCE

# Default dedup windows (days). Tunable by callers of
# ``select_questions_composed``. The historical values (30/60) were a
# naive heuristic; see ``data/audits/spaced_repetition_research_2026_04_28.md``
# for the learning-science backing of the current values. Cluster cooldown
# is NOT configured here — it lives in ``services.scheduler`` because it
# is keyed on ``stimulus_id``, not ``question_id``.
DEFAULT_RECENT_SEEN_DAYS = 30
DEFAULT_MASTERY_COOLDOWN_DAYS = 60

# P4.P3: 7-day cooldown on RC-passage and DI-cluster ``stimulus_id``s.
# Even when sibling qids rotate, the shared passage/chart shouldn't
# reappear inside this window. Read side is
# ``get_recently_served_stimulus_ids``; unioned into
# ``exclude_stimulus_ids`` inside ``_select_rc_passage_anchor`` /
# ``_select_di_cluster``.
DEFAULT_CLUSTER_COOLDOWN_DAYS = 7


# Threshold of distinct flag-submitting users at which a question is
# auto-retired by `auto_retire_flagged_questions`. Conservative because
# the local-only app today has only one user — set to 1 in single-user
# mode and bumped to 3 if user_id ever varies.
AUTO_RETIRE_THRESHOLD_DEFAULT = 3


# Phase 2 E5: randomesque item selection — pick one qid uniformly from the
# top-M candidates closest to the user's theta, then shuffle the rest.
# Degrades gracefully to pure random when the rating service is not
# importable or no candidate has a rating. Toggle off for benchmarks/tests
# that want the legacy behaviour.
RANDOMESQUE_ENABLED = True
RANDOMESQUE_DEFAULT_M = 5


def _randomesque_pick(candidate_ids, m=RANDOMESQUE_DEFAULT_M):
    """Shuffle ``candidate_ids`` with a theta-aware front bias.

    The first element is drawn uniformly at random from the top-``m``
    candidates whose ``|rating - user_theta|`` is smallest (most
    informative for the current ability estimate). The remaining
    candidates follow in random order.

    Graceful degradation: if ``services.rating_service`` cannot be
    imported, or none of the candidates has a rating, or
    ``RANDOMESQUE_ENABLED`` is False, this falls back to plain
    ``random.shuffle`` and returns an equivalently shuffled list — the
    caller never has to care which path was taken.

    The input list is NOT mutated; a new list is always returned. An
    empty input yields an empty list. ``m`` is clamped to at least 1 and
    at most ``len(candidate_ids)``.
    """
    # Tolerate generators/tuples — callers sometimes pass these.
    ids = list(candidate_ids) if not isinstance(candidate_ids, list) \
        else list(candidate_ids)
    if not ids:
        return []

    if not RANDOMESQUE_ENABLED:
        random.shuffle(ids)
        return ids

    # Runtime import so module-load stays cheap and so this file still
    # imports cleanly in worktrees where rating_service hasn't landed
    # yet (Phase 2 E4 lands in parallel).
    try:
        from services import rating_service  # type: ignore
        theta = rating_service.get_user_theta()
        ratings = {qid: rating_service.get_rating(qid) for qid in ids}
    except Exception:
        random.shuffle(ids)
        return ids

    # If the service exists but has no ratings for any candidate, the
    # theta distance collapses — fall back to plain random.
    rated = [(qid, ratings.get(qid)) for qid in ids
             if ratings.get(qid) is not None]
    if not rated:
        random.shuffle(ids)
        return ids

    # Rank by distance to theta (smallest = most informative). Stable
    # sort keeps tie-breaking deterministic per-call; randomness enters
    # only in the uniform draw over top-M.
    try:
        rated.sort(key=lambda pair: abs(pair[1] - theta))
    except Exception:
        random.shuffle(ids)
        return ids

    top_m = max(1, min(m, len(rated)))
    front_pool = rated[:top_m]
    winner_qid = random.choice(front_pool)[0]

    rest = [qid for qid in ids if qid != winner_qid]
    random.shuffle(rest)
    return [winner_qid] + rest


# Real GRE composition targets per section (proportions sum to 1.0)
# Source: ETS GRE official guide — Verbal/Quant section composition
VERBAL_COMPOSITION = {
    "rc_single": 0.35,           # ~5-6 of 12, ~7 of 15
    "rc_multi": 0.10,            # ~1-2 per section
    "rc_select_passage": 0.05,   # rare, ~1 per test
    "tc": 0.25,                  # ~3-4 per section
    "se": 0.25,                  # ~3-4 per section
}

QUANT_COMPOSITION = {
    # 7.1: aligned to Magoosh-estimated real GRE composition 2026-05-18.
    # Source: Magoosh — GRE Format and Section Breakdown (single-vendor estimate, flag).
    # Applied to 12-Q (V1) and 15-Q (V2) sections via _composition_targets:
    "qc": 0.33,                  # ~4 per 12-Q section, ~5 per 15-Q section
    "mcq_single": 0.37,          # ~4 per 12-Q section, ~6 per 15-Q section
    "mcq_multi": 0.09,           # ~1 per 12-Q section, ~1 per 15-Q section
    "numeric_entry": 0.09,       # ~1 per 12-Q section, ~1 per 15-Q section
    "data_interp": 0.11,         # ~1 DI cluster of 3 Q per section
}


# Phase 7.2 — ETS 3-tier difficulty-mix (Path B routing, OQ1 resolution
# 2026-05-18). When ``select_questions_composed`` receives a ``routing_tier``
# argument, the tier→difficulty mix below replaces the legacy hard-WHERE
# / theta-soft-weight band gate. Each row sums to 1.0 across the 5
# difficulty levels (band 1 = easiest, band 5 = hardest). Targets per
# BrightLink Prep tier shapes (report.md §3.4):
#   * 'easy'   → 65–70% L1+L2, 25–30% L3, ~5%  L4+L5
#   * 'medium' → ~20%  L1+L2, ~60% L3, ~20% L4+L5
#   * 'hard'   → ~5%   L1+L2, 25–30% L3, 65–70% L4+L5
# Used as soft probability weights when ranking candidate clusters; we
# never hard-gate so a thin band pool can still ship a full section.
TIER_DIFFICULTY_MIX = {
    "easy": {1: 0.35, 2: 0.32, 3: 0.28, 4: 0.04, 5: 0.01},
    "medium": {1: 0.10, 2: 0.10, 3: 0.60, 4: 0.15, 5: 0.05},
    "hard": {1: 0.01, 2: 0.04, 3: 0.28, 4: 0.32, 5: 0.35},
}

# ETS blueprint: every Quant section contains exactly one DI *set* of 3
# questions sharing a single chart/table stimulus. We prefer real clusters
# (rows whose ``stimulus.stimulus_type`` is graph/table/chart) even when the
# child subtype is mcq_single / qc / numeric_entry. Legacy solo chart items
# marked ``subtype='data_interp'`` with no stimulus fall through to the
# final fallback.
DI_STIMULUS_TYPES = ("graph", "table", "chart")
DI_CLUSTER_TARGET_SIZE = 3
# Minimum ``live`` siblings a stimulus must have to be eligible as a real
# DI cluster. Previously this was ``2`` (require a pair), which silently
# disqualified every stimulus in the current seed bank (0 graph/table
# stimuli have >=2 live children). Lowered to ``1`` so the selector emits
# a result for every stimulus with at least one live child; the selector
# still prefers true multi-sibling clusters (size >=2) and only uses
# size-1 stimuli to compose an "independent singletons" DI block across
# three DIFFERENT stimuli when no real cluster is available.
DI_CLUSTER_MIN_SIZE = 1

# Minimum figure-bearing Quant items per section. Kaplan/Princeton +
# ETS community consensus: 25–35% of Quant items reference a figure
# (geometry diagram, coordinate plane, number line, chart, table). The
# floor covers: the DI block (~3) + 1–2 geometry diagrams in QC/PS.
# Our bank ships 45 geometry singletons (Manhattan 5lb) + 14 DI items;
# without an explicit floor the random sampler from 1,500+ Quant items
# hits a figure ~5% of the time per subtype slot and the user sees a
# nearly figure-less section (GitHub: "no figure-based questions in
# quants").
#
# The floor scales with section length so the figure density stays in
# the 25–33% band regardless of whether the caller asks for Q1 (12) or
# Q2 (15): 3/12 ≈ 25%, 4/15 ≈ 27%.
#
# 7.3: floor values gated on Phase 5+6 figure synthesis closing the live
# geometry-MCQ image gap; current pool is too thin for higher floors per
# docs/implementation_plan_2026_05_18.md. Centralized as module-level
# constants so a Phase 6 follow-up can flip them with a one-line edit.
# Pre-Phase-5/6 (low figure pool): keep at 3/4 to avoid serving repeats.
QUANT_FIGURE_FLOOR_SHORT = 3   # active; raise to 5 after Phase 5/6 close gap
QUANT_FIGURE_FLOOR_LONG = 4    # active; raise to 7 after Phase 5/6 close gap
QUANT_FIGURE_SECTION_BOUNDARY = 12  # count threshold splitting "short" from "long"


def _quant_figure_floor(count, pool_size=None):
    """Minimum figure-bearing items for a Quant section of size ``count``.

    When the live figure pool is smaller than ~8× the nominal floor, we
    back off proportionally — forcing 3-of-43 into every section drives
    the same items into heavy rotation. At the small end we still keep
    ≥1 figure to honor the ETS composition pattern.

    The floor values come from the ``QUANT_FIGURE_FLOOR_SHORT`` /
    ``QUANT_FIGURE_FLOOR_LONG`` module constants so Phase 6 can raise
    them once the figure-gap pool closes (per Phase 7.3 plan, 2026-05-18).
    """
    base = (QUANT_FIGURE_FLOOR_SHORT if count <= QUANT_FIGURE_SECTION_BOUNDARY
            else QUANT_FIGURE_FLOOR_LONG)
    if pool_size is None:
        return base
    soft = max(1, min(base, pool_size // 8))
    return soft


# Minimum multi-question RC passage anchor per Verbal section. Both
# Kaplan and Princeton Review ship at least one passage with 3-4 linked
# questions in each full-length practice Verbal section. Our bank has 84
# live multi-Q passages, but 82.9% of ``rc_single`` cluster keys are
# single-child singletons, so a random shuffle almost always picks four
# standalone passages instead of the real-GRE "1 long + 1-2 short"
# shape. A primary anchor guarantees ≥1 multi-Q passage lands per
# section; a secondary anchor tries for a second (smaller) passage when
# RC budget remains, matching the "2–3 passages per section" shape
# Kaplan PT1 and Princeton diagnostic both ship.
RC_ANCHOR_MIN_SIZE = 2
RC_ANCHOR_PREFER_SIZE = 3  # prefer 3+ Q passages for the primary anchor


def _log_served(qids, user_id: str = "local", session_id=None):
    """R3 — bulk-insert a ServedLog row per qid at pick time.

    Called from ``select_questions_composed`` right before it returns.
    Uses a single ``insert_many`` round-trip; guarded with try/except so
    a DB-layer hiccup (migration not applied, DB locked, etc.) degrades
    to WARN-and-continue rather than blowing up the selection path.

    P4.P3: also captures each qid's ``stimulus_id`` (NULL for singletons)
    so the 7-day cluster cooldown can query served stimuli directly
    without a Question join on every read.
    """
    if not qids:
        return
    try:
        # One query to map qid -> stimulus_id. Missing rows (deleted /
        # retired qids) default to NULL.
        stim_map = {}
        try:
            for row in (Question
                        .select(Question.id, Question.stimulus)
                        .where(Question.id.in_([int(q) for q in qids]))):
                stim_map[row.id] = row.stimulus_id
        except Exception:
            # Worst case: fall back to NULL stimulus_id for everyone.
            logger.debug(
                "_log_served: stimulus lookup failed; writing NULL stim_ids",
                exc_info=True,
            )

        now = datetime.now()
        rows = [
            {"question_id": int(qid),
             "session_id": str(session_id) if session_id is not None else None,
             "user_id": user_id or "local",
             "served_at": now,
             "stimulus_id": stim_map.get(int(qid))}
            for qid in qids
        ]
        try:
            ServedLog.insert_many(rows).execute()
        except OperationalError as e:
            # Older user DB that hasn't run migration 028 yet — drop
            # stimulus_id from the payload and retry so dedup still
            # works (just without cluster cooldown).
            if _is_missing_stim_id_error(e):
                for r in rows:
                    r.pop("stimulus_id", None)
                ServedLog.insert_many(rows).execute()
            else:
                raise
    except Exception:
        logger.warning(
            "servedlog write failed for %d qids (user=%s); "
            "selection proceeds, dedup may regress",
            len(qids), user_id, exc_info=True,
        )


def _is_missing_stim_id_error(exc) -> bool:
    """True when a peewee OperationalError complains about the missing
    ``stimulus_id`` column on ``servedlog`` — the one symptom migration
    028 fixes. Any other schema error still bubbles up."""
    msg = str(exc).lower()
    return "stimulus_id" in msg and ("no column" in msg
                                      or "no such column" in msg
                                      or "has no column" in msg)


def get_recently_served_stimulus_ids(days: int = 7,
                                     user_id: str = "local"):
    """P4.P3 — return the set of ``stimulus_id`` values served to
    ``user_id`` within the last ``days`` days.

    Used by ``_select_rc_passage_anchor`` and ``_select_di_cluster`` to
    refuse to pick a passage/chart whose sibling qids were already
    served — closing the loophole where the assembler rotates to a
    different qid under the same stimulus within the dedup window.

    Gracefully returns an empty set when:
      * ``servedlog.stimulus_id`` doesn't exist yet (user DB hasn't run
        migration 028), or
      * the query otherwise fails. The caller treats "no cooldown" as a
        degraded-but-safe fallback rather than blowing up selection.
    """
    try:
        cutoff = datetime.now() - timedelta(days=days)
        rows = (ServedLog
                .select(ServedLog.stimulus_id)
                .where((ServedLog.served_at >= cutoff) &
                       (ServedLog.user_id == user_id) &
                       (ServedLog.stimulus_id.is_null(False)))
                .distinct())
        return {r.stimulus_id for r in rows if r.stimulus_id is not None}
    except Exception:
        logger.debug(
            "get_recently_served_stimulus_ids: read failed; "
            "cluster cooldown degrades to no-op",
            exc_info=True,
        )
        return set()


def get_recently_seen_ids(days_back: int = 14, user_id: str = "local"):
    """Return question IDs the user has answered OR been served in the last N days.

    Used to avoid showing the same questions in consecutive sessions.
    Unions two sources:
      * ``Response`` — items the user actually answered (historical).
      * ``ServedLog`` — items served to the user at pick time, regardless
        of whether they answered (R3 addition). This is what makes
        fresh-launch, binge-mocking users get dedup on the second mock
        even before any Response row exists.
    """
    cutoff = datetime.now() - timedelta(days=days_back)
    seen = set()
    rows = Response.select(Response.question_id).where(
        Response.created_at >= cutoff
    ).distinct()
    seen.update(r.question_id for r in rows)
    # Served-log union — guarded so a schema gap (older user DB that
    # hasn't run migration 023 yet) can't break the selector.
    try:
        served_rows = (ServedLog
                       .select(ServedLog.question_id)
                       .where((ServedLog.served_at >= cutoff) &
                              (ServedLog.user_id == user_id))
                       .distinct())
        seen.update(r.question_id for r in served_rows)
    except Exception:
        logger.debug(
            "get_recently_seen_ids: servedlog read failed; "
            "falling back to Response-only dedup",
            exc_info=True,
        )
    return seen


def _cluster_group(q):
    """Return the cluster key for a question.

    Cluster subtypes (RC / DI) group under their ``stimulus_id``; everything
    else is its own singleton cluster keyed by question id so the assembler
    can treat every selection uniformly.

    Verbal RC subtypes (rc_single, rc_multi, rc_select_passage) that share
    a stimulus are collapsed into a single cluster so the full passage
    moves as one atomic unit; DI and other cluster subtypes remain
    subtype-scoped.
    """
    subtype = getattr(q, "subtype", None)
    stim_id = getattr(q, "stimulus_id", None)
    if subtype in CLUSTER_SUBTYPES and stim_id:
        if subtype in CLUSTERED_VERBAL_SUBTYPES:
            # Collapse rc_single / rc_multi / rc_select_passage under one
            # cluster key so the whole passage comes together.
            return ("stim", stim_id, "rc")
        return ("stim", stim_id, subtype)
    return ("q", q.id)


def _user_recent_seen(user_id: str = "local",
                      days: int = DEFAULT_RECENT_SEEN_DAYS):
    """Question IDs the user has touched within the last ``days`` days.

    The app is single-user today, so ``user_id`` is informational; every
    response in the DB is this user's. Kept as a parameter so the helper
    grows cleanly when multi-user arrives.

    Unions Response (answered) + ServedLog (served — R3) so dedup fires
    even when the user never answered (abandoned sessions, fresh launch
    after a series of benchmark mocks, etc.).
    """
    if days is None or days <= 0:
        return set()
    cutoff = datetime.now() - timedelta(days=days)
    seen = set()
    rows = (Response
            .select(Response.question_id)
            .where(Response.created_at >= cutoff)
            .distinct())
    seen.update(r.question_id for r in rows)
    try:
        served_rows = (ServedLog
                       .select(ServedLog.question_id)
                       .where((ServedLog.served_at >= cutoff) &
                              (ServedLog.user_id == user_id))
                       .distinct())
        seen.update(r.question_id for r in served_rows)
    except Exception:
        logger.debug(
            "_user_recent_seen: servedlog read failed; "
            "falling back to Response-only dedup",
            exc_info=True,
        )
    return seen


def _user_mastery_last_correct(user_id: str = "local"):
    """Return ``{question_id: last_correct_datetime}``.

    Derived from the append-only ``Response`` log. We take the most recent
    correct answer per question; spaced-repetition logic in
    ``select_questions_composed`` uses this to damp items the user has
    already mastered recently.
    """
    rows = (Response
            .select(Response.question_id,
                    fn.MAX(Response.created_at).alias("last_correct"))
            .where(Response.is_correct == True)  # noqa: E712
            .group_by(Response.question_id))
    out = {}
    for r in rows:
        out[r.question_id] = r.last_correct
    return out


def _dedup_exclusions(user_id: str,
                      recent_days: int,
                      mastery_cooldown_days: int,
                      include_cluster_cooldown: bool = True):
    """Combined dedup set used by ``select_questions_composed``.

    Returns a ``set[int]`` of question IDs to leave out of the pool. Two
    filters always apply:
      * anything the user touched in the last ``recent_days`` days, plus
      * anything the user answered correctly in the last
        ``mastery_cooldown_days`` days (spaced-repetition cooldown).

    When ``include_cluster_cooldown`` is True (default), the scheduler in
    ``services.scheduler`` additionally suppresses every child question
    under a DI chart / RC passage whose cluster was recently served —
    even if that specific qid was never individually seen. This is the
    core fix for "why does the same DI chart keep coming back?".

    Items the user got *wrong* are deliberately NOT added to the mastery
    cooldown set — the learner should see those sooner for review.
    """
    recent = _user_recent_seen(user_id=user_id, days=recent_days)
    mastered = set()
    if mastery_cooldown_days and mastery_cooldown_days > 0:
        cutoff = datetime.now() - timedelta(days=mastery_cooldown_days)
        for qid, last_correct in _user_mastery_last_correct(user_id).items():
            if last_correct and last_correct >= cutoff:
                mastered.add(qid)

    combined = recent | mastered

    if include_cluster_cooldown:
        # Cluster-aware layer (DI/RC). Uses scheduler defaults from
        # ``data/llm_config.json`` so the operator can tune them without
        # code changes. Imported lazily to keep this module importable
        # before scheduler config lands.
        try:
            from services.scheduler import (
                scheduler_exclusions, stim_ids_to_qids,
            )
        except ImportError:  # pragma: no cover — defensive
            logger.warning("scheduler module unavailable; falling back to "
                           "item-only dedup")
            return combined
        _hard, cluster_stims = scheduler_exclusions(
            user_id=user_id,
            recent_seen_days=recent_days,
            mastery_cooldown_days=mastery_cooldown_days,
        )
        if cluster_stims:
            combined = combined | stim_ids_to_qids(cluster_stims)

    return combined


def user_stats(user_id: str = "local",
               recent_days: int = DEFAULT_RECENT_SEEN_DAYS,
               mastery_cooldown_days: int = DEFAULT_MASTERY_COOLDOWN_DAYS):
    """UX helper: how much of the pool has the user chewed through?

    Returns a dict with:
      * ``seen_count`` / ``total_pool`` — raw coverage
      * ``dedup_days_active`` / ``mastery_cooldown_days`` — the cooldown
        windows currently in force
      * ``dedup_active_count`` — items currently suppressed by item-level
        cooldowns
      * ``cluster_cooled_count`` — DI/RC clusters currently cooled; a
        single cluster suppresses every child question in the bank
      * ``cluster_cooled_qid_count`` — expanded count of suppressed child
        questions (useful for the "you've seen 487 / 2990" banner so
        users know why certain types are scarce right now)
    """
    total_pool = (Question
                  .select(fn.COUNT(Question.id))
                  .where(Question.status == "live")
                  .scalar()) or 0
    # "seen" — any response ever, not just in the dedup window.
    seen_rows = (Response
                 .select(Response.question_id)
                 .distinct())
    seen_count = sum(1 for _ in seen_rows)
    dedup_active = _dedup_exclusions(
        user_id=user_id,
        recent_days=recent_days,
        mastery_cooldown_days=mastery_cooldown_days,
        include_cluster_cooldown=False,
    )
    cluster_cooled_count = 0
    cluster_cooled_qids = 0
    try:
        from services.scheduler import scheduler_exclusions, stim_ids_to_qids
        _hard, cluster_stims = scheduler_exclusions(
            user_id=user_id,
            recent_seen_days=recent_days,
            mastery_cooldown_days=mastery_cooldown_days,
        )
        cluster_cooled_count = len(cluster_stims)
        cluster_cooled_qids = len(stim_ids_to_qids(cluster_stims))
    except ImportError:
        pass
    return {
        "seen_count": seen_count,
        "total_pool": total_pool,
        "dedup_days_active": recent_days,
        "mastery_cooldown_days": mastery_cooldown_days,
        "dedup_active_count": len(dedup_active),
        "cluster_cooled_count": cluster_cooled_count,
        "cluster_cooled_qid_count": cluster_cooled_qids,
    }


class QuestionBankService:
    """Query and select questions for test assembly and drills."""

    def select_review_queue(self, count=20, user_id="local"):
        """Return up to ``count`` question IDs from the FSRS due-review
        queue (P2.E2). Items come from ``services.srs.get_due_items`` —
        populated by the error-log Schedule-Redo button and by review
        lapses. Retired items are filtered so a long-stale row doesn't
        surface a deleted question.

        Returns [] if nothing is due; callers decide how to handle the
        empty case (we don't auto-fall-back here — review mode should
        be honest about "nothing to review")."""
        from services import srs
        ids = srs.get_due_items(user_id=user_id, limit=max(1, int(count)))
        if not ids:
            return []
        live_ids = [
            r.id for r in Question.select(Question.id).where(
                (Question.id.in_(ids)) & (Question.status == "live")
            )
        ]
        # Preserve the srs ordering (oldest due first).
        order = {qid: i for i, qid in enumerate(ids)}
        live_ids.sort(key=lambda q: order.get(q, 1_000_000))
        return live_ids

    def select_drill_smart(self, subtopic, count=10, user_id="local",
                           avoid_recent_days=14):
        """Smart drill selection for a single subtopic.

        Priority:
        1. Skip questions seen in the last N days (avoid recent repeats)
        2. Within remaining pool, prefer questions never answered
        3. Then: questions answered incorrectly (need review)
        4. Then: rest, shuffled
        """
        from peewee import fn

        # All live questions for this subtopic
        all_qs_query = (Question
                        .select(Question.id, Question.difficulty_target)
                        .where((Question.subtopic == subtopic) &
                               (Question.status == "live")))
        clause = _exclude_synthetic_clause()
        if clause is not None:
            all_qs_query = all_qs_query.where(clause)
        all_qs = list(all_qs_query)
        if not all_qs:
            return []

        # Recently seen — skip these
        recent = get_recently_seen_ids(days_back=avoid_recent_days, user_id=user_id)

        # Past responses for accuracy lookup
        past_correct = {}
        rows = Response.select(Response.question_id, Response.is_correct).where(
            Response.is_correct.is_null(False)
        )
        for r in rows:
            past_correct[r.question_id] = r.is_correct

        # Bucket questions
        never_seen = []
        wrong_before = []
        right_before = []

        for q in all_qs:
            if q.id in recent:
                continue
            if q.id not in past_correct:
                never_seen.append(q.id)
            elif past_correct[q.id] is False:
                wrong_before.append(q.id)
            else:
                right_before.append(q.id)

        # If everything is recent, fall back to all
        if not never_seen and not wrong_before and not right_before:
            never_seen = [q.id for q in all_qs]

        # Phase 2 E5: theta-aware front bias per qid pool (gracefully
        # falls back to plain shuffle when rating_service is unavailable).
        never_seen = _randomesque_pick(never_seen)
        wrong_before = _randomesque_pick(wrong_before)
        right_before = _randomesque_pick(right_before)

        # Compose drill: most never-seen, then wrong-before for review, fill with right-before
        target = count
        result = []
        # 60% never-seen, 30% wrong-before, 10% right-before
        n_new = min(int(target * 0.6) + 1, len(never_seen))
        n_wrong = min(int(target * 0.3) + 1, len(wrong_before))

        result.extend(never_seen[:n_new])
        result.extend(wrong_before[:n_wrong])

        # Fill the rest from anywhere
        if len(result) < target:
            remaining = (never_seen[n_new:] + wrong_before[n_wrong:] + right_before)
            result.extend(remaining[:target - len(result)])

        random.shuffle(result)
        return result[:target]

    def select_questions(self, measure, count, difficulty_band="medium",
                         topic=None, exclude_ids=None):
        """
        Select `count` question IDs (random, no composition).
        Used for topic drills.
        """
        query = Question.select(Question.id).where(
            Question.measure == measure,
            Question.status == "live",
        )

        if difficulty_band == "easy":
            query = query.where(Question.difficulty_target <= 2)
        elif difficulty_band == "hard":
            query = query.where(Question.difficulty_target >= 4)

        if topic:
            query = query.where(Question.concept_tags.contains(topic))

        if exclude_ids:
            query = query.where(Question.id.not_in(exclude_ids))

        clause = _exclude_synthetic_clause()
        if clause is not None:
            query = query.where(clause)

        available = [q.id for q in query]
        # Phase 2 E5: theta-aware within-band selection (graceful fallback
        # to plain shuffle when rating_service is unavailable).
        available = _randomesque_pick(available)
        return available[:count]

    def select_questions_composed(self, measure, count, difficulty_band="medium",
                                   exclude_ids=None,
                                   exclude_user_seen=None,
                                   recent_seen_days=DEFAULT_RECENT_SEEN_DAYS,
                                   mastery_cooldown_days=DEFAULT_MASTERY_COOLDOWN_DAYS,
                                   target_theta=None,
                                   routing_tier=None):
        """
        Select `count` question IDs respecting real GRE question-type composition
        and keeping RC/DI clusters atomic.

        Verbal: 35% rc_single, 10% rc_multi, 5% rc_select_passage, 25% tc, 25% se
        Quant: 30% qc, 40% mcq_single, 5% mcq_multi, 5% numeric_entry, 20% data_interp

        Cluster-aware: when a selected RC/DI question shares a ``stimulus_id``
        with siblings, every sibling is pulled in together. If the full cluster
        would overflow the remaining budget, the assembler skips that cluster
        and tries the next candidate — partial clusters are never shipped.

        Cross-session dedup: when ``exclude_user_seen`` is a user_id, items
        touched within ``recent_seen_days`` and items answered correctly
        within ``mastery_cooldown_days`` are filtered out. If the dedup-
        adjusted pool can't satisfy the section, a warning is logged and
        the assembler falls back to the full pool (sans in-session exclusions)
        for the shortfall.

        Phase 3 S1 section-CAT: when ``target_theta`` is a float, candidate
        clusters are ranked by maximum-information at theta — approximating
        Fisher info via ``p*(1-p)`` where ``p`` is the Elo expected score
        derived from each item's rating in ``services.rating_service``. The
        ``difficulty_band`` filter becomes a SOFT weight (0.5 multiplier on
        off-band clusters) instead of a hard SQL ``WHERE``, so a hard-
        routed user still sees mostly band-4/5 items but the assembler
        never short-ships because of a thin band pool. When
        ``target_theta`` is ``None`` or rating_service is unavailable, the
        assembler preserves its pre-S1 band-switch behavior exactly —
        this is a non-breaking wire-up.

        Phase 7.2 (Path B): when ``routing_tier`` is one of
        ``'easy' / 'medium' / 'hard'``, candidate clusters are ranked by
        the ETS 3-tier difficulty mix in ``TIER_DIFFICULTY_MIX``. The
        tier wins over ``difficulty_band`` for the soft-weight stage
        (the difficulty mix is a finer-grained probability map than the
        coarse 3-band gate). ``target_theta`` continues to contribute as
        a side-signal for Fisher-info ranking when present. When
        ``routing_tier`` is ``None``, the legacy band-switch + theta path
        runs unchanged — backwards compatible.

        Deficits in any subtype are filled with the most flexible neighbor
        (rc_single for verbal, mcq_single for quant), then any remaining shortfall
        falls back to any matching question.
        """
        if measure == "verbal":
            composition = VERBAL_COMPOSITION
            fill_subtype = "rc_single"
        elif measure == "quant":
            composition = QUANT_COMPOSITION
            fill_subtype = "mcq_single"
        else:
            return self.select_questions(
                measure, count, difficulty_band, exclude_ids=exclude_ids)

        in_session_exclude = set(exclude_ids or [])
        dedup_exclude = set()
        # P4.P3: 7-day stimulus cooldown. Applied to cluster anchors
        # only (RC passage + DI chart) — singleton qids are already
        # governed by the qid-level ``ServedLog`` dedup window, which
        # is 30 days by default. The cluster cooldown closes the
        # loophole where a different qid under the same stimulus
        # re-surfaces within a week even though the passage/chart was
        # just served. Empty set when ``exclude_user_seen`` is off
        # (stateless topic drills etc.) so the cooldown is purely a
        # "known user" feature.
        cluster_cooldown_stims = set()
        if exclude_user_seen:
            dedup_exclude = _dedup_exclusions(
                user_id=exclude_user_seen,
                recent_days=recent_seen_days,
                mastery_cooldown_days=mastery_cooldown_days,
            )
            cluster_cooldown_stims = get_recently_served_stimulus_ids(
                days=DEFAULT_CLUSTER_COOLDOWN_DAYS,
                user_id=exclude_user_seen,
            )
        exclude = set(in_session_exclude) | set(dedup_exclude)

        # Compute target counts per subtype
        targets = self._composition_targets(composition, count)

        selected_ids = []
        # Track per-subtype shortfall so we can re-broaden the pool below.
        per_subtype_deficit = {}

        # ETS blueprint step 1: every Quant section anchors one DI set
        # (2-3 sibling questions under a graph/table stimulus). Pull the
        # cluster up-front so the rest of the composition targets assemble
        # around it. The ``data_interp`` subtype quota is reduced by the
        # cluster size so we don't double-count the DI slot.
        if measure == "quant":
            di_cluster = self._select_di_cluster(
                difficulty_band, exclude,
                exclude_stimulus_ids=cluster_cooldown_stims,
            )
            if di_cluster:
                selected_ids.extend(di_cluster)
                exclude.update(di_cluster)
                targets["data_interp"] = max(
                    0, targets.get("data_interp", 0) - len(di_cluster))
            else:
                logger.warning(
                    "DI-cluster gap: no DI items available (neither a "
                    "multi-sibling graph/table cluster nor a distinct-"
                    "stimulus singleton composition) at band=%s; section "
                    "will fall through with no DI block.", difficulty_band,
                )

            # Figure-floor pass: ensure the section contains at least
            # ``_quant_figure_floor(count, pool_size=...)`` items whose
            # stimulus carries an image or table. The floor scales down
            # when the live figure pool is shallow (P1.R2) so a small
            # bank doesn't get the same qids jammed into every section.
            # The DI cluster already contributed some; top up with
            # figure-bearing singletons (spread across QC / MC / NE / DI
            # subtypes) when the bank has no multi-sibling figure
            # clusters. Each pick is subtracted from its subtype's
            # remaining target so the composition ratios still balance.
            current_figure_count = self._count_figure_bearing(selected_ids)
            pool_size = self._quant_figure_pool_size(
                difficulty_band=difficulty_band)
            needed = max(
                0,
                _quant_figure_floor(count, pool_size=pool_size)
                - current_figure_count,
            )
            if needed > 0:
                figs = self._select_quant_figure_singletons(
                    count=needed,
                    difficulty_band=difficulty_band,
                    exclude=exclude,
                )
                for qid, subtype in figs:
                    targets[subtype] = max(0, targets.get(subtype, 0) - 1)
                    selected_ids.append(qid)
                    exclude.add(qid)

        # Verbal blueprint step 1: every Verbal section anchors on at
        # least one multi-question RC passage (real-GRE shape is 2-4
        # passages per section with 1 long + 1-2 short). Pull the full
        # passage cluster up-front and subtract from the rc_* targets so
        # the remaining slots fill out with TC / SE / other RC.
        if measure == "verbal":
            rc_target_sum = sum(
                targets.get(s, 0) for s in CLUSTERED_VERBAL_SUBTYPES)
            rc_anchor = self._select_rc_passage_anchor(
                difficulty_band=difficulty_band,
                exclude=exclude,
                max_size=rc_target_sum,
                prefer_size=RC_ANCHOR_PREFER_SIZE,
                exclude_stimulus_ids=cluster_cooldown_stims,
            )
            if rc_anchor:
                selected_ids.extend(rc_anchor)
                exclude.update(rc_anchor)
                rc_budget_left = rc_target_sum - len(rc_anchor)

                # Secondary anchor: try for a second (smaller) passage
                # when budget allows. Kaplan PT1 / Princeton diagnostic
                # both ship 2-4 passages per Verbal section. A primary
                # long (3-4 Q) + secondary short (2 Q) + leftover
                # singletons matches that shape. Only attempt if at
                # least RC_ANCHOR_MIN_SIZE budget remains so we don't
                # bisect a cluster; prefer 2-Q passages (``prefer_size
                # =2``) so we don't accidentally stack two long ones.
                if rc_budget_left >= RC_ANCHOR_MIN_SIZE:
                    secondary = self._select_rc_passage_anchor(
                        difficulty_band=difficulty_band,
                        exclude=exclude,
                        max_size=rc_budget_left,
                        prefer_size=RC_ANCHOR_MIN_SIZE,
                        min_size=RC_ANCHOR_MIN_SIZE,
                        exclude_stimulus_ids=cluster_cooldown_stims,
                    )
                    if secondary:
                        selected_ids.extend(secondary)
                        exclude.update(secondary)
                        rc_budget_left -= len(secondary)

                # Reallocate the rc_* budget: the anchors already
                # satisfy the "≥1 multi-Q passage" that the rc_multi
                # and rc_select_passage quotas exist to enforce, so
                # zero those and roll the leftover into rc_single.
                # Without this, the per-subtype targets still sum to
                # ``count`` while ``selected_ids`` grew by anchor_size,
                # so the final ``selected_ids[:count]`` slice can bisect
                # a cluster picked later by the subtype=None fallback
                # (breaks cluster atomicity — see
                # tests/test_cluster_aware_assembly.py).
                targets["rc_single"] = max(0, rc_budget_left)
                targets["rc_multi"] = 0
                targets["rc_select_passage"] = 0
            else:
                logger.warning(
                    "RC-passage-anchor gap: no verbal stimulus with >=%d "
                    "live RC siblings at band=%s; section will rely on "
                    "random cluster shuffle for any multi-Q passages.",
                    RC_ANCHOR_MIN_SIZE, difficulty_band,
                )

        for subtype, target_count in targets.items():
            if target_count == 0:
                continue
            taken = self._take_cluster_aware(
                measure=measure,
                subtype=subtype,
                target=target_count,
                difficulty_band=difficulty_band,
                exclude=exclude,
                target_theta=target_theta,
                routing_tier=routing_tier,
            )
            selected_ids.extend(taken)
            exclude.update(taken)
            per_subtype_deficit[subtype] = target_count - len(taken)

        deficit = sum(per_subtype_deficit.values())

        # Fill deficit with the flexible subtype, still cluster-aware
        if deficit > 0:
            extra = self._take_cluster_aware(
                measure=measure,
                subtype=fill_subtype,
                target=deficit,
                difficulty_band=difficulty_band,
                exclude=exclude,
                target_theta=target_theta,
                routing_tier=routing_tier,
            )
            selected_ids.extend(extra)
            exclude.update(extra)
            deficit -= len(extra)

        # Final fallback: any matching question. Respects clustering too —
        # if a candidate is RC/DI we still pull siblings together.
        if deficit > 0:
            extra = self._take_cluster_aware(
                measure=measure,
                subtype=None,  # any subtype
                target=deficit,
                difficulty_band=difficulty_band,
                exclude=exclude,
                target_theta=target_theta,
                routing_tier=routing_tier,
            )
            selected_ids.extend(extra)
            exclude.update(extra)
            deficit -= len(extra)

        # Pool-exhaustion fallback (P1.R4). Prior behavior dropped the
        # ``dedup_exclude`` set immediately on shortfall, which re-served
        # items the user had just seen in the last 30 days. The three
        # branches below widen the pool in increasing-aggressiveness order
        # so recently-seen items are preserved as long as possible:
        #
        #   1. Band widening (easy/hard → medium): keeps the FULL
        #      dedup_exclude but looks outside the requested difficulty
        #      band. Matches how ETS composes sections when a difficulty
        #      tier is thin.
        #   2. Partial dedup drop: keeps items seen within the last 7 days
        #      out of the pool but lets older-than-7d recently-seen /
        #      mastered items back in. Gives the learner variety while
        #      still protecting "I literally just saw this".
        #   3. Full drop: original behavior — relax every dedup exclusion
        #      except in-session. Logged at WARN so it shows up in the
        #      wild.
        if deficit > 0:
            # Branch 1: widen the difficulty band before touching dedup.
            if difficulty_band in ("easy", "hard"):
                logger.info(
                    "select_questions_composed: %s pool short %d items at "
                    "band=%s; widening to medium before relaxing dedup",
                    measure, deficit, difficulty_band,
                )
                extra = self._take_cluster_aware(
                    measure=measure,
                    subtype=None,
                    target=deficit,
                    difficulty_band="medium",
                    exclude=exclude,
                    target_theta=target_theta,
                    routing_tier=routing_tier,
                )
                selected_ids.extend(extra)
                exclude.update(extra)
                deficit -= len(extra)

        if deficit > 0 and dedup_exclude:
            # Branch 2: partial-dedup — protect only items served within
            # the last 7 days. Uses ``get_recently_seen_ids`` which reads
            # Response; on a fresh-launch user with an empty Response
            # table this helper returns the empty set, in which case we
            # short-circuit through to the full-drop branch below. After
            # R3 lands, this helper will additionally cover ServedLog.
            seven_day_floor = get_recently_seen_ids(
                days_back=7, user_id=exclude_user_seen or "local",
            )
            if seven_day_floor:
                logger.info(
                    "select_questions_composed: %s still short %d after "
                    "band-widening; relaxing dedup but protecting %d "
                    "items seen in last 7 days",
                    measure, deficit, len(seven_day_floor),
                )
                partial_exclude = (set(in_session_exclude)
                                   | set(selected_ids)
                                   | set(seven_day_floor))
                extra = self._take_cluster_aware(
                    measure=measure,
                    subtype=None,
                    target=deficit,
                    difficulty_band=difficulty_band,
                    exclude=partial_exclude,
                    target_theta=target_theta,
                    routing_tier=routing_tier,
                )
                selected_ids.extend(extra)
                exclude.update(extra)
                deficit -= len(extra)

        if deficit > 0 and dedup_exclude:
            # Branch 3: full drop — last resort. Preserves pre-R4
            # behavior so we never ship a short section, but logs at
            # WARN so we can see how often this actually fires.
            logger.warning(
                "select_questions_composed: %s pool exhausted after dedup "
                "(%d-item shortfall); dropping all dedup filters",
                measure, deficit,
            )
            relaxed_exclude = set(in_session_exclude) | set(selected_ids)
            extra = self._take_cluster_aware(
                measure=measure,
                subtype=None,
                target=deficit,
                difficulty_band=difficulty_band,
                exclude=relaxed_exclude,
                target_theta=target_theta,
                routing_tier=routing_tier,
            )
            selected_ids.extend(extra)
            deficit -= len(extra)

        if deficit > 0:
            logger.warning(
                "select_questions_composed: %s genuinely short %d items "
                "after all fallbacks",
                measure, deficit,
            )

        # Do NOT fully shuffle — cluster siblings must stay adjacent so the
        # UI can render the passage once at the top of its cluster. We
        # already assembled them cluster-at-a-time in pick order.
        final_picks = selected_ids[:count]

        # R3 — ServedLog write-through. Record every qid we're about to
        # hand to the caller so future selections (even from fresh-launch
        # users with no Response history) can exclude them. Guarded so a
        # schema/write failure NEVER blocks question selection.
        if final_picks and exclude_user_seen:
            _log_served(final_picks, user_id=exclude_user_seen,
                        session_id=None)

        return final_picks

    @staticmethod
    def _composition_targets(composition, count):
        """Round composition proportions to integer quotas per subtype
        so the quotas sum to ``count``. Largest ratio absorbs the
        rounding residual.
        """
        targets = {}
        running_sum = 0
        sorted_subtypes = sorted(composition.items(), key=lambda x: -x[1])
        for i, (subtype, ratio) in enumerate(sorted_subtypes):
            if i == len(sorted_subtypes) - 1:
                targets[subtype] = max(0, count - running_sum)
            else:
                t = round(count * ratio)
                targets[subtype] = t
                running_sum += t
        return targets

    def _take_cluster_aware(self, measure, subtype, target, difficulty_band,
                             exclude, target_theta=None, routing_tier=None):
        """Pick up to ``target`` question IDs, pulling full clusters atomically.

        ``subtype=None`` means "any subtype for this measure". Questions are
        grouped by ``_cluster_group`` (stimulus_id for RC/DI, q.id otherwise).
        A cluster is admitted only if it fits in the remaining budget; if the
        next candidate cluster is too large, we skip it and look for a smaller
        one instead of truncating.

        Phase 3 S1: when ``target_theta`` is a float, the ``difficulty_band``
        is applied as a SOFT WEIGHT instead of a hard SQL ``WHERE`` — pool
        query widens to all live items, clusters get a theta-info score
        ( ``sum p_i*(1-p_i)`` ), off-band clusters are down-weighted by
        0.5, and the top-ranked clusters feed into the existing
        ``_randomesque_pick`` for randomesque tie-breaking.

        Phase 7.2 (Path B, OQ1): when ``routing_tier`` is one of
        ``'easy' / 'medium' / 'hard'``, the ``TIER_DIFFICULTY_MIX`` table
        replaces the band soft-weight stage. The tier weight is a
        finer-grained probability map across all 5 difficulty levels
        (band 1..5) so the section composition matches the BrightLink
        Prep tier shapes (report.md §3.4) without hard-gating any band.
        Tier weighting stacks with theta info when both are present.
        """
        if target <= 0:
            return []

        # Theta-active path uses the wide pool so it can soft-weight off-band.
        # When rating_service is unreachable (or target_theta is None) we stay
        # on the legacy hard-WHERE path — no regression.
        theta_active = target_theta is not None
        ratings_map = {}
        probe = None
        if theta_active:
            try:
                from services import rating_service  # local import
                probe = rating_service.get_rating
            except Exception:
                theta_active = False
                probe = None

        # Pre-probe: if target_theta was requested but rating_service yields
        # no ratings for the measure's live pool at all, degrade to legacy
        # hard-WHERE band filter BEFORE we build the query. This keeps the
        # section-assembly behaviour of "band=hard → every pick >= 4"
        # intact for fresh DBs / missing-rating environments.
        if theta_active and probe is not None:
            from models.database import ItemRating
            if ItemRating.select().limit(1).count() == 0:
                theta_active = False

        # Phase 7.2 (Path B) — tier routing. When the caller supplies a
        # ``routing_tier`` that maps to ``TIER_DIFFICULTY_MIX``, the tier
        # mix replaces the hard-band WHERE during pool query and supplies
        # a probability weight in cluster ranking. Like ``theta_active``,
        # this is a SOFT signal — pool stays wide so a thin level pool
        # never short-ships the section.
        tier_active = (routing_tier is not None
                       and routing_tier in TIER_DIFFICULTY_MIX)

        query = (Question
                 .select(Question.id, Question.subtype, Question.stimulus,
                         Question.difficulty_target)
                 .where((Question.measure == measure) &
                        (Question.status == "live")))
        if subtype is not None:
            query = query.where(Question.subtype == subtype)
        if not theta_active and not tier_active:
            if difficulty_band == "easy":
                query = query.where(Question.difficulty_target <= 2)
            elif difficulty_band == "hard":
                query = query.where(Question.difficulty_target >= 4)
        if exclude:
            query = query.where(Question.id.not_in(list(exclude)))

        clause = _exclude_synthetic_clause()
        if clause is not None:
            query = query.where(clause)

        candidates = list(query)
        if not candidates:
            return []

        # Group by cluster key
        clusters = {}
        cluster_diffs = {}  # cluster_key -> list of difficulty_targets
        for q in candidates:
            key = _cluster_group(q)
            clusters.setdefault(key, []).append(q.id)
            cluster_diffs.setdefault(key, []).append(q.difficulty_target or 3)

        cluster_keys = list(clusters.keys())
        if theta_active:
            # Fetch ratings for all candidate qids; if NONE have ratings,
            # degrade to legacy path so we don't silently skip CAT.
            qids_for_ratings = [q.id for q in candidates]
            ratings_map = {qid: probe(qid) for qid in qids_for_ratings}
            any_rated = any(v is not None for v in ratings_map.values())
            if not any_rated:
                # Belt-and-suspenders: we built the query wide (no band
                # WHERE) when theta was active. Now that we're falling
                # back, apply the band filter in-memory so the caller
                # still gets hard-WHERE semantics and we don't surface
                # off-band items via the degraded-rating path. When the
                # tier path is also active we keep clusters wide — the
                # tier weighting alone is enough to bias selection.
                theta_active = False
                if not tier_active:
                    if difficulty_band == "easy":
                        clusters = {
                            k: v for k, v in clusters.items()
                            if any((cluster_diffs[k][i] or 3) <= 2
                                   for i in range(len(v)))
                        }
                    elif difficulty_band == "hard":
                        clusters = {
                            k: v for k, v in clusters.items()
                            if any((cluster_diffs[k][i] or 3) >= 4
                                   for i in range(len(v)))
                        }
                    cluster_keys = list(clusters.keys())

        if theta_active or tier_active:
            cluster_keys = self._rank_clusters_by_info(
                cluster_keys, clusters, cluster_diffs, ratings_map,
                target_theta, difficulty_band,
                routing_tier=routing_tier,
            )
        else:
            # Legacy: random cluster order.
            random.shuffle(cluster_keys)

        picked = []
        remaining = target
        skipped_oversized = []

        for key in cluster_keys:
            cluster_ids = clusters[key]
            # Cluster types (RC/DI) may have additional siblings in the DB
            # that got filtered out by exclude/difficulty above. Re-fetch the
            # FULL sibling set so we never ship a partial cluster.
            if key[0] == "stim":
                stim_id = key[1]
                subtype_key = key[2]
                sibling_query = Question.select(Question.id).where(
                    (Question.stimulus == stim_id) &
                    (Question.status == "live")
                )
                if subtype_key == "rc":
                    # RC passages mix rc_single + rc_multi children; the
                    # whole passage-cluster must move together or the reader
                    # loses context.
                    sibling_query = sibling_query.where(
                        Question.subtype.in_(list(CLUSTERED_VERBAL_SUBTYPES))
                    )
                else:
                    sibling_query = sibling_query.where(
                        Question.subtype == subtype_key
                    )
                full_siblings = [q.id for q in sibling_query]
                # Skip cluster entirely if any sibling is excluded — we
                # refuse to split it.
                if any(sid in exclude for sid in full_siblings):
                    continue
                cluster_ids = full_siblings

            size = len(cluster_ids)
            if size == 0:
                continue
            if size <= remaining:
                picked.extend(cluster_ids)
                remaining -= size
                if remaining == 0:
                    break
            else:
                skipped_oversized.append((size, cluster_ids))

        # If we still have budget and skipped some oversized clusters,
        # there's nothing to do — we refuse to ship partial clusters.
        # Log for visibility.
        if remaining > 0 and skipped_oversized:
            logger.info(
                "_take_cluster_aware: %s/%s left %d slot(s) unfilled to "
                "avoid splitting %d oversized cluster(s)",
                measure, subtype, remaining, len(skipped_oversized),
            )

        return picked

    @staticmethod
    def _rank_clusters_by_info(cluster_keys, clusters, cluster_diffs,
                                ratings_map, target_theta, difficulty_band,
                                routing_tier=None):
        """Rank candidate clusters by approximate Fisher information at theta.

        For each item ``i`` in a cluster we compute an Elo expected score
        ``p_i = 1 / (1 + 10 ** ((rating_i - theta) / THETA_SCALE))`` and
        approximate its information as ``p_i * (1 - p_i)``. The cluster
        score is the sum of per-item info across its qids.

        Band soft-weight (P3.S1 spec): off-band clusters are multiplied
        by 0.5 so in-band items lead when info is comparable, but the
        band is never a hard gate. A cluster is "off-band" if NONE of
        its qids falls in the requested band.

        Phase 7.2 (Path B): when ``routing_tier`` is supplied, the tier
        difficulty mix from ``TIER_DIFFICULTY_MIX`` provides a
        finer-grained weight per item difficulty level (1..5). The
        per-item weight is averaged across the cluster, then multiplied
        into the existing info score. When ``target_theta`` is missing
        (e.g., fresh DB / no ratings), the tier weight alone drives
        ranking — info score collapses to a constant (1.0) so tier-mix
        is the entire signal.

        Theta-aware tie-break: top-5 most-informative clusters are
        shuffled uniformly so two back-to-back selections don't land
        the exact same clusters. Mirrors the ``_randomesque_pick``
        pattern at cluster granularity.

        Graceful: clusters whose qids have no rating get a neutral
        score (0.0) when only theta is active — they still appear, just
        not at the front. With tier active, no-rating clusters score by
        tier-mix only, which is still a meaningful signal.
        """
        THETA_SCALE = 0.4  # mirror rating_service.THETA_SCALE
        tier_mix = TIER_DIFFICULTY_MIX.get(routing_tier) if routing_tier else None
        theta_value = None
        try:
            theta_value = float(target_theta) if target_theta is not None else None
        except (TypeError, ValueError):
            theta_value = None

        if theta_value is None and tier_mix is None:
            # Neither signal — fall back to random.
            random.shuffle(cluster_keys)
            return cluster_keys

        def _score_cluster(key):
            """Return (score, in_band) for a candidate cluster."""
            qids = clusters.get(key, [])
            if not qids:
                return 0.0, False
            diffs = cluster_diffs.get(key, [])
            in_band = False
            info_total = 0.0
            tier_total = 0.0
            tier_n = 0
            n_with_info = 0
            for qid, diff in zip(qids, diffs):
                d = diff or 3
                # Theta info contribution (only when we have a rating).
                if theta_value is not None:
                    rating = ratings_map.get(qid)
                    if rating is not None:
                        try:
                            p = 1.0 / (1.0 + 10.0 ** (
                                (float(rating) - theta_value) / THETA_SCALE))
                            info_total += p * (1.0 - p)
                            n_with_info += 1
                        except (TypeError, ValueError, OverflowError):
                            pass
                # Tier-mix contribution (cluster level — average over qids).
                if tier_mix is not None:
                    tier_total += tier_mix.get(d, 0.0)
                    tier_n += 1
                # Band-membership flag (P3.S1 soft weight bookkeeping).
                if difficulty_band == "easy" and d <= 2:
                    in_band = True
                elif difficulty_band == "hard" and d >= 4:
                    in_band = True
                elif difficulty_band == "medium" and d == 3:
                    in_band = True

            # Compose the final score:
            #   * info_total (if theta active) — Fisher info at theta.
            #   * tier_weight (if tier active) — average TIER_DIFFICULTY_MIX
            #     value across the cluster's items.
            # Multiplicative when both present; tier alone when theta
            # collapses (no ratings); info alone when no tier.
            tier_weight = (tier_total / tier_n) if tier_n else 0.0
            if theta_value is not None and tier_mix is not None:
                # Floor the info contribution at a small constant so a
                # cluster with zero theta info but a strong tier weight
                # can still beat the noise floor.
                info_factor = info_total if n_with_info > 0 else 1e-3
                score = info_factor * (tier_weight + 0.05)
            elif tier_mix is not None:
                score = tier_weight
            else:
                score = info_total
            return score, in_band

        scored = []
        for key in cluster_keys:
            raw, in_band = _score_cluster(key)
            # Band soft weight: in-band clusters keep full score, off-band
            # are halved (P3.S1 spec). Skipped under tier path because the
            # tier mix already encodes a finer-grained per-level weight,
            # and applying both stacks the bias.
            if tier_mix is None:
                weight = 1.0 if in_band else 0.5
                bonus = 0.1 if in_band else 0.0
                scored.append((raw * weight + bonus, key))
            else:
                scored.append((raw, key))

        # Highest score first; deterministic tie-break on key repr so the
        # sort is stable before we inject randomness on the top slice.
        scored.sort(key=lambda pair: (-pair[0], repr(pair[1])))

        top_m = max(1, min(5, len(scored)))
        head_pool = scored[:top_m]
        tail = scored[top_m:]
        random.shuffle(head_pool)
        random.shuffle(tail)
        return [key for _score, key in (head_pool + tail)]

    def enforce_cluster_atomicity(self, question_ids, strict_count=False,
                                    max_oversize=3):
        """Rewrite a candidate question-ID list so RC/DI clusters stay atomic.

        Quick Drill (and any other custom assembly that doesn't go through
        ``select_questions_composed``) picks items one at a time from
        per-subtopic pools. That can leave a single sibling of a 3-question
        RC cluster in the list — the user then sees an orphan passage-
        question with no siblings, which breaks the real-GRE experience.

        This helper walks the list, preserving order, and for every item
        that belongs to a cluster either:

        1. Expands: pulls in every live sibling under the same
           ``stimulus_id`` (option "a" — matches real GRE RC presentation),
           so the user sees the full passage-cluster together. The cluster
           is inserted at the position of the original orphan, with
           siblings kept adjacent so the passage pane only has to render
           once.

        2. Drops: if ``strict_count=True`` and expanding would push the
           total past ``len(question_ids) + max_oversize``, the cluster
           (including the original orphan) is removed entirely rather than
           shipped partial.

        Non-cluster questions pass through unchanged. The returned list
        preserves relative order so any caller-chosen interleaving
        (e.g. Quick Drill's verbal/quant shuffle) is still honoured for
        the non-clustered questions.
        """
        if not question_ids:
            return list(question_ids)

        # Fetch subtype + stimulus for every candidate in one round-trip.
        rows = list(
            Question.select(Question.id, Question.subtype, Question.stimulus)
            .where(Question.id.in_(list(question_ids)))
        )
        by_id = {r.id: r for r in rows}

        target_budget = len(question_ids) + max_oversize
        seen_clusters = set()
        seen_ids = set()
        out = []

        for qid in question_ids:
            if qid in seen_ids:
                continue
            q = by_id.get(qid)
            if q is None:
                # Question vanished between selection and expansion (rare —
                # race with retirement). Skip rather than crash.
                continue

            key = _cluster_group(q)
            if key[0] != "stim":
                # Singleton — pass through.
                out.append(qid)
                seen_ids.add(qid)
                continue

            if key in seen_clusters:
                continue
            seen_clusters.add(key)

            # Pull the full live sibling set for this cluster.
            stim_id = key[1]
            subtype_key = key[2]
            sibling_query = Question.select(Question.id).where(
                (Question.stimulus == stim_id) &
                (Question.status == "live")
            )
            if subtype_key == "rc":
                sibling_query = sibling_query.where(
                    Question.subtype.in_(list(CLUSTERED_VERBAL_SUBTYPES))
                )
            else:
                sibling_query = sibling_query.where(
                    Question.subtype == subtype_key
                )
            clause = _exclude_synthetic_clause()
            if clause is not None:
                sibling_query = sibling_query.where(clause)
            siblings = [s.id for s in sibling_query]

            if not siblings:
                # Degenerate: the orphan's own row is the only sibling
                # (DB race). Emit it and move on.
                out.append(qid)
                seen_ids.add(qid)
                continue

            projected = len(out) + len(siblings) + (
                len(question_ids) - question_ids.index(qid) - 1
            )
            if strict_count and projected > target_budget:
                # Expanding would blow past the caller's budget. Drop the
                # whole cluster (orphan included) so the user never sees
                # a partial passage.
                seen_ids.update(siblings)
                continue

            # Keep siblings adjacent so the passage pane only renders once.
            for sid in siblings:
                if sid not in seen_ids:
                    out.append(sid)
                    seen_ids.add(sid)

        return out


    def _pool_for_subtype(self, measure, subtype, difficulty_band, exclude_ids):
        """Get all live question IDs for a measure/subtype with difficulty filter."""
        query = Question.select(Question.id).where(
            Question.measure == measure,
            Question.subtype == subtype,
            Question.status == "live",
        )
        if difficulty_band == "easy":
            query = query.where(Question.difficulty_target <= 2)
        elif difficulty_band == "hard":
            query = query.where(Question.difficulty_target >= 4)
        if exclude_ids:
            query = query.where(Question.id.not_in(list(exclude_ids)))
        clause = _exclude_synthetic_clause()
        if clause is not None:
            query = query.where(clause)
        return [q.id for q in query]

    def _select_di_cluster(self, difficulty_band, exclude_ids,
                            exclude_stimulus_ids=None):
        """Pick one Data-Interpretation set for a Quant section.

        Prefers a real 3-question cluster under a graph/table/chart
        stimulus (children can be any quant subtype — real DI sets mix
        mcq_single / qc / numeric_entry). Falls back to a 2-question
        cluster. When no multi-sibling cluster exists (current seed bank
        reality: every DI stimulus is a singleton), composes a DI block
        from three DIFFERENT singleton stimuli so picks spread across
        the whole DI pool instead of drawing 3-at-a-time from a tiny
        random shuffle — this was the root cause of DI items repeating
        by mock #2 in the repetition-floor benchmark.

        Atomicity: if any sibling of a real multi-cluster is already in
        ``exclude_ids``, the whole cluster is skipped rather than split.
        The distinct-stimuli fallback naturally respects exclusion since
        each pick is independent.

        ``exclude_stimulus_ids`` (P4.P3): a set of stimulus_ids to avoid
        even when no sibling qid is excluded — e.g. the 7-day cluster
        cooldown. Skips both multi-Q clusters *and* singleton-fallback
        picks whose parent stimulus is in this set.

        Returns a list of question IDs (0..DI_CLUSTER_TARGET_SIZE items),
        empty when nothing is available.
        """
        exclude_list = list(exclude_ids) if exclude_ids else []
        stim_block = set(exclude_stimulus_ids or ())
        cand = (
            Question.select(Question.stimulus_id,
                            fn.COUNT(Question.id).alias("n"))
            .join(Stimulus, on=(Stimulus.id == Question.stimulus_id))
            .where((Question.measure == "quant") &
                   (Question.status == "live") &
                   (Stimulus.stimulus_type.in_(list(DI_STIMULUS_TYPES))))
            .group_by(Question.stimulus_id)
            .having(fn.COUNT(Question.id) >= DI_CLUSTER_MIN_SIZE)
        )
        clause = _exclude_synthetic_clause()
        if clause is not None:
            cand = cand.where(clause)

        triples, pairs, singletons = [], [], []
        for row in cand:
            n = row.n
            if n >= DI_CLUSTER_TARGET_SIZE:
                triples.append(row.stimulus_id)
            elif n >= 2:
                pairs.append(row.stimulus_id)
            else:
                # n == 1 — stimulus with a single live child. Held for
                # the distinct-stimuli composition branch below.
                singletons.append(row.stimulus_id)

        random.shuffle(triples)
        random.shuffle(pairs)

        # Try real multi-sibling clusters first (preserves GRE "one
        # chart, three questions" shape when the bank supports it).
        for stim_id in triples + pairs:
            if stim_id in stim_block:
                # P4.P3 cluster cooldown — stimulus already served
                # within the configured window; skip entirely.
                continue
            siblings = list(
                Question.select(Question.id)
                .where((Question.stimulus_id == stim_id) &
                       (Question.measure == "quant") &
                       (Question.status == "live"))
            )
            sibling_ids = [q.id for q in siblings]
            # Drop cluster entirely if any sibling is already excluded.
            if exclude_ids and any(qid in exclude_ids for qid in sibling_ids):
                continue
            # Difficulty band: require at least one sibling in band;
            # keeping a full DI cluster together matches real test behavior.
            if difficulty_band == "easy":
                if not self._any_sibling_matches(sibling_ids, "<=", 2):
                    continue
            elif difficulty_band == "hard":
                if not self._any_sibling_matches(sibling_ids, ">=", 4):
                    continue

            random.shuffle(sibling_ids)
            return sibling_ids[:DI_CLUSTER_TARGET_SIZE]

        # Distinct-stimuli composition fallback.
        #
        # The seed bank currently has 0 graph/table/chart stimuli with
        # >=2 live children, so the cluster branch above always falls
        # through. Previously the code then drew ``DI_CLUSTER_TARGET_SIZE``
        # items in a single shuffle from a 14-item pool (the
        # ``subtype == 'data_interp'`` slice only), and nothing forced
        # picks onto distinct stimuli — so DI repeats were showing up by
        # mock #2 in a 20-mock binge.
        #
        # Approach (A) per docs/implementation_plan_2026_05_12.md Phase
        # 1 R1: compose the DI block by picking ONE child qid from each
        # of three DIFFERENT singleton stimuli in the graph/table/chart
        # umbrella. This pool is ~49 stimuli (vs. 14 subtype-filtered)
        # because real DI children are labelled as qc / mcq_single /
        # numeric_entry just as often as data_interp. Spreading across
        # the full set is the whole point of R1.
        if not singletons:
            return []

        random.shuffle(singletons)
        # Build a per-stimulus "preferred qid" map with one query so we
        # don't N+1. Pull id + content together so we can still rank
        # image-bearing stimuli over HTML-table ones.
        singleton_children = (
            Question.select(Question.id,
                            Question.stimulus_id,
                            Question.difficulty_target,
                            Stimulus.content.alias("stim_content"))
            .join(Stimulus, on=(Stimulus.id == Question.stimulus))
            .where((Question.measure == "quant") &
                   (Question.status == "live") &
                   (Question.stimulus_id.in_(singletons)))
        )
        clause = _exclude_synthetic_clause()
        if clause is not None:
            singleton_children = singleton_children.where(clause)

        # Bucket: stim_id -> list of candidate qids (there may be >1 if
        # the outer HAVING group collapsed duplicates). Rank: image-bearing
        # first, HTML-table second. Respect difficulty_band + exclude_ids.
        image_buckets = {}   # stim_id -> list[qid]
        table_buckets = {}   # stim_id -> list[qid]
        for row in singleton_children:
            qid = row.id
            if exclude_ids and qid in exclude_ids:
                continue
            if row.stimulus_id in stim_block:
                # P4.P3 cooldown — same chart shown within window.
                continue
            if difficulty_band == "easy" and (row.difficulty_target or 0) > 2:
                continue
            if difficulty_band == "hard" and (row.difficulty_target or 0) < 4:
                continue
            content = getattr(row, "stim_content", "") or ""
            stim_id = row.stimulus_id
            if "<img" in content or "data:image/" in content:
                image_buckets.setdefault(stim_id, []).append(qid)
            else:
                table_buckets.setdefault(stim_id, []).append(qid)

        # Order stim_ids: image-bearing shuffled, then table-bearing
        # shuffled; a stimulus that appears in both lists (unlikely
        # since each stim is one content blob) is kept only in the
        # image list.
        image_stims = list(image_buckets.keys())
        table_stims = [s for s in table_buckets.keys()
                       if s not in image_buckets]
        random.shuffle(image_stims)
        random.shuffle(table_stims)

        picked = []
        for stim_id in image_stims + table_stims:
            if len(picked) >= DI_CLUSTER_TARGET_SIZE:
                break
            candidates = (image_buckets.get(stim_id, [])
                          + table_buckets.get(stim_id, []))
            if not candidates:
                continue
            random.shuffle(candidates)
            picked.append(candidates[0])
        return picked

    @staticmethod
    def _any_sibling_matches(sibling_ids, op, threshold):
        """True iff at least one sibling's ``difficulty_target`` matches
        the band test (``<=`` for easy, ``>=`` for hard).
        """
        if not sibling_ids:
            return False
        q = Question.select(fn.COUNT(Question.id)).where(
            Question.id.in_(list(sibling_ids)))
        if op == "<=":
            q = q.where(Question.difficulty_target <= threshold)
        elif op == ">=":
            q = q.where(Question.difficulty_target >= threshold)
        return (q.scalar() or 0) > 0

    @staticmethod
    def _quant_figure_pool_size(difficulty_band=None):
        """Return the live, figure-bearing Quant pool size.

        Used by ``select_questions_composed`` to scale
        ``_quant_figure_floor`` down when the pool is too shallow to
        support the nominal 3/4-per-section floor without forcing a few
        items into heavy rotation. The query mirrors the filter applied
        by ``_select_quant_figure_singletons`` so pool size and pick
        source stay in sync.
        """
        query = (
            Question.select(fn.COUNT(Question.id))
            .join(Stimulus, on=(Stimulus.id == Question.stimulus))
            .where((Question.measure == "quant") &
                   (Question.status == "live"))
            .where(
                (Stimulus.content.contains("<img")) |
                (Stimulus.content.contains("data:image/")) |
                (Stimulus.content.contains("<table"))
            )
        )
        if difficulty_band == "easy":
            query = query.where(Question.difficulty_target <= 2)
        elif difficulty_band == "hard":
            query = query.where(Question.difficulty_target >= 4)
        clause = _exclude_synthetic_clause()
        if clause is not None:
            query = query.where(clause)
        return query.scalar() or 0

    @staticmethod
    def _count_figure_bearing(qids):
        """Count how many of ``qids`` have a figure-bearing stimulus
        (image or table). Used by ``select_questions_composed`` to
        decide whether the figure-floor top-up is still needed after
        the DI cluster and earlier picks."""
        if not qids:
            return 0
        rows = (
            Question.select(Question.id, Stimulus.content.alias("c"))
            .join(Stimulus, on=(Stimulus.id == Question.stimulus))
            .where(Question.id.in_(list(qids)))
        )
        n = 0
        for r in rows:
            c = getattr(r, "c", "") or ""
            if "<img" in c or "data:image/" in c or "<table" in c:
                n += 1
        return n

    def _select_quant_figure_singletons(self, count, difficulty_band, exclude):
        """Pick ``count`` live Quant items whose stimulus carries a figure
        (image or table), subtype-agnostic.

        Prefers real images (``<img>`` / ``data:image/``) over pure HTML
        tables so a user who complains about "no figure-based questions"
        gets actual geometry / coordinate / chart renderings, not just
        the ai_generated DI table blocks. Returns a list of
        ``(qid, subtype)`` tuples so the caller can reduce per-subtype
        targets for each pick.

        The Quant bank has 45 figure-bearing singletons (Manhattan 5lb
        geometry diagrams) that ``_select_di_cluster`` can't surface
        (they all have ``n_siblings=1`` so fail the ``>=2`` gate). This
        helper fills the figure-floor gap directly.
        """
        if count <= 0:
            return []
        query = (
            Question.select(Question.id, Question.subtype,
                            Stimulus.content.alias("stim_content"))
            .join(Stimulus, on=(Stimulus.id == Question.stimulus))
            .where((Question.measure == "quant") &
                   (Question.status == "live"))
            .where(
                (Stimulus.content.contains("<img")) |
                (Stimulus.content.contains("data:image/")) |
                (Stimulus.content.contains("<table"))
            )
        )
        if difficulty_band == "easy":
            query = query.where(Question.difficulty_target <= 2)
        elif difficulty_band == "hard":
            query = query.where(Question.difficulty_target >= 4)
        if exclude:
            query = query.where(Question.id.not_in(list(exclude)))
        clause = _exclude_synthetic_clause()
        if clause is not None:
            query = query.where(clause)

        image_bearing = []
        table_bearing = []
        for q in query:
            content = getattr(q, "stim_content", "") or ""
            if "<img" in content or "data:image/" in content:
                image_bearing.append((q.id, q.subtype))
            else:
                table_bearing.append((q.id, q.subtype))
        random.shuffle(image_bearing)
        random.shuffle(table_bearing)
        return (image_bearing + table_bearing)[:count]

    def _select_rc_passage_anchor(self, difficulty_band, exclude, max_size,
                                   prefer_size=RC_ANCHOR_PREFER_SIZE,
                                   min_size=RC_ANCHOR_MIN_SIZE,
                                   exclude_stimulus_ids=None):
        """Pick one multi-question RC passage cluster for a Verbal section.

        Mirrors ``_select_di_cluster``: queries stimuli that host ≥
        ``min_size`` live Verbal RC children, prefers clusters with
        ≥ ``prefer_size`` siblings before smaller ones, respects the
        difficulty band, and refuses to split siblings. The returned
        list is the full sibling set for the chosen stimulus, atomic.

        Real GRE Verbal sections always include at least one passage
        with 2–4 linked questions (Kaplan/Princeton practice tests: 2–4
        passages per section with 1 long + 1–2 short). Without this
        anchor the assembler's random shuffle of RC cluster keys picks
        single-child singletons 83% of the time and ships sections with
        every RC item standalone — user-reported symptom ("I'm not
        seeing any RC questions [with multiple questions per passage]").

        ``max_size`` caps the anchor so a 5-Q passage doesn't blow the
        RC slot budget. Callers pass the sum of ``rc_single + rc_multi +
        rc_select_passage`` targets for a primary anchor, and the
        remaining RC budget for a secondary anchor.

        ``prefer_size`` lets callers bias toward long passages (primary
        anchor, default 3) or short passages (secondary anchor, pass 2).

        ``exclude_stimulus_ids`` (P4.P3): a set of passage stim_ids to
        skip regardless of sibling exclusion state — e.g. the 7-day
        passage cooldown set from ``get_recently_served_stimulus_ids``.
        """
        if max_size < min_size:
            return []
        stim_block = set(exclude_stimulus_ids or ())
        cand = (
            Question.select(Question.stimulus_id,
                            fn.COUNT(Question.id).alias("n"))
            .where((Question.measure == "verbal") &
                   (Question.status == "live") &
                   (Question.subtype.in_(list(CLUSTERED_VERBAL_SUBTYPES))) &
                   Question.stimulus_id.is_null(False))
            .group_by(Question.stimulus_id)
            .having(fn.COUNT(Question.id) >= min_size)
        )
        clause = _exclude_synthetic_clause()
        if clause is not None:
            cand = cand.where(clause)

        prefer, fallback = [], []
        for row in cand:
            if row.n >= prefer_size:
                prefer.append(row.stimulus_id)
            else:
                fallback.append(row.stimulus_id)
        random.shuffle(prefer)
        random.shuffle(fallback)

        for stim_id in prefer + fallback:
            if stim_id in stim_block:
                # P4.P3 — passage served within cooldown window; skip.
                continue
            siblings = list(
                Question.select(Question.id)
                .where((Question.stimulus == stim_id) &
                       (Question.status == "live") &
                       (Question.subtype.in_(list(CLUSTERED_VERBAL_SUBTYPES))))
            )
            sibling_ids = [q.id for q in siblings]
            if len(sibling_ids) > max_size:
                continue  # would overflow the RC budget
            if exclude and any(sid in exclude for sid in sibling_ids):
                continue
            if difficulty_band == "easy":
                if not self._any_sibling_matches(sibling_ids, "<=", 2):
                    continue
            elif difficulty_band == "hard":
                if not self._any_sibling_matches(sibling_ids, ">=", 4):
                    continue
            return sibling_ids
        return []

    def select_awa_prompt(self):
        """Select a random AWA prompt. Returns [prompt_id]."""
        prompts = list(AWAPrompt.select(AWAPrompt.id))
        if not prompts:
            return []
        chosen = random.choice(prompts)
        return [chosen.id]

    def get_question(self, question_id):
        """Fetch a full question with its options."""
        q = Question.get_or_none(Question.id == question_id)
        if q is None:
            return None

        options = list(
            QuestionOption.select()
            .where(QuestionOption.question == q)
            .order_by(QuestionOption.option_label)
        )

        numeric = None
        if q.subtype == "numeric_entry":
            numeric = NumericAnswer.get_or_none(
                NumericAnswer.question == q
            )

        stimulus = None
        if q.stimulus_id:
            stimulus = Stimulus.get_or_none(Stimulus.id == q.stimulus_id)

        return {
            "id": q.id,
            "measure": q.measure,
            "subtype": q.subtype,
            "prompt": q.prompt,
            "difficulty": q.difficulty_target,
            "tags": q.get_tags(),
            "explanation": q.explanation,
            "stimulus": {
                "type": stimulus.stimulus_type,
                "title": stimulus.title,
                "content": stimulus.content,
            } if stimulus else None,
            "options": [
                {
                    "label": o.option_label,
                    "text": o.option_text,
                    "is_correct": o.is_correct,
                }
                for o in options
            ],
            "numeric_answer": {
                "exact_value": numeric.exact_value,
                "numerator": numeric.numerator,
                "denominator": numeric.denominator,
                "tolerance": numeric.tolerance,
                "mode": getattr(numeric, "mode", "auto"),
            } if numeric else None,
        }

    def get_awa_prompt(self, prompt_id):
        """Fetch an AWA prompt by ID."""
        p = AWAPrompt.get_or_none(AWAPrompt.id == prompt_id)
        if p is None:
            return None
        return {
            "id": p.id,
            "prompt_text": p.prompt_text,
            "instructions": p.instructions,
        }

    def get_question_count(self, measure=None):
        """Count available live questions, optionally filtered by measure."""
        query = Question.select().where(Question.status == "live")
        if measure:
            query = query.where(Question.measure == measure)
        return query.count()

    def get_topics(self, measure):
        """Get distinct concept tags for a measure."""
        questions = (Question.select(Question.concept_tags)
                     .where(Question.measure == measure, Question.status == "live"))
        tags = set()
        for q in questions:
            for tag in q.get_tags():
                tags.add(tag)
        return sorted(tags)

    def subtopic_summary(self, user_id: str = "local"):
        """Return per-subtopic stats joining the bank, mastery, and lessons.

        Single round-trip per source (Question / MasteryRecord / Lesson) to
        avoid N+1; the rest is a Python merge keyed by subtopic name.

        Returns: {subtopic: {
            "question_count": int,
            "mastery": float|None,
            "attempts": int,
            "has_lesson": bool,
        }}
        """
        from peewee import fn
        from models.database import MasteryRecord, Lesson

        counts = {}
        rows = (Question
                .select(Question.subtopic,
                        fn.COUNT(Question.id).alias("cnt"))
                .where((Question.subtopic != "") &
                       (Question.status == "live"))
                .group_by(Question.subtopic)
                .dicts())
        for row in rows:
            counts[row["subtopic"]] = row["cnt"]

        mastery = {m.subtopic: (m.mastery_score, m.attempts)
                   for m in MasteryRecord
                   .select()
                   .where(MasteryRecord.user_id == user_id)}

        lesson_subs = {l.subtopic for l in Lesson.select(Lesson.subtopic)}

        out = {}
        for sub, cnt in counts.items():
            m_score, m_attempts = mastery.get(sub, (None, 0))
            out[sub] = {
                "question_count": cnt,
                "mastery": m_score,
                "attempts": m_attempts,
                "has_lesson": sub in lesson_subs,
            }
        # Surface mastered-but-no-question rows too (rare; defends against
        # mid-migration drift).
        for sub, (m_score, m_attempts) in mastery.items():
            out.setdefault(sub, {
                "question_count": 0,
                "mastery": m_score,
                "attempts": m_attempts,
                "has_lesson": sub in lesson_subs,
            })
        return out


# ── User flagging API ────────────────────────────────────────────────

VALID_FLAG_REASONS = {
    "wrong_answer", "wrong_explanation", "doesnt_make_sense", "other",
}


def flag_question(question_id: int, reason: str, note: str = "",
                  user_id: str = "local") -> bool:
    """Record a user's complaint about a question.

    Idempotent per (user, question, reason) — re-clicking the report
    button doesn't create a duplicate row.

    Returns True if a new flag row was created (or an old one updated),
    False if the inputs were invalid.
    """
    if reason not in VALID_FLAG_REASONS:
        logger.warning("flag_question: invalid reason %r", reason)
        return False
    q = Question.get_or_none(Question.id == question_id)
    if q is None:
        logger.warning("flag_question: missing question %d", question_id)
        return False

    existing = QuestionFlag.get_or_none(
        QuestionFlag.question == q,
        QuestionFlag.user_id == user_id,
        QuestionFlag.reason == reason,
    )
    if existing is not None:
        # Update note (user may add detail on a re-report).
        if note and note != existing.note:
            existing.note = note
            existing.save()
        return True

    QuestionFlag.create(
        question=q,
        user_id=user_id,
        reason=reason,
        note=note or "",
    )
    logger.info(
        "user %s flagged question %d as %s", user_id, question_id, reason,
    )
    return True


def auto_retire_flagged_questions(threshold: int = AUTO_RETIRE_THRESHOLD_DEFAULT,
                                  single_user_threshold: int = 1) -> list:
    """Retire questions with enough distinct-user flags.

    In single-user mode (every flag from `local`) we still want a way to
    auto-retire after a clear signal — the per-question count of *flag
    rows* must reach `single_user_threshold`. In multi-user mode the
    `threshold` of *distinct user_ids* applies. Whichever rule trips
    first wins.

    Returns the list of question IDs that were newly retired.
    """
    distinct_users_per_q = (
        QuestionFlag
        .select(QuestionFlag.question, fn.COUNT(fn.DISTINCT(QuestionFlag.user_id)).alias("n"))
        .group_by(QuestionFlag.question)
    )
    rows_per_q = (
        QuestionFlag
        .select(QuestionFlag.question, fn.COUNT(QuestionFlag.id).alias("n"))
        .group_by(QuestionFlag.question)
    )

    distinct = {r.question_id: r.n for r in distinct_users_per_q}
    total = {r.question_id: r.n for r in rows_per_q}

    candidates = set()
    for qid, n in distinct.items():
        if n >= threshold:
            candidates.add(qid)
    for qid, n in total.items():
        if n >= single_user_threshold + 2:  # 3+ rows even from one user
            candidates.add(qid)

    if not candidates:
        return []

    with db.atomic():
        retired_now = list(
            Question
            .select(Question.id)
            .where(Question.id.in_(candidates), Question.status != "retired")
        )
        retired_ids = [q.id for q in retired_now]
        if retired_ids:
            (Question
             .update(status="retired")
             .where(Question.id.in_(retired_ids))
             .execute())
            logger.info("auto-retired %d flagged questions: %s",
                        len(retired_ids), retired_ids)
    return retired_ids


def get_user_flag_for(question_id: int, user_id: str = "local"):
    """Return this user's existing flag on a question (if any), else None."""
    return (
        QuestionFlag
        .get_or_none(
            QuestionFlag.question == question_id,
            QuestionFlag.user_id == user_id,
        )
    )
