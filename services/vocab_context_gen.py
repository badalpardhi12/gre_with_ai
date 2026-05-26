"""
Contextual vocabulary generator (P3.S2).

For a given GRE target word, ask the runtime LLM (via ``services.llm_service``)
to write a ~120-word GRE-register mini-passage that embeds the word naturally
plus one inference question whose correct answer hinges on the word's
in-context meaning. Results are cached in ``VocabContextItem`` keyed by
``(word, difficulty_tier, prompt_version)`` so a second request for the same
tuple is a DB hit, not an LLM call.

Network-free failure mode: if the LLM is unreachable (no API key, outbound
block, parse failure), ``generate_context_item`` returns ``None`` and callers
should fall back to the plain flashcard path. Tests mock ``llm_service`` and
do not hit the network.
"""
import json
from typing import List, Optional

from models.database import VocabContextItem
from services.log import get_logger

logger = get_logger("vocab_context_gen")


# Bump this when the system/user prompt changes in a way that would make old
# cached passages stylistically out of spec. The (word, tier, version) tuple
# is the cache key — a version bump invalidates old entries without a
# destructive migration.
PROMPT_VERSION = 2

_SYSTEM_PROMPT = (
    "You are a GRE vocabulary tutor. Produce a single ~120-word mini-passage "
    "in GRE-register prose (academic, precise) that uses the TARGET WORD "
    "naturally with strong context clues — but do NOT define the word "
    "explicitly. Then write ONE multiple-choice question that DIRECTLY "
    "tests the meaning of the target word in this passage, in the standard "
    "GRE 'Sentence Equivalence / vocabulary in context' style. The question "
    "stem MUST be one of these forms:\n"
    "  • \"In context, the word 'X' most nearly means:\"\n"
    "  • \"As used in the passage, 'X' most nearly means:\"\n"
    "  • \"Which of the following could best replace 'X' in the passage "
    "without changing its meaning?\"\n"
    "The four options must each be a single word or short phrase that is a "
    "candidate meaning for the target word. The correct option is a true "
    "synonym (or near-synonym) for the target word AS USED in this passage. "
    "The three distractors must be plausible — a careless reader might pick "
    "them — but each must be unambiguously wrong on a careful reading. "
    "Do NOT write a reading-comprehension inference question about the "
    "passage's content; the only thing being tested is the word's meaning. "
    "Output valid JSON only — no prose, no markdown fences."
)


def _user_prompt(word: str, difficulty: str) -> str:
    return (
        f"Target word: {word}\n"
        f"Difficulty: {difficulty}\n\n"
        "Output JSON with exactly these keys:\n"
        '  "passage": the ~120-word mini-passage (string, must contain the '
        f'target word "{word}")\n'
        '  "question": one of the meaning-of-target-word stems above '
        '(string, must literally name the target word in quotes)\n'
        '  "correct_answer": a single word or short phrase that is a '
        f'synonym for "{word}" as used in the passage (string)\n'
        '  "distractors": exactly three single-word or short-phrase '
        'wrong-answer candidates (each plausible but unambiguously wrong)\n'
    )


def _validate_payload(payload: dict) -> Optional[str]:
    """Return an error string if the payload is malformed, else None."""
    if not isinstance(payload, dict):
        return f"expected dict, got {type(payload).__name__}"
    for key in ("passage", "question", "correct_answer", "distractors"):
        if key not in payload:
            return f"missing key {key!r}"
    if not isinstance(payload["passage"], str) or not payload["passage"].strip():
        return "passage must be a non-empty string"
    if not isinstance(payload["question"], str) or not payload["question"].strip():
        return "question must be a non-empty string"
    if not isinstance(payload["correct_answer"], str) or not payload["correct_answer"].strip():
        return "correct_answer must be a non-empty string"
    distractors = payload["distractors"]
    if not isinstance(distractors, list) or len(distractors) != 3:
        return "distractors must be a list of exactly 3 strings"
    for d in distractors:
        if not isinstance(d, str) or not d.strip():
            return "each distractor must be a non-empty string"
    return None


def _cached(word: str, difficulty: str,
            prompt_version: int = PROMPT_VERSION) -> Optional[VocabContextItem]:
    """Return a cached VocabContextItem for (word, difficulty, version) or
    None. Exposed as a module-level helper so tests and the UI can probe the
    cache without triggering a fresh generation."""
    return VocabContextItem.get_or_none(
        (VocabContextItem.word == word) &
        (VocabContextItem.difficulty_tier == difficulty) &
        (VocabContextItem.prompt_version == prompt_version)
    )


