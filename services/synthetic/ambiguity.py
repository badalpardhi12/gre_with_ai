"""
Ambiguity probe — stage (d).

For each option in turn, mask it and ask a probe model "of the visible
options, which is most defensible? Or could the hidden one be correct?"

Failure modes:
- Mask a *distractor*; probe still picks the correct letter → fine.
- Mask the *correct* answer; probe picks a distractor with high
  confidence → distractor is co-derivable → reject the item.
- Mask any option; probe says INSUFFICIENT → fine (probe acknowledges
  the masked option could matter).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List

from services.log import get_logger
from services.synthetic.llm_client import LLMClient
from services.synthetic.prompts.ambiguity import build_ambiguity_prompt
from services.synthetic.types import PipelineResult, PipelineStage

logger = get_logger("synthetic.ambiguity")


_DECISION_RE = re.compile(r"DECISION:\s*(\S+)", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r"CONFIDENCE:\s*(\w+)", re.IGNORECASE)


@dataclass
class AmbiguityProbe:
    masked_label: str
    decision: str               # letter or "INSUFFICIENT"
    confidence: str             # low | medium | high | unknown
    raw_response: str = ""


def _parse_probe(raw: str) -> AmbiguityProbe:
    decision_m = _DECISION_RE.search(raw or "")
    conf_m = _CONFIDENCE_RE.search(raw or "")
    return AmbiguityProbe(
        masked_label="",
        decision=(decision_m.group(1).strip().upper() if decision_m else ""),
        confidence=(conf_m.group(1).strip().lower() if conf_m else "unknown"),
        raw_response=raw,
    )


class AmbiguityChecker:
    """Run masked-option probes against a draft."""

    def __init__(self, probe_client: LLMClient):
        self.client = probe_client

    def probe(
        self,
        item_payload: Dict[str, Any],
        correct_label: str,
    ) -> List[AmbiguityProbe]:
        probes: List[AmbiguityProbe] = []
        for option in item_payload.get("options", []) or []:
            label = option.get("label", "")
            if not label:
                continue
            prompt = build_ambiguity_prompt(item_payload, label)
            try:
                resp = self.client.complete(
                    messages=[{"role": "user", "content": prompt["user"]}],
                    system=prompt["system"],
                    max_tokens=400,
                )
                raw = resp.text or ""
            except Exception as exc:
                logger.exception("ambiguity probe failed for label %s", label)
                raw = f"ERROR: {exc}"
            parsed = _parse_probe(raw)
            parsed.masked_label = label
            probes.append(parsed)
        return probes

    def gate(
        self,
        item_id: str,
        probes: List[AmbiguityProbe],
        correct_label: str,
    ) -> PipelineResult:
        # Collect violations: when the *correct* answer is masked AND
        # the probe still confidently picks a distractor.
        violations: List[Dict[str, Any]] = []
        for p in probes:
            if p.masked_label.upper() == correct_label.upper():
                if (
                    p.decision
                    and p.decision != "INSUFFICIENT"
                    and p.confidence == "high"
                ):
                    violations.append({
                        "masked": p.masked_label,
                        "chose": p.decision,
                        "confidence": p.confidence,
                    })
        passed = not violations
        return PipelineResult(
            item_id=item_id,
            stage=PipelineStage.AMBIGUITY,
            passed=passed,
            reason="" if passed else f"co-derivable distractor(s): {violations}",
            details={
                "probes": [
                    {
                        "masked": p.masked_label,
                        "decision": p.decision,
                        "confidence": p.confidence,
                    }
                    for p in probes
                ],
                "violations": violations,
            },
        )
