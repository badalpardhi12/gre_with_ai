"""
Analytics service — per-question telemetry, pacing, and diagnostics.
"""
import json
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

from models.database import (
    db, TelemetryEvent, Response, SectionResult, Session,
    Question, QuestionOption, NumericAnswer, ScoringResult, Stimulus,
)


class AnalyticsService:
    """Compute diagnostics from session data."""

    @staticmethod
    def record_event(session_id, event_type, payload=None):
        """Log a telemetry event to the database."""
        TelemetryEvent.create(
            session=session_id,
            event_type=event_type,
            event_payload=json.dumps(payload or {}),
            created_at=datetime.now(),
        )

    @staticmethod
    def get_section_summary(session_id):
        """
        Per-section summary: time used, questions answered, accuracy.
        Returns list of section summary dicts.
        """
        sections = (SectionResult.select()
                    .where(SectionResult.session == session_id)
                    .order_by(SectionResult.id))

        summaries = []
        for sec in sections:
            responses = (Response.select()
                         .where(Response.section_result == sec.id))
            total = responses.count()
            answered = responses.where(Response.response_payload != "{}").count()
            correct = responses.where(Response.is_correct == True).count()

            summaries.append({
                "section_name": sec.section_name,
                "measure": sec.measure,
                "difficulty_band": sec.difficulty_band,
                "time_limit": sec.time_limit_seconds,
                "time_used": sec.time_used_seconds,
                "total_questions": total,
                "answered": answered,
                "correct": correct,
                "accuracy": correct / total if total > 0 else 0,
            })

        return summaries

    @staticmethod
    def get_question_details(session_id):
        """
        Per-question detail: time spent, correctness, marked status.
        Returns list of question detail dicts.
        """
        responses = (Response.select(Response, Question)
                     .join(Question)
                     .where(Response.session == session_id)
                     .order_by(Response.id))

        details = []
        for r in responses:
            details.append({
                "question_id": r.question_id,
                "measure": r.question.measure,
                "subtype": r.question.subtype,
                "difficulty": r.question.difficulty_target,
                "tags": r.question.get_tags(),
                "is_correct": r.is_correct,
                "is_marked": r.is_marked,
                "time_spent": r.time_spent_seconds,
            })

        return details

    @staticmethod
    def get_difficulty_breakdown(session_id, measure=None):
        """
        Group accuracy by difficulty level (1-5).
        Returns dict: {difficulty: {"total": N, "correct": N, "accuracy": float}}
        """
        query = (Response.select(Response, Question)
                 .join(Question)
                 .where(Response.session == session_id))
        if measure:
            query = query.where(Question.measure == measure)

        breakdown = defaultdict(lambda: {"total": 0, "correct": 0})
        for r in query:
            d = r.question.difficulty_target
            breakdown[d]["total"] += 1
            if r.is_correct:
                breakdown[d]["correct"] += 1

        for d in breakdown:
            t = breakdown[d]["total"]
            c = breakdown[d]["correct"]
            breakdown[d]["accuracy"] = c / t if t > 0 else 0

        return dict(breakdown)

    @staticmethod
    def get_topic_breakdown(session_id, measure=None):
        """
        Group accuracy by concept tag.
        Returns dict: {tag: {"total": N, "correct": N, "accuracy": float}}
        """
        query = (Response.select(Response, Question)
                 .join(Question)
                 .where(Response.session == session_id))
        if measure:
            query = query.where(Question.measure == measure)

        breakdown = defaultdict(lambda: {"total": 0, "correct": 0})
        for r in query:
            tags = r.question.get_tags()
            for tag in tags:
                breakdown[tag]["total"] += 1
                if r.is_correct:
                    breakdown[tag]["correct"] += 1

        for tag in breakdown:
            t = breakdown[tag]["total"]
            c = breakdown[tag]["correct"]
            breakdown[tag]["accuracy"] = c / t if t > 0 else 0

        return dict(breakdown)

    @staticmethod
    def get_pacing_data(session_id):
        """
        Time-per-question ordered by question position for pacing analysis.
        Returns list of {"position": i, "time": seconds, "measure": str}
        """
        responses = (Response.select(Response, Question)
                     .join(Question)
                     .where(Response.session == session_id)
                     .order_by(Response.id))

        return [
            {
                "position": i,
                "time": r.time_spent_seconds,
                "measure": r.question.measure,
                "is_correct": r.is_correct,
            }
            for i, r in enumerate(responses)
        ]


# ── Past-tests review helpers ─────────────────────────────────────────
#
# These functions back the "Past Tests" sidebar tab. The sidebar lists
# every completed session newest-first; clicking a row builds the same
# question_details list shape that AnswerReviewDialog already consumes
# for the post-test review on the live results screen.
#
# Kept as module-level functions (not methods on AnalyticsService) so
# the screen can import them directly without instantiating the class —
# AnalyticsService is a stateless namespace today, but the past-tests
# helpers are a self-contained read-only surface.


