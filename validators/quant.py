"""Quant validators.

Checks every live Quant item for:

- ``QUANT_NUMERIC_NO_ANSWER`` — numeric_entry without a NumericAnswer row OR
  with NumericAnswer row whose ``exact_value`` and fraction fields are both
  null. This breaks scoring entirely (the engine has nothing to compare
  against).
- ``QUANT_NUMERIC_NEGATIVE_TOLERANCE`` — tolerance < 0. Either a typo or a
  schema violation; tolerance must be ≥ 0.
- ``QUANT_QC_NON_CANONICAL_OPTIONS`` — QC item without the four canonical
  ETS option strings. The session engine doesn't enforce this so a typo
  ("equally") would silently render with weird options.
- ``QUANT_QC_OPTION_COUNT`` — QC item with a count other than 4 options.
- ``QUANT_MCQ_MULTI_TOO_FEW_CORRECT`` — mcq_multi with only 0 or 1 correct
  options. Almost certainly a mislabel of an mcq_single.
- ``QUANT_NO_ANSWER_KEY`` — item has no QuestionOption.is_correct=True row
  AND no NumericAnswer with a non-null answer value. Item cannot be graded.

Entry point:

    findings = validators.quant.validate(question, deep=False)

The ``deep`` flag is reserved for future LLM-driven correctness verification
(Phase 3.2); today it's a no-op.
"""
import re
from typing import Any, Iterable, List, Optional

from validators.findings import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    ValidationFinding,
)


# The four canonical ETS QC option strings, in the order ETS prints them.
# We compare against a normalized set (lowercase + whitespace-collapsed) so
# minor whitespace / punctuation drift doesn't flag every item.
QC_CANONICAL_OPTIONS = (
    "Quantity A is greater",
    "Quantity B is greater",
    "The two quantities are equal",
    "The relationship cannot be determined from the information given",
)


def _normalize_option(text: str) -> str:
    """Lowercase, strip HTML-ish tags, collapse whitespace."""
    if text is None:
        return ""
    # Strip simple HTML tags so a wrapping <p>...</p> doesn't break the match.
    text = re.sub(r"<[^>]+>", " ", text)
    # Strip period at end (some sources end with a full stop).
    text = text.strip().rstrip(".").strip()
    return re.sub(r"\s+", " ", text.lower())


_QC_CANONICAL_NORMALIZED = frozenset(
    _normalize_option(opt) for opt in QC_CANONICAL_OPTIONS
)


def _options(question) -> List[Any]:
    """Materialize options as a list. Works for ORM rows (backref) and for
    plain dataclass-style fixtures used by tests."""
    opts = getattr(question, "options", None)
    if opts is None:
        return []
    return list(opts)


def _numeric_answers(question) -> List[Any]:
    nas = getattr(question, "numeric_answers", None)
    if nas is None:
        return []
    return list(nas)


def _has_answer_value(na) -> bool:
    """True if a NumericAnswer row carries a usable answer."""
    if getattr(na, "exact_value", None) is not None:
        return True
    num = getattr(na, "numerator", None)
    den = getattr(na, "denominator", None)
    if num is not None and den is not None:
        return True
    return False


def _check_numeric_entry(question) -> List[ValidationFinding]:
    findings: List[ValidationFinding] = []
    nas = _numeric_answers(question)
    qid = getattr(question, "id", None)

    if not nas:
        findings.append(ValidationFinding(
            rule_id="QUANT_NUMERIC_NO_ANSWER",
            severity=SEVERITY_ERROR,
            message="numeric_entry has no NumericAnswer row",
            details={"qid": qid, "subtype": "numeric_entry"},
        ))
        return findings

    has_any_value = False
    for na in nas:
        if _has_answer_value(na):
            has_any_value = True
        tol = getattr(na, "tolerance", None)
        if tol is not None and tol < 0:
            findings.append(ValidationFinding(
                rule_id="QUANT_NUMERIC_NEGATIVE_TOLERANCE",
                severity=SEVERITY_ERROR,
                message=f"numeric_entry has negative tolerance: {tol}",
                details={"qid": qid, "tolerance": tol},
            ))

    if not has_any_value:
        findings.append(ValidationFinding(
            rule_id="QUANT_NUMERIC_NO_ANSWER",
            severity=SEVERITY_ERROR,
            message="numeric_entry NumericAnswer rows have no exact_value or "
                    "numerator/denominator",
            details={"qid": qid, "n_rows": len(nas)},
        ))
    return findings