def generate_context_item(
    word: str,
    difficulty: str = "mid",
    prompt_version: int = PROMPT_VERSION,
) -> Optional[VocabContextItem]:
    """Generate (or return cached) a context item for ``word``.

    Cache hit → existing row returned, no LLM call.
    Cache miss → call ``llm_service.generate_json``, validate payload, insert
    a new row. On any failure (no API key, network block, invalid JSON, schema
    violation) logs and returns None — callers fall back to non-context UX.

    Args:
        word: target vocab word, e.g. ``"perfunctory"``.
        difficulty: ``"easy"`` | ``"mid"`` | ``"hard"`` — informs LLM register.
        prompt_version: bump to invalidate cached entries after a prompt edit.

    Returns: the ``VocabContextItem`` row or ``None`` on failure.
    """
    existing = _cached(word, difficulty, prompt_version)
    if existing is not None:
        return existing

    # Lazy import keeps the module importable in environments without the
    # OpenAI SDK on the path (notably some CI test runners).
    from services.llm_service import llm_service

    try:
        payload = llm_service.generate_json(
            _SYSTEM_PROMPT,
            _user_prompt(word, difficulty),
        )
    except Exception as exc:
        logger.warning("LLM call failed for word=%r difficulty=%r: %s",
                       word, difficulty, exc)
        return None

    err = _validate_payload(payload)
    if err is not None:
        logger.warning("LLM payload validation failed for word=%r: %s",
                       word, err)
        return None

    try:
        model_name = llm_service.get_current_config().get("model", "")
    except Exception:
        model_name = ""

    row = VocabContextItem.create(
        word=word,
        difficulty_tier=difficulty,
        passage_text=payload["passage"].strip(),
        question_text=payload["question"].strip(),
        correct_answer=payload["correct_answer"].strip(),
        distractors=json.dumps([d.strip() for d in payload["distractors"]]),
        llm_model=model_name,
        prompt_version=prompt_version,
    )
    return row


def get_or_generate(
    word: str,
    difficulty: str = "mid",
    prompt_version: int = PROMPT_VERSION,
) -> Optional[VocabContextItem]:
    """Return a VocabContextItem for (word, difficulty) — cache or fresh.

    Thin wrapper over ``generate_context_item`` kept separate so callers can
    express intent ("I don't care whether it's new or cached; just give me
    one") without threading through the "could be None" concern that the
    primary generator advertises. Returns None on generation failure just
    like the base function."""
    return generate_context_item(word, difficulty, prompt_version)


def batch_generate(
    words: List[str],
    difficulty: str = "mid",
    max_n: int = 20,
    prompt_version: int = PROMPT_VERSION,
) -> List[VocabContextItem]:
    """Generate context items for up to ``max_n`` words, skipping any whose
    generation failed. Cached words short-circuit — no LLM call for them.

    Args:
        words: candidate target words.
        difficulty: applied uniformly to all words in this batch.
        max_n: cap on the number of items returned (protects against
            runaway LLM spend if a larger list is passed by mistake).

    Returns: list of VocabContextItem rows (possibly shorter than
    ``min(len(words), max_n)`` if some generations failed).
    """
    out: List[VocabContextItem] = []
    for word in words[:max(0, int(max_n))]:
        item = generate_context_item(word, difficulty, prompt_version)
        if item is not None:
            out.append(item)
    return out


def due_context_words(
    user_id: str = "local",
    limit: int = 20,
) -> List[str]:
    """Return words due for contextual review — pulled from the existing
    FlashcardReview SRS queue, filtered to words that already have a cached
    ``VocabContextItem``. Words without a cached passage are *not* generated
    here; the UI generates on demand when a word is actually drawn so a
    preview fetch doesn't burn LLM calls on words the user may never see."""
    from services.srs import due_cards
    from models.database import VocabWord

    out: List[str] = []
    for card in due_cards(user_id=user_id, limit=limit):
        try:
            word_row = VocabWord.get_by_id(card.word_id)
        except VocabWord.DoesNotExist:
            continue
        out.append(word_row.word)
    return out
