"""
Explanation service — provides detailed question explanations.
Uses stored explanations from the database, or falls back to LLM generation.
"""
from models.database import Question
from services.llm_service import llm_service


EXPLANATION_SYSTEM_PROMPT = """You are a GRE tutor providing clear, concise explanations
for GRE practice questions. Explain WHY the correct answer is correct and WHY each
incorrect answer is wrong. Use step-by-step reasoning.

For quantitative questions, show all mathematical steps clearly.
For verbal questions, explain vocabulary and context clues.

Keep explanations educational but concise (150-300 words).
Format with clear paragraphs. Use **bold** for key terms."""


class ExplanationService:
    """Retrieve or generate question explanations."""

    def get_explanation(self, question_data, user_response=None):
        """
        Get explanation for a question.
        Returns stored explanation if available, otherwise generates via LLM.
        """
        # Try stored explanation first
        if question_data.get("explanation"):
            return question_data["explanation"]

        # Generate via LLM
        return self._generate_explanation(question_data, user_response)

    def get_explanation_async(self, question_data, user_response, callback):
        """Async version for GUI. callback(explanation_text, error)."""
        stored = question_data.get("explanation")
        if stored:
            callback(stored, None)
            return

        prompt = self._build_prompt(question_data, user_response)
        llm_service.call_async(
            EXPLANATION_SYSTEM_PROMPT,
            prompt,
            lambda result, err: callback(result, err),
        )

    def _generate_explanation(self, question_data, user_response=None):
        """Generate an explanation using the LLM."""
        prompt = self._build_prompt(question_data, user_response)
        return llm_service.generate(EXPLANATION_SYSTEM_PROMPT, prompt)

    @staticmethod
    def _build_prompt(question_data, user_response=None):
        """Build the user prompt for explanation generation."""
        parts = [
            f"Question type: {question_data.get('subtype', 'unknown')}",
            f"Question: {question_data.get('prompt', '')}",
        ]

        if question_data.get("stimulus"):
            parts.append(f"Passage/Context: {question_data['stimulus'].get('content', '')[:500]}")

        if question_data.get("options"):
            opts = "\n".join(
                f"  {o['label']}) {o['text']} {'[CORRECT]' if o.get('is_correct') else ''}"
                for o in question_data["options"]
            )
            parts.append(f"Answer choices:\n{opts}")

        if question_data.get("numeric_answer"):
            na = question_data["numeric_answer"]
            if na.get("exact_value") is not None:
                parts.append(f"Correct answer: {na['exact_value']}")
            elif na.get("numerator") is not None:
                parts.append(f"Correct answer: {na['numerator']}/{na['denominator']}")

        if user_response:
            parts.append(f"Student's answer: {user_response}")

        parts.append("Explain why the correct answer is right and each incorrect answer is wrong.")
        return "\n\n".join(parts)

    def save_explanation(self, question_id, explanation_text):
        """Cache generated explanation back to the database."""
        q = Question.get_or_none(Question.id == question_id)
        if q:
            q.explanation = explanation_text
            q.save()
