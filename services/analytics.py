"""
Analytics service — per-question telemetry, pacing, and diagnostics.
"""
from collections import defaultdict
from datetime import datetime

from models.database import (
    db, TelemetryEvent, Response, SectionResult, Session,
    Question,
)


class AnalyticsService:
    """Compute diagnostics from session data."""

    @staticmethod
    def record_event(session_id, event_type, payload=None):
        """Log a telemetry event to the database."""
        TelemetryEvent.create(
            session=session_id,
            event_type=event_type,
            event_payload=str(payload or {}),
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
