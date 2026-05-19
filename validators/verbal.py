"""Verbal validators.

Checks every live Verbal item for:

- ``VERBAL_TC_BLANK_COUNT_MISMATCH`` — the number of blanks in the TC stem
  doesn't match the implied number of option groups (1 group of 5, 2 groups
  of 3, or 3 groups of 3 — totals 5, 6, 9 options respectively).
  Stems use a zoo of blank styles (``___``, ``\\_\\_\\_``, LaTeX
  ``\\rule{...}{...}``, ``\\underline{\\phantom{...}}``, ``\\hspace``,
  ``\\hphantom``); the counter normalizes them.
- ``VERBAL_SE_OPTION_COUNT`` — SE not having exactly 6 options.
- ``VERBAL_SE_CORRECT_COUNT`` — SE not having exactly 2 correct.
- ``VERBAL_RC_NO_PASSAGE`` — rc_single / rc_multi / rc_select_passage with
  null Stimulus or empty content.
- ``VERBAL_SELECT_PASSAGE_MISSING_INSTRUCTION`` — rc_select_passage stem
  doesn't mention "click on the sentence" (or close variant). Today the
  bank has 0 such items so this validator finds nothing — but the rule
  is in place for Phase 6.3.

Entry point:

    findings = validators.verbal.validate(question, deep=False)
"""
import re
from typing import Any, List, Optional

from validators.findings import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    ValidationFinding,
)


# TC option-count → expected blank count. ETS only ships these three shapes.
_TC_VALID_SHAPES = {
    5: 1,
    6: 2,
    9: 3,
}


def _options(question) -> List[Any]:
    opts = getattr(question, "options", None)
    if opts is None:
        return []
    return list(opts)


def _stimulus(question):
    return getattr(question, "stimulus", None)


def count_blanks(prompt: str) -> int:
    """Count blank placeholders in a TC/SE stem.

    The bank uses a zoo of styles. We count, in order, the number of
    distinct blank-marking constructs and return the SUM. A 2-blank
    item with one ``___`` and one ``\\rule{...}`` returns 2.

    Patterns recognized:
      - 2+ consecutive ASCII underscores (``___`` / ``_______``).
      - 2+ consecutive escaped underscores (``\\_\\_``).
      - LaTeX ``\\rule{...}{...}`` (PDF-extracted bank items).
      - LaTeX ``\\underline{\\phantom{...}}``,
        ``\\underline{\\hphantom{...}}``,
        ``\\underline{\\hspace{...}}``.
      - LaTeX ``\\underline{\\qquad}`` / ``\\underline{\\quad}`` —
        a common shorthand in GenAI-extracted items.
    """
    if not prompt:
        return 0
    n = 0
    n += len(re.findall(r"_{2,}", prompt))
    n += len(re.findall(r"(?:\\_){2,}", prompt))
    n += len(re.findall(r"\\rule\{[^}]+\}\{[^}]+\}", prompt))
    n += len(re.findall(
        r"\\underline\{\\(?:phantom|hphantom|hspace)\{[^}]+\}\}", prompt))
    n += len(re.findall(
        r"\\underline\{\\q?quad\}", prompt))
    return n


def _expected_groups(opts: List[Any]) -> Optional[int]:
    """Infer expected blank count from options.

    Strategy:
      1. If labels start with ``blank1_`` / ``blank2_`` / ``blank3_``,
         count distinct ``blankN_`` prefixes.
      2. Otherwise, fall back to total option count → expected groups
         via :data:`_TC_VALID_SHAPES`. If the option count isn't a known
         shape, return ``None``.
    """
    prefixes = set()
    for o in opts:
        label = getattr(o, "option_label", "") or ""
        m = re.match(r"^blank(\d+)_", label)
        if m:
            prefixes.add(int(m.group(1)))
    if prefixes:
        return len(prefixes)
    return _TC_VALID_SHAPES.get(len(opts))