def get_past_session_summaries(limit: int = 50) -> List[Dict]:
    """Return one row per completed session, newest-first.

    Each row is a dict with the fields the Past Tests screen renders:
        session_id, started_at, finished_at, test_type, mode,
        n_questions, n_correct, accuracy, scores

    `accuracy` is computed from Response rows (not the section result),
    so it stays accurate even if the user abandoned mid-section. AWA-
    only sessions show ``n_questions == 0`` and ``accuracy is None``.
    """
    sessions = (
        Session.select()
        .where(Session.state == "completed")
        .order_by(Session.created_at.desc())
        .limit(limit)
    )

    out: List[Dict] = []
    for sess in sessions:
        responses = list(Response.select().where(Response.session == sess.id))
        n_questions = len(responses)
        n_correct = sum(1 for r in responses if r.is_correct is True)
        # Accuracy denominator is non-blank attempts: skip "{}" payloads
        # the user never touched.
        n_attempted = sum(
            1 for r in responses if r.response_payload and r.response_payload != "{}"
        )
        accuracy: Optional[float] = (
            n_correct / n_attempted if n_attempted > 0 else None
        )

        sc = ScoringResult.get_or_none(ScoringResult.session == sess.id)
        scores: Optional[Dict] = None
        if sc is not None:
            scores = {
                "verbal_low": sc.verbal_estimated_low,
                "verbal_high": sc.verbal_estimated_high,
                "quant_low": sc.quant_estimated_low,
                "quant_high": sc.quant_estimated_high,
                "awa": sc.awa_estimated,
            }

        # Prefer the declared started_at; fall back to created_at so old
        # rows that pre-date that column (or sessions that crashed
        # before the engine wrote it) still surface a date.
        started_at = sess.started_at or sess.created_at
        finished_at = sess.ended_at

        out.append({
            "session_id": sess.id,
            "started_at": started_at,
            "finished_at": finished_at,
            "test_type": sess.test_type,
            "mode": sess.mode,
            "n_questions": n_questions,
            "n_correct": n_correct,
            "accuracy": accuracy,
            "scores": scores,
        })
    return out


def build_session_question_details(session_id: int) -> List[Dict]:
    """Build the question_details list for a past session.

    Returns the same shape `main_frame._build_question_details` produces
    for an in-progress session so the existing AnswerReviewDialog can
    consume it without modification. Re-reads the live Question /
    QuestionOption / Stimulus / NumericAnswer rows so explanations and
    options reflect the current bank — retired questions still have
    rows so they render fine.
    """
    responses = (
        Response.select()
        .where(Response.session == session_id)
        .order_by(Response.id)
    )

    details: List[Dict] = []
    for r in responses:
        q = Question.get_or_none(Question.id == r.question_id)
        if q is None:
            # Question row was hard-deleted (very rare — retirement
            # uses status='retired', not DELETE). Surface a stub so
            # the dialog still shows a placeholder card.
            details.append({
                "question_id": r.question_id,
                "measure": "unknown",
                "subtype": "unknown",
                "difficulty": 0,
                "is_correct": r.is_correct,
                "is_marked": r.is_marked,
                "time_spent": r.time_spent_seconds,
                "prompt": "(question no longer in bank)",
                "options": [],
                "stimulus": None,
                "numeric_answer": None,
                "explanation": "",
                "user_response": r.get_payload(),
            })
            continue

        options = [
            {
                "label": o.option_label,
                "text": o.option_text,
                "is_correct": o.is_correct,
            }
            for o in (
                QuestionOption.select()
                .where(QuestionOption.question == q)
                .order_by(QuestionOption.option_label)
            )
        ]

        stimulus_payload = None
        if q.stimulus_id:
            stim = Stimulus.get_or_none(Stimulus.id == q.stimulus_id)
            if stim is not None:
                stimulus_payload = {
                    "type": stim.stimulus_type,
                    "title": stim.title,
                    "content": stim.content,
                }

        numeric_payload = None
        if q.subtype == "numeric_entry":
            na = NumericAnswer.get_or_none(NumericAnswer.question == q)
            if na is not None:
                numeric_payload = {
                    "exact_value": na.exact_value,
                    "numerator": na.numerator,
                    "denominator": na.denominator,
                    "tolerance": na.tolerance,
                    "mode": getattr(na, "mode", "auto"),
                }

        details.append({
            "question_id": q.id,
            "measure": q.measure,
            "subtype": q.subtype,
            "difficulty": q.difficulty_target,
            "is_correct": r.is_correct,
            "is_marked": r.is_marked,
            "time_spent": r.time_spent_seconds,
            "prompt": q.prompt or "",
            "options": options,
            "stimulus": stimulus_payload,
            "numeric_answer": numeric_payload,
            "explanation": q.explanation or "",
            "user_response": r.get_payload(),
        })
    return details
