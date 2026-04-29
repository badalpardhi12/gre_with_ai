"""
Generator (drafter) — stage (b).

Wraps a `LLMClient` configured for the `drafter` role. Produces one
DraftItem per call. Phase 0 ships only the function shape; concrete
calling is deferred to Phase 1 when actual generation begins.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.log import get_logger
from services.synthetic.llm_client import LLMClient
from services.synthetic.prompts.drafter import build_drafter_prompt
from services.synthetic.types import DraftItem, DraftOption, Seed

logger = get_logger("synthetic.generator")


def _payload_to_draft(seed: Seed, payload: Dict[str, Any], prompt_hash: str) -> DraftItem:
    options = [
        DraftOption(
            label=o.get("label", ""),
            text=o.get("text", ""),
            is_correct=bool(o.get("is_correct", False)),
            misconception=o.get("misconception", "") or "",
        )
        for o in payload.get("options", []) or []
    ]
    return DraftItem(
        subtype=payload.get("subtype", seed.subtype),
        stem=payload.get("stem", ""),
        options=options,
        correct_label=payload.get("correct_label", ""),
        explanation=payload.get("explanation", ""),
        difficulty_target=int(payload.get("difficulty_target",
                                          seed.difficulty_target)),
        vocab_tier=payload.get("vocab_tier", "n/a"),
        domain_assumptions=list(payload.get("domain_assumptions", []) or []),
        expected_solve_steps=int(payload.get("expected_solve_steps", 1)),
        concept_tags=list(payload.get("concept_tags", []) or []),
        stimulus=payload.get("stimulus"),
        figure_spec=payload.get("figure_spec"),
        correct_value=payload.get("correct_value"),
        numerator=payload.get("numerator"),
        denominator=payload.get("denominator"),
        tolerance=payload.get("tolerance"),
        seed=seed,
        prompt_hash=prompt_hash,
        generated_at=datetime.now(),
    )


class Generator:
    """One-call drafter that returns a DraftItem (or raises)."""

    def __init__(self, client: LLMClient):
        self.client = client

    def draft(
        self,
        seed: Seed,
        subtopic_display: str,
        few_shot_examples: Optional[List[Dict[str, Any]]] = None,
        extra_guidance: Optional[str] = None,
    ) -> DraftItem:
        prompt = build_drafter_prompt(
            subtype=seed.subtype,
            subtopic_display=subtopic_display,
            difficulty=seed.difficulty_target,
            few_shot_examples=few_shot_examples,
            extra_guidance=extra_guidance,
        )
        prompt_hash = hashlib.sha256(
            (prompt["system"] + "\n" + prompt["user"]).encode("utf-8")
        ).hexdigest()
        resp = self.client.complete_json(
            messages=[{"role": "user", "content": prompt["user"]}],
            system=prompt["system"],
            max_tokens=3000,
            temperature=1.0,
        )
        payload = resp.parsed_json or {}
        if not payload:
            try:
                payload = json.loads(resp.text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"generator returned non-JSON for seed {seed!r}: {exc}"
                )
        return _payload_to_draft(seed, payload, prompt_hash)