def _check_tc(question) -> List[ValidationFinding]:
    findings: List[ValidationFinding] = []
    qid = getattr(question, "id", None)
    opts = _options(question)
    expected = _expected_groups(opts)
    actual = count_blanks(getattr(question, "prompt", "") or "")
    if expected is None:
        # Option count is non-canonical (e.g. 7 options) — flag the shape
        # itself, then skip blank comparison.
        findings.append(ValidationFinding(
            rule_id="VERBAL_TC_OPTION_SHAPE",
            severity=SEVERITY_ERROR,
            message=f"tc has non-canonical option count {len(opts)} "
                    f"(expected 5, 6, or 9)",
            details={"qid": qid, "n_options": len(opts)},
        ))
        return findings

    if actual != expected:
        findings.append(ValidationFinding(
            rule_id="VERBAL_TC_BLANK_COUNT_MISMATCH",
            severity=SEVERITY_ERROR,
            message=(
                f"tc has {actual} blank(s) in stem but options imply "
                f"{expected} group(s)"
            ),
            details={
                "qid": qid,
                "blanks_in_stem": actual,
                "expected_groups": expected,
                "n_options": len(opts),
            },
        ))

    # Each blank should have exactly 1 correct option (TC is one-correct
    # per group, all groups required).
    n_correct = sum(1 for o in opts if getattr(o, "is_correct", False))
    if expected and n_correct != expected:
        findings.append(ValidationFinding(
            rule_id="VERBAL_TC_CORRECT_COUNT",
            severity=SEVERITY_ERROR,
            message=(
                f"tc must have exactly {expected} correct option(s) "
                f"(one per blank), got {n_correct}"
            ),
            details={
                "qid": qid,
                "n_correct": n_correct,
                "expected": expected,
            },
        ))
    return findings


def _check_se(question) -> List[ValidationFinding]:
    findings: List[ValidationFinding] = []
    qid = getattr(question, "id", None)
    opts = _options(question)
    if len(opts) != 6:
        findings.append(ValidationFinding(
            rule_id="VERBAL_SE_OPTION_COUNT",
            severity=SEVERITY_ERROR,
            message=f"se must have exactly 6 options, got {len(opts)}",
            details={"qid": qid, "n_options": len(opts)},
        ))
    n_correct = sum(1 for o in opts if getattr(o, "is_correct", False))
    if n_correct != 2:
        findings.append(ValidationFinding(
            rule_id="VERBAL_SE_CORRECT_COUNT",
            severity=SEVERITY_ERROR,
            message=f"se must have exactly 2 correct options, got {n_correct}",
            details={"qid": qid, "n_correct": n_correct},
        ))
    return findings


def _check_rc(question) -> List[ValidationFinding]:
    findings: List[ValidationFinding] = []
    qid = getattr(question, "id", None)
    subtype = getattr(question, "subtype", "")
    stim = _stimulus(question)
    if stim is None:
        findings.append(ValidationFinding(
            rule_id="VERBAL_RC_NO_PASSAGE",
            severity=SEVERITY_ERROR,
            message="rc item has no Stimulus row",
            details={"qid": qid, "subtype": subtype},
        ))
    else:
        content = getattr(stim, "content", None) or ""
        if not content.strip():
            findings.append(ValidationFinding(
                rule_id="VERBAL_RC_NO_PASSAGE",
                severity=SEVERITY_ERROR,
                message="rc item Stimulus.content is empty",
                details={
                    "qid": qid,
                    "subtype": subtype,
                    "stimulus_id": getattr(stim, "id", None),
                },
            ))
    return findings


# Loose match for the click-instruction. The blueprint phrasing is
# "Click on the sentence", but acceptable variants include "Select the
# sentence" and "Click the sentence". Match any of those (case-insensitive,
# tolerating intervening words like "in the passage").
_CLICK_INSTRUCTION_RE = re.compile(
    r"\b(click|select)\b[^.]{0,40}\bsentence\b",
    re.IGNORECASE | re.DOTALL,
)


def _check_select_passage(question) -> List[ValidationFinding]:
    findings: List[ValidationFinding] = []
    qid = getattr(question, "id", None)
    prompt = getattr(question, "prompt", "") or ""
    if not _CLICK_INSTRUCTION_RE.search(prompt):
        findings.append(ValidationFinding(
            rule_id="VERBAL_SELECT_PASSAGE_MISSING_INSTRUCTION",
            severity=SEVERITY_WARNING,
            message="rc_select_passage stem missing 'click/select on the "
                    "sentence' instruction",
            details={"qid": qid, "stem_excerpt": prompt[:120]},
        ))
    return findings


def validate(question, *, deep: bool = False) -> List[ValidationFinding]:
    """Run all Verbal validators against ``question``."""
    _ = deep  # reserved
    findings: List[ValidationFinding] = []

    subtype = getattr(question, "subtype", "")
    if subtype == "tc":
        findings.extend(_check_tc(question))
    elif subtype == "se":
        findings.extend(_check_se(question))

    if subtype in ("rc_single", "rc_multi", "rc_select_passage"):
        findings.extend(_check_rc(question))
    if subtype == "rc_select_passage":
        findings.extend(_check_select_passage(question))

    return findings