def _check_qc(question) -> List[ValidationFinding]:
    findings: List[ValidationFinding] = []
    opts = _options(question)
    qid = getattr(question, "id", None)

    if len(opts) != 4:
        findings.append(ValidationFinding(
            rule_id="QUANT_QC_OPTION_COUNT",
            severity=SEVERITY_ERROR,
            message=f"qc must have 4 options, got {len(opts)}",
            details={"qid": qid, "n_options": len(opts)},
        ))
        # Still try to check canonicality below — but only if we have at
        # least one option to check.

    seen_normalized = {_normalize_option(o.option_text) for o in opts}
    missing = _QC_CANONICAL_NORMALIZED - seen_normalized
    extra = seen_normalized - _QC_CANONICAL_NORMALIZED
    if missing or extra:
        findings.append(ValidationFinding(
            rule_id="QUANT_QC_NON_CANONICAL_OPTIONS",
            severity=SEVERITY_ERROR,
            message="qc options do not match the canonical ETS set",
            details={
                "qid": qid,
                "missing": sorted(missing),
                "extra": sorted(extra),
            },
        ))

    # QC must have exactly one correct option.
    n_correct = sum(1 for o in opts if getattr(o, "is_correct", False))
    if n_correct != 1:
        findings.append(ValidationFinding(
            rule_id="QUANT_QC_CORRECT_COUNT",
            severity=SEVERITY_ERROR,
            message=f"qc must have exactly 1 correct option, got {n_correct}",
            details={"qid": qid, "n_correct": n_correct},
        ))
    return findings


def _check_mcq_multi(question) -> List[ValidationFinding]:
    findings: List[ValidationFinding] = []
    opts = _options(question)
    qid = getattr(question, "id", None)
    n_correct = sum(1 for o in opts if getattr(o, "is_correct", False))
    if n_correct < 2:
        findings.append(ValidationFinding(
            rule_id="QUANT_MCQ_MULTI_TOO_FEW_CORRECT",
            severity=SEVERITY_ERROR,
            message=f"mcq_multi must have ≥2 correct options, got {n_correct}",
            details={
                "qid": qid,
                "n_correct": n_correct,
                "n_options": len(opts),
            },
        ))
    return findings


def _check_answer_key_present(question) -> List[ValidationFinding]:
    """Generic last-resort check: every Quant item must have SOMETHING
    that scoring can compare against."""
    qid = getattr(question, "id", None)
    subtype = getattr(question, "subtype", "")
    opts = _options(question)
    nas = _numeric_answers(question)

    has_correct_option = any(getattr(o, "is_correct", False) for o in opts)
    has_numeric_answer = any(_has_answer_value(na) for na in nas)
    if has_correct_option or has_numeric_answer:
        return []

    return [ValidationFinding(
        rule_id="QUANT_NO_ANSWER_KEY",
        severity=SEVERITY_ERROR,
        message="quant item has no answer key (no correct option, no numeric answer)",
        details={
            "qid": qid,
            "subtype": subtype,
            "n_options": len(opts),
            "n_numeric_rows": len(nas),
        },
    )]


def validate(question, *, deep: bool = False) -> List[ValidationFinding]:
    """Run all Quant validators against ``question``.

    Args:
        question: a Peewee ``Question`` row OR any object exposing
            ``id``, ``subtype``, an iterable ``options`` (each with
            ``option_text``, ``is_correct``), and an iterable
            ``numeric_answers`` (each with ``exact_value``, ``numerator``,
            ``denominator``, ``tolerance``).
        deep: reserved for Phase 3.2 LLM-driven correctness; currently a
            no-op.

    Returns:
        list of :class:`ValidationFinding` (empty if all checks pass).
    """
    _ = deep  # reserved
    findings: List[ValidationFinding] = []

    subtype = getattr(question, "subtype", "")
    if subtype == "numeric_entry":
        findings.extend(_check_numeric_entry(question))
    elif subtype == "qc":
        findings.extend(_check_qc(question))
    elif subtype == "mcq_multi":
        findings.extend(_check_mcq_multi(question))

    # Always run the catch-all answer-key presence check on every Quant item.
    findings.extend(_check_answer_key_present(question))
    return findings
