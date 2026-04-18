"""
AI study plan generator using Opus 4.7.

Given diagnostic results + target score + test date + hours/week, produces
a week-by-week schedule of lessons, drills, vocab review, and practice tests.

Plan stored in StudyPlan.plan_json. Replan triggers handled separately.

Now also incorporates:
- Per-subtopic mastery scores (focus weak areas)
- Current question-bank availability per subtopic
- Vocab progress (don't pile on if user hasn't reviewed in days)
"""
import json
from datetime import datetime, timedelta
from typing import Optional

from models.database import db, StudyPlan, DiagnosticResult, Question, MasteryRecord
from models.taxonomy import get_taxonomy_summary
from services.llm_client import get_client, MODEL_OPUS
from services.mastery import weakness_ranking, get_all_mastery
from services.srs import stats as vocab_stats


PLANNER_SYSTEM = """You are an expert GRE prep coach building a HIGHLY PERSONALIZED weekly study plan.

CRITICAL RULES:
1. Plan must be ADAPTIVE — it cannot be a generic "do verbal then quant" template
2. Use the student's diagnostic + mastery data to PRIORITIZE their weakest subtopics
3. Every weekday's tasks must reference SPECIFIC subtopic_ids (snake_case from taxonomy)
4. Spread practice tests evenly (1 every ~2 weeks); don't waste them early
5. Save lighter review for the final week before the test
6. Daily vocab is mandatory (~15 min): 20 new words + due reviews
7. Be REALISTIC — the student has limited hours/week; don't overload

OUTPUT FORMAT — JSON only, no markdown fences:
{
  "summary": "2-3 sentence personalized overview that names the student's biggest gaps",
  "priority_subtopics": [<top-5 weakest subtopics this plan focuses on>],
  "weekly_focus": ["Week 1: X — Y subtopics", "Week 2: ...", ...],
  "weeks": [
    {
      "week_number": 1,
      "start_date": "YYYY-MM-DD",
      "theme": "...",
      "tasks": [
        {"day": "Monday", "items": [
          "Lesson: <subtopic_id>",
          "Drill: 10 questions <subtopic_id>",
          "Vocab: 20 new words"
        ]},
        {"day": "Tuesday", "items": [...]},
        ... (one entry per study day)
      ]
    },
    ...
  ],
  "milestones": ["After Week 2: Practice Test 1", ...]
}

CONSTRAINTS:
- Use ONLY subtopic_ids from the provided taxonomy (otherwise the app can't link them)
- Distribute days realistically given hours_per_week
- Final week: only review + 1 light practice test, no new content"""


def _build_context(diagnostic, user_id: str = "local"):
    """Gather all the data needed for personalized planning."""
    parts = []

    # Diagnostic
    if diagnostic:
        scores = json.loads(diagnostic.scores_per_topic_json)
        weaknesses = json.loads(diagnostic.weakness_ranking_json)[:10]
        parts.append("DIAGNOSTIC RESULTS:")
        parts.append(f"  Predicted Verbal: {diagnostic.predicted_verbal_band}")
        parts.append(f"  Predicted Quant: {diagnostic.predicted_quant_band}")
        parts.append(f"  Per-topic accuracy: {json.dumps(scores)}")
        parts.append(f"  Top 10 weakest subtopics: {json.dumps(weaknesses)}")
    else:
        parts.append("DIAGNOSTIC: not yet completed")

    # Live mastery scores (from any drills the user has done)
    mastery = get_all_mastery(user_id)
    if mastery:
        sorted_m = sorted(mastery.items(), key=lambda kv: kv[1])
        weakest = sorted_m[:8]
        strongest = sorted_m[-3:]
        parts.append("\nMASTERY (from completed drills):")
        for sub, score in weakest:
            parts.append(f"  WEAK   {sub}: {int(score*100)}%")
        for sub, score in strongest:
            parts.append(f"  STRONG {sub}: {int(score*100)}%")
    else:
        parts.append("\nMASTERY: no drills completed yet")

    # Bank availability
    parts.append("\nQUESTION BANK AVAILABILITY:")
    bank_lines = []
    for measure, topic, sub, target in get_taxonomy_summary():
        cnt = Question.select().where(
            (Question.subtopic == sub) & (Question.status == "live")
        ).count()
        if cnt > 0:
            bank_lines.append(f"  {sub}: {cnt} questions")
    parts.extend(bank_lines[:30])
    if len(bank_lines) > 30:
        parts.append(f"  ... and {len(bank_lines) - 30} more subtopics")

    # Vocab progress
    try:
        v = vocab_stats(user_id)
        parts.append(f"\nVOCAB: {v['reviewed']} studied / {v['total_words']} total, "
                     f"{v['due_today']} due today, {v['mastered']} mastered")
    except Exception:
        parts.append("\nVOCAB: stats unavailable")

    return "\n".join(parts)


