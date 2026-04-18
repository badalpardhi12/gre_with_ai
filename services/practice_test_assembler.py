"""
Full-length practice test assembler.

Builds 6 unique practice tests from the question bank, with:
- Replay protection: each test uses a disjoint subset
- Real GRE composition (verbal + quant per ETS proportions)
- Section-level adaptive routing (V2/Q2 difficulty depends on V1/Q1 performance)
"""
import random
from typing import List, Dict, Set

from models.database import db, Question
from models.taxonomy import VERBAL_TAXONOMY, QUANT_TAXONOMY


# Per-section composition (matches services/question_bank.py)
VERBAL_COMPOSITION = {
    "rc_single": 0.35, "rc_multi": 0.10, "rc_select_passage": 0.05,
    "tc": 0.25, "se": 0.25,
}
QUANT_COMPOSITION = {
    "qc": 0.30, "mcq_single": 0.40, "mcq_multi": 0.05,
    "numeric_entry": 0.05, "data_interp": 0.20,
}


def assemble_test(used_question_ids: Set[int], difficulty_band_q: str = "medium",
                  difficulty_band_v: str = "medium") -> Dict:
    """Assemble one full mock test, avoiding used_question_ids.

    Returns a dict:
    {
      "awa_prompt_id": int,
      "verbal_s1": [qids],
      "verbal_s2": [qids],
      "quant_s1": [qids],
      "quant_s2": [qids],
    }
    """
    test = {}
    used = set(used_question_ids)

    # Verbal S1: 12 medium-difficulty questions
    test["verbal_s1"] = _select_section("verbal", 12, "medium", used)
    used.update(test["verbal_s1"])

    # Verbal S2: 15 questions, difficulty determined by S1 performance (assumed medium for assembly)
    test["verbal_s2"] = _select_section("verbal", 15, difficulty_band_v, used)
    used.update(test["verbal_s2"])

    # Quant S1: 12 medium
    test["quant_s1"] = _select_section("quant", 12, "medium", used)
    used.update(test["quant_s1"])

    # Quant S2: 15
    test["quant_s2"] = _select_section("quant", 15, difficulty_band_q, used)
    used.update(test["quant_s2"])

    # AWA prompt
    from models.database import AWAPrompt
    prompts = list(AWAPrompt.select(AWAPrompt.id))
    test["awa_prompt_id"] = random.choice(prompts).id if prompts else None

    return test


def _select_section(measure: str, count: int, difficulty: str,
                    exclude: Set[int]) -> List[int]:
    """Pick `count` questions for one section using composition + difficulty filter."""
    composition = VERBAL_COMPOSITION if measure == "verbal" else QUANT_COMPOSITION

    # Compute per-subtype targets
    targets = {}
    running = 0
    sorted_subs = sorted(composition.items(), key=lambda x: -x[1])
    for i, (subtype, ratio) in enumerate(sorted_subs):
        if i == len(sorted_subs) - 1:
            targets[subtype] = max(0, count - running)
        else:
            t = round(count * ratio)
            targets[subtype] = t
            running += t

    selected = []
    deficit = 0
    for subtype, target_count in targets.items():
        pool = _pool(measure, subtype, difficulty, exclude)
        random.shuffle(pool)
        taken = pool[:target_count]
        selected.extend(taken)
        exclude.update(taken)
        deficit += target_count - len(taken)

    # Fill deficit from any subtype
    if deficit > 0:
        fallback = _pool(measure, None, difficulty, exclude)
        random.shuffle(fallback)
        selected.extend(fallback[:deficit])

    random.shuffle(selected)
    return selected[:count]


def _pool(measure: str, subtype, difficulty: str, exclude: Set[int]) -> List[int]:
    """Get question IDs matching filters, excluding given IDs."""
    q = Question.select(Question.id).where(
        (Question.measure == measure) & (Question.status == "live")
    )
    if subtype:
        q = q.where(Question.subtype == subtype)
    if difficulty == "easy":
        q = q.where(Question.difficulty_target <= 2)
    elif difficulty == "hard":
        q = q.where(Question.difficulty_target >= 4)
    if exclude:
        q = q.where(Question.id.not_in(list(exclude)))
    return [row.id for row in q]


def assemble_test_suite(num_tests: int = 6) -> List[Dict]:
    """Assemble multiple unique tests with no question overlap."""
    used_global = set()
    suite = []
    for i in range(num_tests):
        test = assemble_test(used_global)
        # Track used IDs to ensure no overlap across tests in the suite
        for sec in ("verbal_s1", "verbal_s2", "quant_s1", "quant_s2"):
            used_global.update(test[sec])
        test["test_number"] = i + 1
        suite.append(test)
    return suite


def determine_s2_difficulty(s1_correct: int, s1_total: int) -> str:
    """Mirror real GRE adaptive routing: S2 difficulty depends on S1 performance.

    >70% correct → hard S2
    40-70% → medium S2
    <40% → easy S2
    """
    if s1_total == 0:
        return "medium"
    pct = s1_correct / s1_total
    if pct >= 0.7:
        return "hard"
    elif pct >= 0.4:
        return "medium"
    else:
        return "easy"
