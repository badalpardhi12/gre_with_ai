"""
Reviser — stage (e) of the refined pipeline.

Takes a `DraftItem` plus a `CriticReview` and returns a revised
`DraftItem` (v2). Same model family as the drafter (voice consistency)
but a different prompt that explicitly:

- Lists the critic notes
- Requires preserving `correct_label`, the option count, and the
  subtype shape
- Asks for the smallest edit that addresses each note

Loop budget: the orchestrator caps the number of revision rounds at 2
(plan §10 R2: Self-Refine literature shows diminishing returns past 2).

Idempotency: revising a draft against an empty critic review must
return the same draft (no spurious edits). Tested in
`tests/synthetic/test_critic_revise.py::test_reviser_idempotent`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.log import get_logger
from services.synthetic.critic import CriticReview, CriticNote
from services.synthetic.generator import _payload_to_draft
from services.synthetic.llm_client import LLMClient
from services.synthetic.prompts.reviser import build_reviser_prompt
from services.synthetic.types import DraftItem

logger = get_logger("synthetic.reviser")


# Reject revisions whose stem edit-distance exceeds this fraction of
# the original stem length. Prevents the well-known Self-Refine
# failure mode of "rewrite everything, then call it a revision".
DEFAULT_MAX_STEM_DRIFT = 0.30


def _levenshtein_distance(a: str, b: str) -> int:
    """Standard DP edit distance.

    Used for the stem-drift lint, not for any user-visible logic, so
    O(n*m) memory is fine for stem-length strings (typically <300 chars).
    """
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * lb
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                curr[j - 1] + 1,           # insert
                prev[j] + 1,               # delete
                prev[j - 1] + cost,        # substitute
            )
        prev = curr
    return prev[lb]


def stem_drift(original: str, revised: str) -> float:
    """Fraction of the original stem length that changed.

    Returns 0.0 for identical strings, ~1.0 for completely-different
    strings, > 1.0 if the revised stem is much longer than the
    original (insertions only). Caller compares against
    `DEFAULT_MAX_STEM_DRIFT` to decide whether to accept the revision.
    """
    if not original:
        return 0.0 if not revised else 1.0
    return _levenshtein_distance(original, revised) / max(1, len(original))


class Reviser:
    """Apply critic notes to a draft, returning v2.

    `client` should be the same model family as the drafter for voice
    consistency (plan §10 R2). The orchestrator selects this; the
    reviser does not enforce family equality (a same-model warning
    would be inverted from the critic's no-self-grade warning).
    """

    def __init__(
        self,
        client: LLMClient,
        max_stem_drift: float = DEFAULT_MAX_STEM_DRIFT,
    ):
        self.client = client
        self.max_stem_drift = max_stem_drift

    def revise(
        self,
        draft: DraftItem,
        review: CriticReview,
    ) -> DraftItem:
        """Apply the critic notes; return the v2 draft.

        Idempotent: an empty `review.notes` list returns the input
        draft unchanged (no LLM call).
        """
        if not review.notes:
            logger.debug("reviser: no notes for %s; skipping", review.item_id)
            return draft

        item_payload = _draft_to_payload(draft)
        prompt = build_reviser_prompt(item_payload, review)
        try:
            resp = self.client.complete_json(
                messages=[{"role": "user", "content": prompt["user"]}],
                system=prompt["system"],
                max_tokens=3000,
            )
            payload = resp.parsed_json or {}
            if not payload and resp.text:
                try:
                    payload = json.loads(resp.text)
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "reviser returned non-JSON for %s; keeping draft",
                        review.item_id,
                    )
                    return draft
        except Exception as exc:
            logger.exception("reviser failed for %s", review.item_id)
            return draft

        # Preserve the subtype, correct_label, and option count from
        # the original draft so the reviser can't change item shape.
        payload["subtype"] = draft.subtype
        payload["correct_label"] = draft.correct_label
        if draft.options and "options" in payload:
            if len(payload["options"]) != len(draft.options):
                logger.warning(
                    "reviser changed option count (%d->%d) for %s; "
                    "discarding revision",
                    len(draft.options), len(payload["options"]),
                    review.item_id,
                )
                return draft

        revised = _payload_to_draft(
            seed=draft.seed,
            payload=payload,
            prompt_hash=_hash_revision(draft.prompt_hash, review),
        )
        # Drift guard: if the revision rewrites too much of the stem,
        # reject and keep the original.
        drift = stem_drift(draft.stem, revised.stem)
        if drift > self.max_stem_drift:
            logger.warning(
                "reviser stem drift %.2f exceeds max %.2f for %s; "
                "discarding revision",
                drift, self.max_stem_drift, review.item_id,
            )
            return draft
        revised.generated_at = datetime.now()
        return revised


def _draft_to_payload(draft: DraftItem) -> Dict[str, Any]:
    """Mirror of `_payload_to_draft` for round-tripping."""
    return {
        "subtype": draft.subtype,
        "stem": draft.stem,
        "options": [
            {
                "label": o.label,
                "text": o.text,
                "is_correct": o.is_correct,
                "misconception": o.misconception,
            }
            for o in draft.options
        ],
        "correct_label": draft.correct_label,
        "explanation": draft.explanation,
        "difficulty_target": draft.difficulty_target,
        "vocab_tier": draft.vocab_tier,
        "domain_assumptions": list(draft.domain_assumptions),
        "expected_solve_steps": draft.expected_solve_steps,
        "concept_tags": list(draft.concept_tags),
        "stimulus": draft.stimulus,
        "figure_spec": draft.figure_spec,
        "correct_value": draft.correct_value,
        "numerator": draft.numerator,
        "denominator": draft.denominator,
        "tolerance": draft.tolerance,
    }


def _hash_revision(original_prompt_hash: str, review: CriticReview) -> str:
    """Fingerprint a revision for provenance.

    Combines the original prompt hash with the SHA-256 of the critic
    notes, so two revisions of the same draft against different
    critique sets get different prompt_hash values.
    """
    notes_blob = json.dumps(
        [
            {"axis": n.axis, "target": n.target, "edit": n.edit,
             "severity": n.severity}
            for n in review.notes
        ],
        sort_keys=True,
    )
    return hashlib.sha256(
        (original_prompt_hash + "::" + notes_blob).encode("utf-8")
    ).hexdigest()