def generate_plan(
    target_score: int,
    test_date: datetime,
    hours_per_week: int,
    diagnostic: Optional[DiagnosticResult] = None,
    user_id: str = "local",
    model: str = MODEL_OPUS,
) -> StudyPlan:
    """Generate and persist a new personalized study plan."""
    weeks_until_test = max(1, (test_date - datetime.now()).days // 7)
    context = _build_context(diagnostic, user_id)

    user_prompt = f"""STUDENT PROFILE:
- Target combined score: {target_score} (Verbal+Quant, range 260-340)
- Test date: {test_date.strftime("%Y-%m-%d")} ({weeks_until_test} weeks away)
- Available hours per week: {hours_per_week}
- Today: {datetime.now().strftime("%Y-%m-%d")}

{context}

Build a {weeks_until_test}-week PERSONALIZED plan starting today.
Heavily focus the first half of the plan on the WEAKEST subtopics from diagnostic+mastery.
Output the JSON plan now."""

    client = get_client()
    plan_dict = client.call_anthropic_json(
        model=model,
        messages=[{"role": "user", "content": user_prompt}],
        system=PLANNER_SYSTEM,
        max_tokens=8192,
    )

    # Deactivate any existing active plans for this user
    StudyPlan.update(is_active=False).where(
        (StudyPlan.user_id == user_id) & (StudyPlan.is_active == True)
    ).execute()

    plan = StudyPlan.create(
        user_id=user_id,
        target_score=target_score,
        test_date=test_date,
        hours_per_week=hours_per_week,
        plan_json=json.dumps(plan_dict),
        is_active=True,
    )
    return plan


def get_active_plan(user_id: str = "local") -> Optional[StudyPlan]:
    return (StudyPlan.select()
            .where((StudyPlan.user_id == user_id) & (StudyPlan.is_active == True))
            .order_by(StudyPlan.created_at.desc())
            .first())


def get_today_tasks(user_id: str = "local") -> list:
    """Return today's task list from the active plan."""
    plan = get_active_plan(user_id)
    if not plan:
        return []
    plan_data = json.loads(plan.plan_json)
    today = datetime.now().date()
    weeks = plan_data.get("weeks", [])
    for week in weeks:
        try:
            start = datetime.strptime(week["start_date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        end = start + timedelta(days=6)
        if start <= today <= end:
            day_name = today.strftime("%A")
            for task in week.get("tasks", []):
                if task.get("day") == day_name:
                    return task.get("items", [])
    return []


def needs_replan(user_id: str = "local") -> bool:
    """Check if the active plan needs replanning.

    Triggers:
    - Plan older than 7 days since last replan
    - Test date passed without replan
    - Mastery scores show major shift since plan was created
    """
    plan = get_active_plan(user_id)
    if not plan:
        return False
    days_old = (datetime.now() - plan.last_replanned_at).days
    if days_old > 7:
        return True
    if datetime.now() > plan.test_date:
        return True
    return False
