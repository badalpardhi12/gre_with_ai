"""
Question bank service — loads, filters, and selects questions from the database.
"""
import random
from collections import defaultdict
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
# Source: data/audits/ets_blueprint_2026.md — cross-verified against ETS
# quotes + Manhattan Prep + Target Test Prep + CrackVerbal + Magoosh.
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
    "data_interp": 0.20,         # ~2-3 per section (delivered as a cluster)
}

# ETS blueprint: every Quant section contains exactly one DI *set* of 3
# questions sharing a single chart/table stimulus. We prefer real clusters
# (rows whose `stimulus.stimulus_type` is graph/table/chart) even when the
# question subtype is mcq_single / qc / numeric_entry. The legacy seed
# marks solo chart items with `subtype='data_interp'` — those are used as
# the fallback only.
DI_STIMULUS_TYPES = ("graph", "table", "chart")
DI_CLUSTER_TARGET_SIZE = 3
DI_CLUSTER_MIN_SIZE = 2  # degrade gracefully when a 3-cluster is unavailable

# Subtypes whose questions must be delivered with every live sibling that
# shares their stimulus_id. Missing a single sibling breaks the passage
# experience (the reader has already scanned the shared passage).
CLUSTERED_VERBAL_SUBTYPES = ("rc_single", "rc_multi", "rc_select_passage")


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
                                   exclude_ids=None, user_id="local",
                                   avoid_recent_days=14):
        """
        Select `count` question IDs respecting the real GRE composition and
        **cluster atomicity** rules (see data/audits/ets_blueprint_2026.md).

        Guarantees:
        - Verbal: any RC question picked brings every live sibling that shares
          its stimulus_id with it; clusters that would overflow the remaining
          budget are skipped rather than split.
        - Quant: every section receives exactly one Data-Interpretation set
          (2-3 sibling questions under a graph/table stimulus) when the DB
          can supply one; if not, we fall back to solo `data_interp` items
          and log an explicit gap warning.
        - Recently-seen questions (past `avoid_recent_days` days for this
          `user_id`) are pre-excluded unless doing so would starve the pool.
        - `exclude_ids` (in-exam dedup, e.g. S1→S2) is also honored.

        Composition targets:
          Verbal: 35% rc_single, 10% rc_multi, 5% rc_select_passage,
                  25% tc, 25% se
          Quant:  30% qc, 40% mcq_single, 5% mcq_multi, 5% numeric_entry,
                  20% data_interp (the DI cluster)
        Deficits are filled with the most flexible neighbor (rc_single /
        mcq_single) and then any matching question at the right difficulty.
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

        # Build the working exclusion set: caller-provided + recently-seen
        # (only if filtering doesn't empty the pool entirely).
        exclude = set(exclude_ids or [])
        recent = get_recently_seen_ids(days_back=avoid_recent_days,
                                        user_id=user_id)
        recent_candidates = recent - exclude
        # Count how many live items at this difficulty-band remain after
        # applying recent — if it would leave us unable to fill, skip the
        # recent filter.
        remaining = self._count_pool(measure, difficulty_band,
                                     exclude | recent_candidates)
        if remaining >= count:
            exclude |= recent_candidates
        else:
            logger.info(
                "Skipping recent-dedup for measure=%s count=%d (pool too "
                "thin: %d remain after filter)", measure, count, remaining,
            )

        selected_ids = []

        # ── Step 1: anchor mandatory clusters ────────────────────────
        if measure == "quant":
            di_cluster = self._select_di_cluster(difficulty_band, exclude)
            if di_cluster:
                selected_ids.extend(di_cluster)
                exclude.update(di_cluster)
            else:
                logger.warning(
                    "DI-cluster gap: no graph/table stimulus with ≥%d live "
                    "quant siblings at band=%s; section will lack a true "
                    "DI set.", DI_CLUSTER_MIN_SIZE, difficulty_band,
                )

        # ── Step 2: compute remaining per-subtype targets ─────────────
        remaining_count = count - len(selected_ids)
        targets = self._composition_targets(composition, count)
        if measure == "quant" and selected_ids:
            # DI slot already filled; zero it out so we don't double-count.
            targets["data_interp"] = max(
                0, targets.get("data_interp", 0) - len(selected_ids))

        # ── Step 3: fill per-subtype, with cluster atomicity for verbal RC ─
        for subtype, target_count in list(targets.items()):
            if target_count == 0:
                continue
            budget = count - len(selected_ids)
            if budget <= 0:
                break
            take = min(target_count, budget)

            if measure == "verbal" and subtype in CLUSTERED_VERBAL_SUBTYPES:
                picked = self._pick_cluster_atomic(
                    measure, subtype, difficulty_band, exclude, take)
            else:
                pool = self._pool_for_subtype(
                    measure, subtype, difficulty_band, exclude)
                random.shuffle(pool)
                picked = pool[:take]

            selected_ids.extend(picked)
            exclude.update(picked)

        # ── Step 4: fill deficit with the flexible subtype (cluster-aware
        # for verbal) ────────────────────────────────────────────────
        deficit = count - len(selected_ids)
        if deficit > 0:
            if measure == "verbal":
                extra = self._pick_cluster_atomic(
                    measure, fill_subtype, difficulty_band, exclude, deficit)
            else:
                extra_pool = self._pool_for_subtype(
                    measure, fill_subtype, difficulty_band, exclude)
                random.shuffle(extra_pool)
                extra = extra_pool[:deficit]
            selected_ids.extend(extra)
            exclude.update(extra)

        # ── Step 5: final fallback for any remaining deficit (ignores
        # cluster atomicity — last resort when the DB is too thin) ────
        deficit = count - len(selected_ids)
        if deficit > 0:
            fallback_query = Question.select(Question.id).where(
                Question.measure == measure,
                Question.status == "live",
                Question.id.not_in(list(exclude) if exclude else [0]),
            )
            if difficulty_band == "easy":
                fallback_query = fallback_query.where(
                    Question.difficulty_target <= 2)
            elif difficulty_band == "hard":
                fallback_query = fallback_query.where(
                    Question.difficulty_target >= 4)
            fallback = [q.id for q in fallback_query]
            random.shuffle(fallback)
            selected_ids.extend(fallback[:deficit])

        random.shuffle(selected_ids)
        return selected_ids[:count]

    # ── Helpers for cluster-aware assembly ────────────────────────────

    @staticmethod
    def _composition_targets(composition, count):
        """Round composition proportions to an integer quota per subtype so
        the quotas sum to `count`. Largest ratio gets the rounding residual."""
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

    def _count_pool(self, measure, difficulty_band, exclude):
        """How many live items are available after exclusions?"""
        q = Question.select(fn.COUNT(Question.id)).where(
            Question.measure == measure,
            Question.status == "live",
        )
        if difficulty_band == "easy":
            q = q.where(Question.difficulty_target <= 2)
        elif difficulty_band == "hard":
            q = q.where(Question.difficulty_target >= 4)
        if exclude:
            q = q.where(Question.id.not_in(list(exclude)))
        return q.scalar() or 0

    def _pick_cluster_atomic(self, measure, subtype, difficulty_band,
                              exclude_ids, take):
        """Return up to `take` question IDs of this subtype, respecting
        cluster atomicity — every question's full live sibling-set comes
        along for the ride, and clusters larger than `take` are skipped.

        Unclustered items (stimulus_id IS NULL) fill any leftover budget
        after all viable clusters are exhausted.
        """
        if take <= 0:
            return []

        query = Question.select(Question.id, Question.stimulus_id).where(
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

        clustered = defaultdict(list)
        solo = []
        for q in query:
            if q.stimulus_id is None:
                solo.append(q.id)
            else:
                clustered[q.stimulus_id].append(q.id)

        # Pool the stimuli; for each, fetch the complete live-sibling set so
        # we can detect partial clusters the exclude set has already
        # fragmented (and skip them rather than split).
        cluster_ids = list(clustered.keys())
        random.shuffle(cluster_ids)
        random.shuffle(solo)

        selected = []
        for stim_id in cluster_ids:
            if len(selected) >= take:
                break
            full = self._full_live_cluster(stim_id, measure, subtype,
                                           difficulty_band)
            if not full:
                continue
            # Skip if any sibling is already excluded (partial cluster)
            if any(qid in exclude_ids for qid in full):
                continue
            if len(selected) + len(full) > take:
                continue  # won't fit; don't split
            selected.extend(full)

        # Fill leftover budget with solo items (no cluster hazard).
        remaining = take - len(selected)
        if remaining > 0:
            selected.extend(solo[:remaining])

        return selected

    @staticmethod
    def _full_live_cluster(stimulus_id, measure, subtype, difficulty_band):
        """Return every live sibling question id for a stimulus. For RC,
        we intentionally cross subtype boundaries — a passage stimulus
        frequently holds a mix of rc_single + rc_multi children and every
        child must come along for the cluster to be atomic."""
        q = Question.select(Question.id).where(
            Question.stimulus_id == stimulus_id,
            Question.measure == measure,
            Question.status == "live",
        )
        if measure == "verbal" and subtype in CLUSTERED_VERBAL_SUBTYPES:
            q = q.where(Question.subtype.in_(list(CLUSTERED_VERBAL_SUBTYPES)))
        else:
            q = q.where(Question.subtype == subtype)
        if difficulty_band == "easy":
            q = q.where(Question.difficulty_target <= 2)
        elif difficulty_band == "hard":
            q = q.where(Question.difficulty_target >= 4)
        return [row.id for row in q]

    def _select_di_cluster(self, difficulty_band, exclude_ids):
        """Pick one Data-Interpretation set. Prefers a real 3-question
        cluster under a graph/table stimulus (children can be any quant
        subtype — real DI sets mix mcq_single / qc / numeric_entry). Falls
        back to a 2-question cluster, then (last resort) to 3 solo
        `data_interp` items.

        Returns a list of question IDs, empty if nothing is available.
        """
        # Candidate stimuli: anything with ≥DI_CLUSTER_MIN_SIZE live quant
        # siblings and a chart/table/graph type.
        cand = (
            Question.select(Question.stimulus_id,
                            fn.COUNT(Question.id).alias("n"))
            .join(Stimulus, on=(Stimulus.id == Question.stimulus_id))
            .where((Question.measure == "quant") &
                   (Question.status == "live") &
                   (Stimulus.stimulus_type.in_(DI_STIMULUS_TYPES)))
            .group_by(Question.stimulus_id)
            .having(fn.COUNT(Question.id) >= DI_CLUSTER_MIN_SIZE)
        )
        if exclude_ids:
            cand = cand.where(Question.id.not_in(list(exclude_ids)))

        # Sort: prefer clusters matching target size first, then larger ones
        # we can truncate to 3, then the minimum.
        triples, pairs = [], []
        for row in cand:
            n = row.n
            if n >= DI_CLUSTER_TARGET_SIZE:
                triples.append(row.stimulus_id)
            elif n >= DI_CLUSTER_MIN_SIZE:
                pairs.append(row.stimulus_id)

        random.shuffle(triples)
        random.shuffle(pairs)

        for stim_id in triples + pairs:
            siblings = list(
                Question.select(Question.id)
                .where((Question.stimulus_id == stim_id) &
                       (Question.measure == "quant") &
                       (Question.status == "live")))
            sibling_ids = [q.id for q in siblings]
            # Drop cluster entirely if any sibling is already excluded
            # (keeps atomicity).
            if any(qid in exclude_ids for qid in sibling_ids):
                continue
            # Apply difficulty filter: if every sibling violates it, skip;
            # otherwise keep the whole cluster (mixing bands within a real
            # DI set is normal on the real test, so we don't split).
            if difficulty_band == "easy":
                if not self._any_sibling_matches(sibling_ids, "<=", 2):
                    continue
            elif difficulty_band == "hard":
                if not self._any_sibling_matches(sibling_ids, ">=", 4):
                    continue

            random.shuffle(sibling_ids)
            return sibling_ids[:DI_CLUSTER_TARGET_SIZE]

        # Final fallback: 3 solo items tagged data_interp (legacy seed).
        solo = (Question.select(Question.id).where(
            Question.measure == "quant",
            Question.subtype == "data_interp",
            Question.status == "live",
        ))
        if exclude_ids:
            solo = solo.where(Question.id.not_in(list(exclude_ids)))
        solo_ids = [q.id for q in solo]
        random.shuffle(solo_ids)
        return solo_ids[:DI_CLUSTER_TARGET_SIZE]

    @staticmethod
    def _any_sibling_matches(sibling_ids, op, threshold):
        """Is there at least one sibling whose difficulty_target matches
        the band (e.g. `<=2` for easy, `>=4` for hard)?"""
        q = Question.select(fn.COUNT(Question.id)).where(
            Question.id.in_(sibling_ids))
        if op == "<=":
            q = q.where(Question.difficulty_target <= threshold)
        elif op == ">=":
            q = q.where(Question.difficulty_target >= threshold)
        return (q.scalar() or 0) > 0

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
