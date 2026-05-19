"""AWA prompt validators.

Checks every AWAPrompt row for:

- ``AWA_NOT_ISSUE_FORMAT`` — prompt + instructions do NOT contain the
  hallmark Issue-task language: "discuss" + ("agree" or "disagree") +
  one of ("claim" / "statement" / "recommendation" / "view" / "position").
  Tolerant of phrasing variants ("Write a response in which you discuss
  the extent to which you agree or disagree with the recommendation").
  Severity warning, not error — alternate ETS phrasings ("which view more
  closely aligns with your own position") legitimately violate the
  default keyword set, so we keep these as inspect-not-block.
- ``AWA_TOO_LONG`` — prompt > 300 words. Likely a leaked rubric/spec, not
  a prompt. Severity error.
- ``AWA_EMPTY_PROMPT`` — prompt_text is empty / whitespace. Severity error.

Entry point:

    findings = validators.awa.validate(awa_prompt, deep=False)
"""
import re
from typing import Any, List

from validators.findings import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    ValidationFinding,
)


def _has_issue_format(text: str) -> bool:
    """True if the combined prompt+instructions text looks like an Issue
    task. We require the verb 'discuss' AND one of 'agree'/'disagree' to
    appear somewhere (case-insensitive). Both keywords appear in every
    canonical ETS Issue-task instruction phrasing.
    """
    if not text:
        return False
    lc = text.lower()
    has_discuss = "discuss" in lc
    has_agree_or_disagree = ("agree" in lc) or ("disagree" in lc)
    # Some phrasings use "which view ... aligns with your own position"
    # without "agree". Allow either of those alternate framings as well.
    has_position_aligning = bool(re.search(
        r"\bwhich view\b.*\b(aligns|aligning)\b", lc, re.DOTALL))
    return (has_discuss and has_agree_or_disagree) or has_position_aligning


def _word_count(text: str) -> int:
    if not text:
        return 0
    return len(text.split())


def validate(awa_prompt, *, deep: bool = False) -> List[ValidationFinding]:
    """Run all AWA validators against an :class:`AWAPrompt` row.

    Args:
        awa_prompt: Peewee ``AWAPrompt`` row OR any object exposing
            ``id``, ``prompt_text``, ``instructions``.
        deep: reserved.

    Returns:
        list of :class:`ValidationFinding`.
    """
    _ = deep  # reserved
    findings: List[ValidationFinding] = []

    pid = getattr(awa_prompt, "id", None)
    prompt_text = getattr(awa_prompt, "prompt_text", "") or ""
    instructions = getattr(awa_prompt, "instructions", "") or ""

    if not prompt_text.strip():
        findings.append(ValidationFinding(
            rule_id="AWA_EMPTY_PROMPT",
            severity=SEVERITY_ERROR,
            message="AWAPrompt.prompt_text is empty",
            details={"prompt_id": pid},
        ))
        # Don't bother running the rest of the checks against empty text.
        return findings

    combined = f"{prompt_text}\n{instructions}"

    if not _has_issue_format(combined):
        findings.append(ValidationFinding(
            rule_id="AWA_NOT_ISSUE_FORMAT",
            severity=SEVERITY_WARNING,
            message="AWAPrompt does not contain Issue-format keywords "
                    "(discuss + agree/disagree)",
            details={
                "prompt_id": pid,
                "prompt_excerpt": prompt_text[:120],
                "instructions_excerpt": instructions[:120],
            },
        ))

    wc = _word_count(prompt_text)
    if wc > 300:
        findings.append(ValidationFinding(
            rule_id="AWA_TOO_LONG",
            severity=SEVERITY_ERROR,
            message=f"AWAPrompt prompt_text is {wc} words "
                    f"(>300 — likely a leaked spec, not a prompt)",
            details={"prompt_id": pid, "word_count": wc},
        ))

    return findings
