"""
Ambiguity-probe prompt — masked-option attack on a draft.

For each option in turn, hide it (replace text with `[REDACTED]`), ask a
fresh solver "of the visible options, which is most defensible? Or is
the hidden one possibly correct?" If a *distractor* is masked and the
solver still picks the genuine correct answer with high confidence,
that's expected. If the *correct* answer is masked and the solver picks
a distractor with high confidence, we have a co-derivable distractor →
reject the item.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List


AMBIGUITY_SYSTEM = """You are evaluating a GRE practice item where one
answer choice is hidden. Among the VISIBLE choices, pick the one most
defensible as the intended answer. If you believe the hidden choice
could plausibly be the intended answer, respond INSUFFICIENT instead.

Output format:
DECISION: <letter or INSUFFICIENT>
CONFIDENCE: <low | medium | high>
REASONING: <one sentence>
"""


def build_ambiguity_prompt(
    item_payload: Dict[str, Any],
    masked_label: str,
) -> Dict[str, str]:
    visible: List[Dict[str, Any]] = []
    for o in item_payload.get("options", []):
        if o.get("label") == masked_label:
            visible.append({"label": masked_label, "text": "[REDACTED]"})
        else:
            visible.append({"label": o.get("label"), "text": o.get("text")})
    safe = {
        "subtype": item_payload.get("subtype", ""),
        "stem": item_payload.get("stem", ""),
        "options": visible,
        "stimulus": item_payload.get("stimulus"),
    }
    user = (
        f"Hidden option: {masked_label}.\n\nItem:\n```json\n"
        + json.dumps(safe, indent=2, ensure_ascii=False)
        + "\n```\n\nFollow the output format exactly."
    )
    return {"system": AMBIGUITY_SYSTEM, "user": user}
