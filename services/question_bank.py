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
        all_qs = list(Question.select(Question.id, Question.difficulty_target)
                      .where((Question.subtopic == subtopic) &
                             (Question.status == "live")))
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

        available = [q.id for q in query]
        random.shuffle(available)
        return available[:count]

    def select_questions_composed(self, measure, count, difficulty_band="medium",
                                   exclude_ids=None):
        """
        Select `count` question IDs respecting real GRE question-type composition.

        Verbal: 35% rc_single, 10% rc_multi, 5% rc_select_passage, 25% tc, 25% se
        Quant: 30% qc, 40% mcq_single, 5% mcq_multi, 5% numeric_entry, 20% data_interp

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

        exclude = set(exclude_ids or [])

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

        # Pull pool per subtype
        selected_ids = []
        deficit = 0

        for subtype, target_count in targets.items():
            if target_count == 0:
                continue
            pool = self._pool_for_subtype(measure, subtype, difficulty_band, exclude)
            random.shuffle(pool)
            taken = pool[:target_count]
            selected_ids.extend(taken)
            exclude.update(taken)
            deficit += target_count - len(taken)

        # Fill deficit with the flexible subtype
        if deficit > 0:
            extra_pool = self._pool_for_subtype(
                measure, fill_subtype, difficulty_band, exclude)
            random.shuffle(extra_pool)
            extra = extra_pool[:deficit]
            selected_ids.extend(extra)
            exclude.update(extra)
            deficit -= len(extra)

        # Final fallback: any matching question
        if deficit > 0:
            fallback_query = Question.select(Question.id).where(
                Question.measure == measure,
                Question.status == "live",
                Question.id.not_in(list(exclude)),
            )
            if difficulty_band == "easy":
                fallback_query = fallback_query.where(Question.difficulty_target <= 2)
            elif difficulty_band == "hard":
                fallback_query = fallback_query.where(Question.difficulty_target >= 4)
            fallback = [q.id for q in fallback_query]
            random.shuffle(fallback)
            selected_ids.extend(fallback[:deficit])

        random.shuffle(selected_ids)
        return selected_ids[:count]

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
