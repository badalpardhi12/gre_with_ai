"""
Per-question AI chat ("Why is C wrong?") and mistake-pattern coach.

Two services:
1. answer_chat: Conversational follow-up on a specific question after a user misses it
2. mistake_coach: Analyzes recent error log and produces a diagnosis + targeted drill

Both use Opus 4.7 via LLM gateway.
"""
import json
from datetime import datetime, timedelta
from typing import List, Optional

from models.database import (
    db, Question, QuestionOption, NumericAnswer, Response, Stimulus,
)
from services.llm_client import get_client, MODEL_OPUS


# ── Per-question chat ────────────────────────────────────────────────

ANSWER_CHAT_SYSTEM = """You are a patient, expert GRE tutor helping a student understand a specific question they just answered.

You have full knowledge of:
- The question text and any passage/stimulus
- All answer options with the correct one marked
- The official explanation
- The student's wrong answer (if applicable)

RULES:
1. NEVER override the deterministic correct answer. The official answer key is the truth.
2. Stay scoped to THIS question — don't venture into unrelated topics.
3. Be concise but pedagogically clear. Use student-friendly language.
4. When asked "why is X wrong?", explain the trap that X falls into.
5. When asked "why is the correct answer correct?", show the reasoning step-by-step.
6. Use plain text (no markdown headers); short paragraphs are okay.
7. If the user asks something outside the scope of this question, politely redirect."""


def build_question_context(q_data: dict) -> str:
    """Format a question + options + correct answer + explanation for system prompt."""
    parts = [f"QUESTION (subtype: {q_data['subtype']}):"]
    if q_data.get("stimulus"):
        parts.append(f"PASSAGE/STIMULUS:\n{q_data['stimulus']['content'][:2000]}")
    parts.append(f"PROMPT: {q_data['prompt']}")
    if q_data.get("options"):
        parts.append("OPTIONS:")
        for opt in q_data["options"]:
            marker = " ← CORRECT" if opt["is_correct"] else ""
            parts.append(f"  {opt['label']}: {opt['text']}{marker}")
    if q_data.get("numeric_answer"):
        na = q_data["numeric_answer"]
        if na.get("exact_value") is not None:
            parts.append(f"CORRECT ANSWER: {na['exact_value']}")
    if q_data.get("explanation"):
        parts.append(f"OFFICIAL EXPLANATION:\n{q_data['explanation']}")
    return "\n\n".join(parts)


class AnswerChat:
    """Stateful chat scoped to a single question."""

    def __init__(self, q_data: dict, user_response: Optional[dict] = None):
        self.q_data = q_data
        self.user_response = user_response
        self.history: List[dict] = []
        self._client = get_client()

    def _system_prompt(self) -> str:
        ctx = build_question_context(self.q_data)
        user_ans = ""
        if self.user_response:
            user_ans = f"\n\nSTUDENT'S ANSWER (which was wrong): {json.dumps(self.user_response)}"
        return f"{ANSWER_CHAT_SYSTEM}\n\n--- QUESTION CONTEXT ---\n{ctx}{user_ans}"

    def ask(self, user_message: str, model: str = MODEL_OPUS, max_tokens: int = 1024) -> str:
        """Ask a follow-up question. Returns the assistant's reply."""
        self.history.append({"role": "user", "content": user_message})
        reply = self._client.call_anthropic(
            model=model,
            messages=self.history,
            system=self._system_prompt(),
            max_tokens=max_tokens,
        )
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def reset(self):
        self.history = []


# ── Mistake-pattern coach ────────────────────────────────────────────

COACH_SYSTEM = """You are a GRE prep coach analyzing a student's recent mistake pattern.

You'll receive a list of questions the student got wrong, with:
- subtopic and difficulty
- their wrong answer + correct answer
- the official explanation

Identify 1-3 PATTERNS in the mistakes (don't just list them). For each pattern:
- Name the recurring error type (e.g., "inequality flips when multiplying by negatives", "missing 'EXCEPT' / 'NOT' in Verbal stems")
- Cite which questions exemplify it
- Recommend the most effective intervention (lesson, drill subtopic, or rule to memorize)

Keep tone warm and actionable. Output as plain text in this structure:

DIAGNOSIS:
1. [Pattern name]
   What's happening: ...
   Examples: Q123, Q456
   Action: ...

2. [...]

NEXT STEP DRILL: <subtopic_id> for 10 questions"""


def get_recent_mistakes(user_id: str = "local", since_days: int = 7,
                        limit: int = 50) -> List[dict]:
    """Pull recent mistakes from Response history."""
    cutoff = datetime.now() - timedelta(days=since_days)
    rows = (Response
            .select(Response, Question)
            .join(Question)
            .where((Response.is_correct == False) &
                   (Response.created_at >= cutoff))
            .order_by(Response.created_at.desc())
            .limit(limit))
    out = []
    for r in rows:
        q = r.question
        opts = list(QuestionOption.select().where(QuestionOption.question == q))
        correct_labels = [o.option_label for o in opts if o.is_correct]
        out.append({
            "qid": q.id,
            "subtopic": q.subtopic,
            "topic": q.topic,
            "subtype": q.subtype,
            "difficulty": q.difficulty_target,
            "prompt": q.prompt[:300],
            "user_answer": r.get_payload(),
            "correct_answer": correct_labels,
            "explanation_excerpt": q.explanation[:300] if q.explanation else "",
        })
    return out


def analyze_mistakes(user_id: str = "local",
                     since_days: int = 7,
                     model: str = MODEL_OPUS) -> str:
    """Run the mistake-pattern coach over recent errors. Returns markdown report."""
    mistakes = get_recent_mistakes(user_id, since_days)
    if len(mistakes) < 5:
        return ("Not enough recent mistakes to analyze a pattern. "
                "Complete more questions and try again.")

    # Format for the LLM
    context_lines = []
    for m in mistakes:
        context_lines.append(
            f"Q{m['qid']} ({m['subtopic']}, diff {m['difficulty']}, {m['subtype']}):\n"
            f"  Prompt: {m['prompt']}\n"
            f"  User: {m['user_answer']}\n"
            f"  Correct: {m['correct_answer']}\n"
            f"  Note: {m['explanation_excerpt']}"
        )

    user_prompt = (
        f"Recent {len(mistakes)} mistakes from the past {since_days} days:\n\n"
        + "\n\n".join(context_lines)
        + "\n\nProduce the diagnosis and next-step drill now."
    )

    client = get_client()
    return client.call_anthropic(
        model=model,
        messages=[{"role": "user", "content": user_prompt}],
        system=COACH_SYSTEM,
        max_tokens=2048,
    )
