"""
Question bank service — loads, filters, and selects questions from the database.
"""
import random
from datetime import datetime, timedelta

from peewee import fn

from models.database import (
    db, Question, QuestionOption, NumericAnswer, Stimulus,
    AWAPrompt, VocabWord, Response, QuestionFlag,
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
# ``select_questions_composed``.
DEFAULT_RECENT_SEEN_DAYS = 30
DEFAULT_MASTERY_COOLDOWN_DAYS = 60


# Threshold of distinct flag-submitting users at which a question is
# auto-retired by `auto_retire_flagged_questions`. Conservative because
# the local-only app today has only one user — set to 1 in single-user
# mode and bumped to 3 if user_id ever varies.
AUTO_RETIRE_THRESHOLD_DEFAULT = 3


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
    "qc": 0.30,                  # ~4 per 12-question section
    "mcq_single": 0.40,          # ~5 per 12-question section
    "mcq_multi": 0.05,           # ~1 per section
    "numeric_entry": 0.05,       # ~1 per section
    "data_interp": 0.20,         # ~2-3 per section
}


def get_recently_seen_ids(days_back: int = 14, user_id: str = "local"):
    """Return question IDs the user has answered in the last N days.

    Used to avoid showing the same questions in consecutive sessions.
    """
    cutoff = datetime.now() - timedelta(days=days_back)
    rows = Response.select(Response.question_id).where(
        Response.created_at >= cutoff
    ).distinct()
    return set(r.question_id for r in rows)


def _cluster_group(q):
    """Return the cluster key for a question.

    Cluster subtypes (RC / DI) group under their ``stimulus_id``; everything
    else is its own singleton cluster keyed by question id so the assembler
    can treat every selection uniformly.
    """
    subtype = getattr(q, "subtype", None)
    stim_id = getattr(q, "stimulus_id", None)
    if subtype in CLUSTER_SUBTYPES and stim_id:
        return ("stim", stim_id, subtype)
    return ("q", q.id)


def _user_recent_seen(user_id: str = "local",
                      days: int = DEFAULT_RECENT_SEEN_DAYS):
    """Question IDs the user has touched within the last ``days`` days.

    The app is single-user today, so ``user_id`` is informational; every
    response in the DB is this user's. Kept as a parameter so the helper
    grows cleanly when multi-user arrives.
    """
    if days is None or days <= 0:
        return set()
    cutoff = datetime.now() - timedelta(days=days)
    rows = (Response
            .select(Response.question_id)
            .where(Response.created_at >= cutoff)
            .distinct())
    return set(r.question_id for r in rows)


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
                      mastery_cooldown_days: int):
    """Combined dedup set used by ``select_questions_composed``.

    Returns a ``set[int]`` of question IDs to leave out of the pool:
      * anything the user touched in the last ``recent_days`` days, plus
      * anything the user answered correctly in the last
        ``mastery_cooldown_days`` days (spaced-repetition cooldown).

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
    return recent | mastered


def user_stats(user_id: str = "local",
               recent_days: int = DEFAULT_RECENT_SEEN_DAYS,
               mastery_cooldown_days: int = DEFAULT_MASTERY_COOLDOWN_DAYS):
    """UX helper: how much of the pool has the user chewed through?

    Returns a dict ``{seen_count, total_pool, dedup_days_active,
    mastery_cooldown_days, dedup_active_count}`` so the app can render a
    "you've seen 487 of 1544 items" nudge.
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
    )
    return {
        "seen_count": seen_count,
        "total_pool": total_pool,
        "dedup_days_active": recent_days,
        "mastery_cooldown_days": mastery_cooldown_days,
        "dedup_active_count": len(dedup_active),
    }


class QuestionBankService:
    """Query and select questions for test assembly and drills."""

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

        random.shuffle(never_seen)
        random.shuffle(wrong_before)
        random.shuffle(right_before)

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
        random.shuffle(available)
        return available[:count]

    def select_questions_composed(self, measure, count, difficulty_band="medium",
                                   exclude_ids=None,
                                   exclude_user_seen=None,
                                   recent_seen_days=DEFAULT_RECENT_SEEN_DAYS,
                                   mastery_cooldown_days=DEFAULT_MASTERY_COOLDOWN_DAYS):
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
        if exclude_user_seen:
            dedup_exclude = _dedup_exclusions(
                user_id=exclude_user_seen,
                recent_days=recent_seen_days,
                mastery_cooldown_days=mastery_cooldown_days,
            )
        exclude = set(in_session_exclude) | set(dedup_exclude)

        # Compute target counts per subtype
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

        selected_ids = []
        # Track per-subtype shortfall so we can re-broaden the pool below.
        per_subtype_deficit = {}

        for subtype, target_count in targets.items():
            if target_count == 0:
                continue
            taken = self._take_cluster_aware(
                measure=measure,
                subtype=subtype,
                target=target_count,
                difficulty_band=difficulty_band,
                exclude=exclude,
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
            )
            selected_ids.extend(extra)
            exclude.update(extra)
            deficit -= len(extra)

        # Pool-exhaustion fallback: ignore dedup (but not in-session exclusions)
        # and try once more so we never ship a short section.
        if deficit > 0 and dedup_exclude:
            logger.warning(
                "select_questions_composed: %s pool exhausted after dedup "
                "(%d-item shortfall); relaxing dedup filter",
                measure, deficit,
            )
            relaxed_exclude = set(in_session_exclude) | set(selected_ids)
            extra = self._take_cluster_aware(
                measure=measure,
                subtype=None,
                target=deficit,
                difficulty_band=difficulty_band,
                exclude=relaxed_exclude,
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
        return selected_ids[:count]

    def _take_cluster_aware(self, measure, subtype, target, difficulty_band,
                             exclude):
        """Pick up to ``target`` question IDs, pulling full clusters atomically.

        ``subtype=None`` means "any subtype for this measure". Questions are
        grouped by ``_cluster_group`` (stimulus_id for RC/DI, q.id otherwise).
        A cluster is admitted only if it fits in the remaining budget; if the
        next candidate cluster is too large, we skip it and look for a smaller
        one instead of truncating.
        """
        if target <= 0:
            return []

        query = (Question
                 .select(Question.id, Question.subtype, Question.stimulus)
                 .where((Question.measure == measure) &
                        (Question.status == "live")))
        if subtype is not None:
            query = query.where(Question.subtype == subtype)
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
        for q in candidates:
            key = _cluster_group(q)
            clusters.setdefault(key, []).append(q.id)

        # Shuffle cluster order (seeds) but keep ids inside each cluster
        # stable — the full sibling set lands together.
        cluster_keys = list(clusters.keys())
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
                full_siblings = [
                    q.id for q in Question.select(Question.id).where(
                        (Question.stimulus == stim_id) &
                        (Question.subtype == subtype_key) &
                        (Question.status == "live")
                    )
                ]
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
