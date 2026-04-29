"""
Reviser prompt — applies critic notes to a draft, returning v2.

The reviser is asked to:
- Make the SMALLEST edit that addresses each note
- Preserve correct_label and the option count
- Return the same JSON schema as the drafter

A diff lint in services/synthetic/reviser.py rejects revisions that
rewrite more than 30% of the stem; the prompt warns about this so the
model self-restricts.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from services.synthetic.critic import CriticReview


REVISER_SYSTEM = """You are revising a GRE draft item using targeted
notes from an editor. You wrote the original (or a sibling model did);
your job here is to apply minimum edits, NOT to rewrite.

Hard rules:
- Apply each note. If a note is impossible, file the conflict in
  `revision_notes` and skip it.
- Preserve `correct_label` exactly. Preserve the option count exactly.
  Preserve the subtype. The orchestrator will discard your output if
  any of these change.
- Preserve the stem unless a note targets it. Even when revising the
  stem, change the smallest span that resolves the note. The
  orchestrator rejects revisions whose stem edit-distance exceeds 30%
  of the original.
- If a note proposes a misconception label, set the option's
  `misconception` field to that label.
- Do NOT add notes of your own; ONLY apply the supplied list.

Output STRICT JSON ONLY (no prose, no code fences). Schema is identical
to the drafter's:

{
  "subtype": "<unchanged>",
  "stem": "<minimum-edited stem>",
  "options": [
    {"label": "A", "text": "...", "is_correct": false, "misconception": "..."},
    ...
  ],
  "correct_label": "<unchanged>",
  "explanation": "<updated to reflect changes>",
  "difficulty_target": <int>,
  "vocab_tier": "...",
  "domain_assumptions": [...],
  "expected_solve_steps": <int>,
  "concept_tags": [...],
  "stimulus": ...,
  "revision_notes": "<<= 60-word log of what you changed and any notes you skipped>>"
}
"""


def _render_notes_block(review: CriticReview) -> str:
    if not review.notes:
        return "No notes — return the input unchanged."
    lines = []
    for i, n in enumerate(review.notes, 1):
        lines.append(
            f"{i}. axis={n.axis}, severity={n.severity}, target={n.target}\n"
            f"   rationale: {n.rationale}\n"
            f"   edit: {n.edit}"
        )
    return "\n".join(lines)


def build_reviser_prompt(
    item_payload: Dict[str, Any],
    review: CriticReview,
) -> Dict[str, str]:
    """Return {"system": str, "user": str} for one revision call."""
    notes_block = _render_notes_block(review)
    item_block = json.dumps(item_payload, indent=2, ensure_ascii=False)
    user = (
        "ORIGINAL DRAFT:\n"
        f"```json\n{item_block}\n```\n\n"
        f"OVERALL ASSESSMENT FROM CRITIC: {review.overall_assessment}\n\n"
        "EDIT NOTES (apply each in order, smallest possible edit):\n"
        f"{notes_block}\n\n"
        "Return JSON in the schema described in the system prompt. "
        "Do not change correct_label, option count, or subtype."
    )
    return {"system": REVISER_SYSTEM, "user": user}
