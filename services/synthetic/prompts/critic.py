"""
Critic prompt — produces targeted edit notes for one draft item.

The critic is told to be specific (one note per concrete issue), to
*localise* (point at an option label / stem span / missing constraint),
and to suggest a *minimal edit*, never a wholesale rewrite. The
reviser will apply these edits in a separate pass.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from services.synthetic.judge import JudgeAggregate
from services.synthetic.types import RUBRIC_AXES, RUBRIC_AXIS_DESCRIPTIONS


CRITIC_SYSTEM = """You are a senior GRE item editor reviewing a draft
that another author wrote. Your job is NOT to score — it is to find
specific, fixable problems and propose minimum-edit revisions.

Operating principles:
- One note per concrete issue. Do not bundle multiple problems into
  one note. Three crisp notes beat one paragraph.
- LOCALISE every note. The `target` field must point at exactly one
  thing: an option label ("options[B]"), a stem span ("stem cue:
  'Although ... but'"), a missing constraint ("domain_assumptions"),
  or a numeric_entry field ("tolerance").
- MINIMUM EDIT. The `edit` field describes the smallest change that
  resolves the issue. NEVER suggest "rewrite the stem". If the stem
  has multiple defects, file multiple notes.
- Map each note to ONE rubric axis. If a note touches two axes, pick
  the one most directly responsible.
- Do not suggest stylistic changes that don't move a rubric score.
- Preserve `correct_label` and the structural shape of the item
  (number of options, subtype). The reviser is forbidden from
  changing those, so you must not propose edits that require it.

Severity is auto-assigned by the orchestrator based on the judge's
axis scores; you may set it but it will be overwritten if a judge
aggregate is available.

Output STRICT JSON ONLY (no prose, no code fences). Schema:
{
  "overall_assessment": "<<= 60-word summary of the draft's main weaknesses>>",
  "notes": [
    {
      "axis": "<one of: content_validity, construct_alignment, difficulty_plausibility, distractor_quality, language_clarity, fairness_bias>",
      "target": "<localised pointer, e.g. options[B] | stem | domain_assumptions | tolerance>",
      "rationale": "<<= 25-word reason>",
      "edit": "<<= 40-word concrete revision instruction>",
      "severity": "blocking | major | minor"
    }
  ]
}

If the draft has no defects worth revising, return:
  {"overall_assessment": "no defects", "notes": []}
"""


def _render_judge_block(agg: Optional[JudgeAggregate]) -> str:
    if not agg:
        return ""
    rows = "\n".join(
        f"  - {axis}: median {agg.medians.get(axis, 0):.1f}"
        for axis in RUBRIC_AXES
    )
    return (
        "Judge panel scores (medians across the panel) — "
        "focus your critique on the weakest axes:\n"
        f"{rows}\n"
        f"  mean: {agg.mean_overall:.2f}, "
        f"min_axis: {agg.min_axis_median:.1f}\n"
    )


def build_critic_prompt(
    item_payload: Dict[str, Any],
    judge_aggregate: Optional[JudgeAggregate] = None,
) -> Dict[str, str]:
    """Return {"system": str, "user": str} for one critic call."""
    rubric_block = "\n".join(
        f"- {axis}: {RUBRIC_AXIS_DESCRIPTIONS.get(axis, '')}"
        for axis in RUBRIC_AXES
    )
    judge_block = _render_judge_block(judge_aggregate)
    item_block = json.dumps(item_payload, indent=2, ensure_ascii=False)
    user = (
        "RUBRIC AXES:\n"
        f"{rubric_block}\n\n"
        f"{judge_block}"
        "ITEM TO CRITIQUE:\n"
        f"```json\n{item_block}\n```\n\n"
        "Return JSON in the schema described in the system prompt. "
        "Notes must be localised and propose minimum edits."
    )
    return {"system": CRITIC_SYSTEM, "user": user}
