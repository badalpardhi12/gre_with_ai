"""
Generator (drafter) prompt — produces a single GRE item per call.

Per-subtype variants are wired through `build_drafter_prompt` which
dispatches on `seed.subtype`. The schema is locked: the generator must
return exactly the JSON shape described in the system prompt, which
matches `services.synthetic.types.DraftItem`.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


DRAFTER_SYSTEM_BASE = """You are an ETS-trained GRE item writer. Output
EXACTLY one item as STRICT JSON (no prose, no code fences). The schema:

{
  "subtype": "<subtype>",
  "stem": "<question stem; LaTeX with \\\\(...\\\\) for math>",
  "options": [
    {
      "label": "A",
      "text": "...",
      "is_correct": false,
      "misconception": "<named distractor pattern, '' for the correct answer>"
    },
    ...
  ],
  "correct_label": "<letter>",
  "explanation": "<step-by-step solution and distractor analysis>",
  "difficulty_target": <int 1-5>,
  "vocab_tier": "core | advanced | recondite | n/a",
  "domain_assumptions": ["x is a positive integer", ...],
  "expected_solve_steps": <int>,
  "concept_tags": ["..."],
  "stimulus": null
}

For RC items (rc_single / rc_multi / rc_select_passage) the "stimulus"
field MUST be an object with this exact shape (do not rename keys):

  "stimulus": {
    "type": "passage",
    "title": "<short topic label, optional>",
    "content": "<the FULL passage text, 1-3 paragraphs>"
  }

For DI items the "stimulus" field MUST be an object with this shape and
a chart_spec render_spec for matplotlib:

  "stimulus": {
    "type": "graph",
    "title": "<chart title>",
    "content": "<one-sentence verbal description of the chart>",
    "render_spec": { "spec": { ... matplotlib chart_spec ... } }
  }

For geometry items provide a top-level "figure_spec" object (NOT a
stimulus); the figure renderer reads it directly. The persisted
stimulus row is auto-built from the rendered SVG.

Numeric Entry items omit "options" and "correct_label" and instead
provide "correct_value" (decimal) OR ("numerator", "denominator") with
"tolerance".

