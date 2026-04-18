"""
Diagnostic test service: 30-question stratified mini-test that produces
per-topic accuracy + weakness ranking + predicted score band.

Output is saved to DiagnosticResult and feeds the AI study plan generator.
"""
import json
import random
from datetime import datetime
from typing import Optional

from models.database import (
    db, DiagnosticResult, Question, QuestionOption, NumericAnswer,
)
from models.taxonomy import (
    QUANT_TAXONOMY, VERBAL_TAXONOMY, get_taxonomy_summary,
)
from services.scoring import ScoringEngine


# Diagnostic blueprint: 6 quant topic groups × 2-3 questions + 6 verbal × 2-3
DIAG_QUANT_PLAN = [
    ("arithmetic", 4),
    ("algebra", 4),
    ("geometry", 3),
    ("data_analysis", 4),
]
DIAG_VERBAL_PLAN = [
    ("text_completion", 4),
    ("sentence_equivalence", 3),
    ("reading_comprehension", 5),
    ("critical_reasoning", 3),
]
# Total = 15 quant + 15 verbal = 30


def assemble_diagnostic() -> list:
    """Return list of question IDs for a diagnostic test.

    Stratified across topics, with mixed difficulty.
    """
    selected = []
    used = set()

    for topic, count in DIAG_QUANT_PLAN:
        # Pick ~equal across subtopics within this topic
        subtopics = list(QUANT_TAXONOMY[topic]["subtopics"].keys())
        random.shuffle(subtopics)
        per_sub = max(1, count // max(1, len(subtopics)))
        picked = 0
        for sub in subtopics:
            if picked >= count:
                break
            avail = list(Question.select(Question.id)
                         .where((Question.subtopic == sub) &
                                (Question.status == "live") &
                                (Question.id.not_in(list(used)))))
            random.shuffle(avail)
            for q in avail[:per_sub]:
                if picked >= count:
                    break
                selected.append(q.id)
                used.add(q.id)
                picked += 1

        # Fallback: any quant question in this topic
        if picked < count:
            avail = list(Question.select(Question.id)
                         .where((Question.topic == topic) &
                                (Question.status == "live") &
                                (Question.id.not_in(list(used)))))
            random.shuffle(avail)
            for q in avail[:count - picked]:
                selected.append(q.id)
                used.add(q.id)
                picked += 1

    for topic, count in DIAG_VERBAL_PLAN:
        subtopics = list(VERBAL_TAXONOMY[topic]["subtopics"].keys())
        random.shuffle(subtopics)
        per_sub = max(1, count // max(1, len(subtopics)))
        picked = 0
        for sub in subtopics:
            if picked >= count:
                break
            avail = list(Question.select(Question.id)
                         .where((Question.subtopic == sub) &
                                (Question.status == "live") &
                                (Question.id.not_in(list(used)))))
            random.shuffle(avail)
            for q in avail[:per_sub]:
                if picked >= count:
                    break
                selected.append(q.id)
                used.add(q.id)
                picked += 1

        if picked < count:
            avail = list(Question.select(Question.id)
                         .where((Question.topic == topic) &
                                (Question.status == "live") &
                                (Question.id.not_in(list(used)))))
            random.shuffle(avail)
            for q in avail[:count - picked]:
                selected.append(q.id)
                used.add(q.id)
                picked += 1

    random.shuffle(selected)
    return selected


def grade_diagnostic(question_ids: list, responses: dict,
                     user_id: str = "local") -> DiagnosticResult:
    """Grade the diagnostic and produce a DiagnosticResult.

    Args:
        question_ids: list of question IDs in the order presented
        responses: {question_id: response_dict}
    """
    # Collect per-topic accuracy
    from collections import defaultdict
    per_topic = defaultdict(lambda: {"attempted": 0, "correct": 0})
    per_subtopic = defaultdict(lambda: {"attempted": 0, "correct": 0})

    # Wrap the scoring + mastery-update pass so a partial diagnostic doesn't
    # leave half-updated MasteryRecord rows.
    with db.atomic():
        for qid in question_ids:
            q = Question.get_or_none(Question.id == qid)
            if not q:
                continue
            resp = responses.get(qid) or responses.get(str(qid))
            if not resp:
                continue

            # Build q_data for scoring
            from services.question_bank import QuestionBankService
            qb = QuestionBankService()
            q_data = qb.get_question(qid)
            if not q_data:
                continue

            is_correct = ScoringEngine.check_answer(q_data, resp)

            topic_key = q.topic or "unknown"
            per_topic[topic_key]["attempted"] += 1
            if is_correct:
                per_topic[topic_key]["correct"] += 1

            if q.subtopic:
                per_subtopic[q.subtopic]["attempted"] += 1
                if is_correct:
                    per_subtopic[q.subtopic]["correct"] += 1

            # Update mastery
            from services.mastery import update_mastery
            update_mastery(q.subtopic, is_correct, q.difficulty_target, user_id)

    # Compute scores per topic (accuracy)
    scores_per_topic = {
        topic: {
            "accuracy": (data["correct"] / data["attempted"]) if data["attempted"] else 0,
            "attempted": data["attempted"],
            "correct": data["correct"],
        }
        for topic, data in per_topic.items()
    }

    # Weakness ranking by subtopic accuracy (ascending)
    weakness = sorted(
        per_subtopic.items(),
        key=lambda kv: (kv[1]["correct"] / kv[1]["attempted"]) if kv[1]["attempted"] else 0,
    )
    weakness_ranking = [
        {"subtopic": sub,
         "accuracy": (data["correct"] / data["attempted"]) if data["attempted"] else 0,
         "attempted": data["attempted"]}
        for sub, data in weakness
    ]

    # Predict bands (rough mapping based on overall accuracy per measure)
    quant_correct = sum(per_topic[t]["correct"] for t in
                        ("arithmetic", "algebra", "geometry", "data_analysis"))
    quant_total = sum(per_topic[t]["attempted"] for t in
                      ("arithmetic", "algebra", "geometry", "data_analysis"))
    verbal_correct = sum(per_topic[t]["correct"] for t in
                         ("text_completion", "sentence_equivalence",
                          "reading_comprehension", "critical_reasoning"))
    verbal_total = sum(per_topic[t]["attempted"] for t in
                       ("text_completion", "sentence_equivalence",
                        "reading_comprehension", "critical_reasoning"))

    quant_band = predict_band(quant_correct, quant_total)
    verbal_band = predict_band(verbal_correct, verbal_total)

    diag = DiagnosticResult.create(
        user_id=user_id,
        scores_per_topic_json=json.dumps(scores_per_topic),
        weakness_ranking_json=json.dumps(weakness_ranking),
        predicted_verbal_band=verbal_band,
        predicted_quant_band=quant_band,
    )
    return diag


def predict_band(correct: int, total: int) -> str:
    """Map accuracy to a rough scaled-score band on 130-170."""
    if total == 0:
        return "unknown"
    pct = correct / total
    # Mapping table
    if pct >= 0.93:
        return "165-170"
    elif pct >= 0.85:
        return "160-165"
    elif pct >= 0.75:
        return "155-160"
    elif pct >= 0.60:
        return "150-155"
    elif pct >= 0.45:
        return "145-150"
    elif pct >= 0.30:
        return "140-145"
    else:
        return "130-140"


def get_latest_diagnostic(user_id: str = "local") -> Optional[DiagnosticResult]:
    return (DiagnosticResult
            .select()
            .where(DiagnosticResult.user_id == user_id)
            .order_by(DiagnosticResult.completed_at.desc())
            .first())
