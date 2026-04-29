"""
Solver prompt — adversarial cold-attempt at a draft.

Models see only the stem + options, never the answer or explanation.
The agreement gate compares each solver's chosen letter against the
drafter's claimed correct_label.
"""
from __future__ import annotations

import json
from typing import Any, Dict


SOLVER_SYSTEM = """You are sitting the GRE General Test. Solve the item
shown. Do NOT skip steps. After your reasoning, on a final line, output
exactly:

ANSWER: <letter>

For Sentence Equivalence (two correct answers), output:

ANSWER: <letter1>,<letter2>

Sentence Equivalence correctness test: the two correct words must
produce sentences with EQUIVALENT meaning when substituted into the
blank — contextual synonymy, not dictionary-level synonymy. A word
that fits grammatically but shifts the meaning (wrong valence,
narrower scope, different register) is a TRAP. Report the two letters
whose substituted sentences are most clearly meaning-equivalent in
alphabetical order, comma-separated.

For Numeric Entry, output:

ANSWER: <numeric value>

Do not output anything after that line.
"""


def build_solver_prompt(item_payload: Dict[str, Any]) -> Dict[str, str]:
    """Return {"system": str, "user": str} for a cold solve.

    Strips `correct_label`, `is_correct`, and `explanation` from the
    payload before showing it. Caller is responsible for parsing the
    `ANSWER:` line out of the response.
    """
    safe = {
        "subtype": item_payload.get("subtype", ""),
        "stem": item_payload.get("stem", ""),
        "options": [
            {"label": o.get("label"), "text": o.get("text")}
            for o in item_payload.get("options", [])
        ],
        "stimulus": item_payload.get("stimulus"),
    }
    user = (
        "Item:\n```json\n"
        + json.dumps(safe, indent=2, ensure_ascii=False)
        + "\n```\n\nSolve. Show reasoning, then `ANSWER: ...` on its own line."
    )
    return {"system": SOLVER_SYSTEM, "user": user}