Quality bar: indistinguishable from official ETS material. Refuse to
hedge with phrases like "I think" or "as a language model".
"""


# Per-subtype guidance fragments. Kept short here; the per-subtype
# recipes in §3 of the plan can extend these later.
SUBTYPE_GUIDANCE: Dict[str, str] = {
    "tc": (
        "Text Completion: 1-3 blanks (one word each). Each blank gets "
        "5 options for 1-blank items, 3 for 2/3-blank. Distractors must "
        "embody NAMED misconceptions (wrong-valence synonym, "
        "context-irrelevant homonym, near-synonym missing the contrast)."
    ),
    "se": (
        "Sentence Equivalence: one sentence with one blank, 6 options, "
        "EXACTLY 2 correct.\n\n"
        "BEFORE choosing your correct pair, execute this self-check — "
        "it is the single most common failure mode for SE drafters and "
        "it must not ship:\n"
        "  1. Pick the pair of words you believe are correct "
        "(call them W1 and W2).\n"
        "  2. Write out the TWO resulting sentences in full by "
        "substituting W1 then W2 into the blank.\n"
        "  3. Read both sentences and confirm they convey the SAME "
        "meaning in this specific context — not merely that both fit "
        "grammatically. Words that fit grammatically but shift the "
        "meaning (wrong valence, narrower scope, stronger or weaker "
        "force) are TRAPS, not correct answers.\n"
        "  4. Scan the other four distractors and confirm that NO "
        "other pair also produces meaning-equivalent sentences. If a "
        "second valid pair exists, swap one option out — the item is "
        "broken as written.\n"
        "  5. Only now label the two words from step 1 "
        "`is_correct: true` and set `correct_label` to the two "
        "letters in sorted alphabetical order, comma-separated "
        "(e.g. `A,C`, not `C,A`).\n\n"
        "Correctness test: contextual synonymy, NOT dictionary-level "
        "synonymy. Two words can be dictionary synonyms but only one "
        "fits the sentence's tone, register, or argument direction — "
        "that pair is wrong. Two words can be dictionary near-opposites "
        "but both fit the sentence's contextual need — that pair is "
        "right.\n\n"
        "Distractor design: each of the four wrong options should be "
        "tied to a NAMED trap (record it in `misconception`):\n"
        "  - `near_synonym_wrong_valence`: fits shape of meaning but "
        "flips positive/negative charge.\n"
        "  - `narrower_scope`: a synonym of one correct word only "
        "(so swapping it in creates a subtly different sentence).\n"
        "  - `register_mismatch`: correct meaning but wrong "
        "formality / tone for the sentence.\n"
        "  - `shared_prefix_decoy`: morphologically similar to a "
        "correct answer but unrelated in meaning.\n"
        "Do NOT include any wrong option that is a near-synonym of "
        "the OTHER wrong options — that creates a second valid pair "
        "and breaks the item."
    ),
    "qc": (
        "Quantitative Comparison: 4 fixed options "
        "(A: greater, B: greater, C: equal, D: cannot be determined). "
        "EVERY variable must be explicitly constrained in "
        "domain_assumptions. Test STRUCTURAL reasoning, not pure "
        "computation."
    ),
    "mcq_single": (
        "Problem Solving (single answer): 5 options, exactly one "
        "correct. Each distractor embodies a named misconception."
    ),
    "mcq_multi": (
        "Problem Solving (multi-answer): 3-7 options, 1-3 correct. "
        "Each correct option must be independently derivable."
    ),
    "numeric_entry": (
        "Numeric Entry: single numeric answer (decimal or fraction). "
        "Specify tolerance for decimals; tolerance=0 for fractions."
    ),
    "rc_single": (
        "Reading Comprehension single-answer: provide a passage in "
        "the stimulus field plus a 5-option question."
    ),
    "rc_multi": (
        "Reading Comprehension multi-answer: passage stimulus + "
        "question with 3 options, 1-3 correct."
    ),
    "rc_select_passage": (
        "Reading Comprehension select-sentence: passage stimulus, "
        "answer is a 0-indexed sentence number."
    ),
    "data_interp": (
        "Data Interpretation: include a chart_spec object describing "
        "the chart, then 1-3 questions that draw on it.\n\n"
        "TEXT SELF-CONTAINMENT (mandatory): the chart you describe will "
        "be RENDERED as a separate image, but every numeric value the "
        "question requires MUST also appear textually in the stem. "
        "Do not require the reader to extract numbers from the chart. "
        "Restate the relevant data points (e.g. 'In 2018 sales were "
        "$240M and in 2019 they were $312M.') inside the stem itself. "
        "Tables and totals referenced by the question must be reproduced "
        "in the stem as plain text. A reviewer reading ONLY the stem "
        "(no image) must be able to compute the answer."
    ),
}


def build_drafter_prompt(
    subtype: str,
    subtopic_display: str,
    difficulty: int,
    *,
    few_shot_examples: Optional[List[Dict[str, Any]]] = None,
    extra_guidance: Optional[str] = None,
) -> Dict[str, str]:
    """Compose system + user messages for one draft.

    Few-shot examples are inserted into the user message; the system
    message stays subtopic-agnostic for cache friendliness.
    """
    guidance = SUBTYPE_GUIDANCE.get(subtype, "")
    if extra_guidance:
        guidance = guidance + "\n\n" + extra_guidance
    examples_block = ""
    if few_shot_examples:
        examples_block = (
            "\n\nReference examples (do NOT copy verbatim):\n"
            + json.dumps(few_shot_examples, indent=2, ensure_ascii=False)
        )
    user = (
        f"Write ONE GRE item.\n"
        f"- subtype: {subtype}\n"
        f"- subtopic: {subtopic_display}\n"
        f"- difficulty_target: {difficulty}/5\n\n"
        f"Subtype guidance:\n{guidance}"
        f"{examples_block}\n\n"
        "Return only the JSON object described in the system prompt."
    )
    return {"system": DRAFTER_SYSTEM_BASE, "user": user}
