"""
Critic — stage (d) of the refined pipeline.

Self-Refine pattern (Madaan et al. 2023, NeurIPS): a *different* model
from the drafter reads the draft, sees the rubric scores from a
recent judge call (or scores the draft itself), and emits *targeted,
localised* edit suggestions. The reviser then applies those edits in
a separate pass.

Why a separate model from the drafter?  Self-preference bias (Wataoka
et al. 2024) — same-family critique is systematically lenient. Why the
*same* family for the reviser?  Voice consistency: the reviser is
applying minimum edits, not rewriting from scratch, so we want the
same stylistic priors that produced the draft.

Critic notes are intentionally structured: each note targets one rubric
axis, names the offending element (option label, stem span, missing
constraint), and proposes a *concrete* edit. The reviser is told to
make the smallest edit that addresses each note, never to rewrite the
whole stem. A diff-distance lint in `services.synthetic.reviser` rejects
revisions whose edit distance exceeds 30% of the original stem.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.log import get_logger
from services.synthetic.llm_client import LLMClient
from services.synthetic.judge import JudgeAggregate
from services.synthetic.prompts.critic import build_critic_prompt
from services.synthetic.types import RUBRIC_AXES

logger = get_logger("synthetic.critic")


@dataclass
class CriticNote:
    """One targeted edit suggestion.

    `axis` ties this note to a specific rubric axis so the reviser can
    prioritise (judge-flagged axes first). `target` localises the note
    to a stem span, an option label, or a numeric_entry field. `edit`
    is the concrete change the reviser should apply.

    `severity` is one of "blocking" (judge gave the axis ≤ 2),
    "major" (axis = 3), or "minor" (axis = 4 with room to grow). The
    reviser may skip "minor" notes if the loop budget is tight.
    """
    axis: str
    target: str
    rationale: str
    edit: str
    severity: str = "major"


@dataclass
class CriticReview:
    """The critic's assessment of one draft."""
    item_id: str
    notes: List[CriticNote] = field(default_factory=list)
    overall_assessment: str = ""
    raw_response: str = ""

    @property
    def has_blocking_notes(self) -> bool:
        return any(n.severity == "blocking" for n in self.notes)

    @property
    def axes_flagged(self) -> List[str]:
        return sorted({n.axis for n in self.notes})


def _severity_for_score(score: float) -> str:
    if score <= 2:
        return "blocking"
    if score <= 3:
        return "major"
    return "minor"


def _parse_critic_response(item_id: str, raw: str) -> CriticReview:
    """Parse a critic JSON response into structured notes.

    The expected schema:
        {
          "overall_assessment": "<= 60-word summary>",
          "notes": [
            {
              "axis": "distractor_quality",
              "target": "options[B]",
              "rationale": "B repeats the misconception of D",
              "edit": "Replace B's text with a 'wrong_register' distractor",
              "severity": "blocking" | "major" | "minor"
            }, ...
          ]
        }

    Defensive: if the response is broken JSON, we record an empty
    review and log; the orchestrator will skip the revise step.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(
            ln for ln in text.split("\n") if not ln.strip().startswith("```")
        )
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("critic returned non-JSON for item %s: %s", item_id, e)
        return CriticReview(item_id=item_id, raw_response=raw)

    notes_payload = payload.get("notes") or []
    notes: List[CriticNote] = []
    for entry in notes_payload:
        if not isinstance(entry, dict):
            continue
        axis = entry.get("axis") or ""
        # Validate axis against the known list; drop bogus axes.
        if axis and axis not in RUBRIC_AXES:
            logger.debug("critic note targets unknown axis %r; dropping", axis)
            continue
        notes.append(CriticNote(
            axis=axis,
            target=str(entry.get("target", "")),
            rationale=str(entry.get("rationale", "")),
            edit=str(entry.get("edit", "")),
            severity=str(entry.get("severity", "major")),
        ))
    return CriticReview(
        item_id=item_id,
        notes=notes,
        overall_assessment=str(payload.get("overall_assessment", "")),
        raw_response=raw,
    )


class Critic:
    """Run the critic against a draft and return targeted edit notes.

    The critic should be a different model family from the drafter
    (plan §10 R2). The pipeline orchestrator enforces this; calling
    `Critic.review` with a same-family client raises a warning but
    doesn't hard-fail (some test setups intentionally re-use one stub
    client for both roles).
    """

    def __init__(
        self,
        client: LLMClient,
        drafter_model_alias: Optional[str] = None,
    ):
        self.client = client
        self.drafter_model_alias = drafter_model_alias
        critic_alias = getattr(client, "model_alias", None)
        if (
            drafter_model_alias
            and critic_alias
            and drafter_model_alias == critic_alias
        ):
            logger.warning(
                "Critic uses the same model alias (%r) as the drafter; "
                "self-preference bias will inflate critique leniency. "
                "Pick a different model for the critic role.",
                critic_alias,
            )

    def review(
        self,
        item_id: str,
        item_payload: Dict[str, Any],
        judge_aggregate: Optional[JudgeAggregate] = None,
    ) -> CriticReview:
        """Score the draft and emit edit notes.

        If `judge_aggregate` is provided, the critic prompt includes
        the per-axis median scores so notes can be focused on the
        weakest axes. Otherwise the critic forms its own assessment.
        """
        prompt = build_critic_prompt(item_payload, judge_aggregate)
        try:
            resp = self.client.complete_json(
                messages=[{"role": "user", "content": prompt["user"]}],
                system=prompt["system"],
                max_tokens=2000,
            )
            raw = resp.text or json.dumps(resp.parsed_json or {})
        except Exception as exc:
            logger.exception("critic failed on item %s", item_id)
            return CriticReview(item_id=item_id, raw_response=f"ERROR: {exc}")
        review = _parse_critic_response(item_id, raw)
        # Promote severity if the judge already flagged the axis.
        if judge_aggregate:
            for note in review.notes:
                axis_median = judge_aggregate.medians.get(note.axis)
                if axis_median is not None:
                    note.severity = _severity_for_score(axis_median)
        logger.info(
            "critic %s: %d note(s) across axes=%s",
            item_id, len(review.notes), review.axes_flagged,
        )
        return review
