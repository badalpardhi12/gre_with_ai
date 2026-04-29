"""
Domain check registry.

Subtype-keyed list of checkers; the runner short-circuits on the first
failure and returns a `PipelineResult`. Tests register/unregister
checkers freely; the production registry is built from sibling modules.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

from services.synthetic.types import PipelineResult, PipelineStage


CheckResult = Tuple[bool, str]
Checker = Callable[[Dict[str, Any]], CheckResult]


class DomainCheckRegistry:
    def __init__(self):
        # By default a check applies to one or more subtypes; we keep
        # both per-subtype and "applies to every subtype" buckets.
        self._common: List[Tuple[str, Checker]] = []
        self._per_subtype: Dict[str, List[Tuple[str, Checker]]] = {}

    def register_common(self, name: str, fn: Checker) -> None:
        self._common.append((name, fn))

    def register(self, subtype: str, name: str, fn: Checker) -> None:
        self._per_subtype.setdefault(subtype, []).append((name, fn))

    def for_subtype(self, subtype: str) -> List[Tuple[str, Checker]]:
        return list(self._common) + list(self._per_subtype.get(subtype, []))


def run_checks(
    item_id: str,
    item_payload: Dict[str, Any],
    registry: DomainCheckRegistry,
) -> PipelineResult:
    subtype = item_payload.get("subtype", "")
    checks = registry.for_subtype(subtype)
    failures: List[Dict[str, Any]] = []
    for name, fn in checks:
        try:
            ok, reason = fn(item_payload)
        except Exception as exc:
            ok = False
            reason = f"checker raised: {exc}"
        if not ok:
            failures.append({"check": name, "reason": reason})
    return PipelineResult(
        item_id=item_id,
        stage=PipelineStage.DOMAIN,
        passed=not failures,
        reason="" if not failures else f"{len(failures)} domain failure(s)",
        details={"failures": failures},
    )


# ── Common (subtype-agnostic) checkers ─────────────────────────────


def _check_stem_nonempty(item: Dict[str, Any]) -> CheckResult:
    stem = (item.get("stem") or "").strip()
    return (bool(stem), "stem is empty" if not stem else "")


def _check_no_self_reference(item: Dict[str, Any]) -> CheckResult:
    text = " ".join([
        item.get("stem", "") or "",
        item.get("explanation", "") or "",
        " ".join((o.get("text") or "") for o in item.get("options", []) or []),
    ]).lower()
    bad_phrases = (
        "as an ai", "as a language model", "i would say", "i'm not sure",
        "wait, let me reconsider", "wait — let me reconsider",
        "wait, let me re-examine",
    )
    for phrase in bad_phrases:
        if phrase in text:
            return (False, f"self-reference: {phrase!r}")
    return (True, "")


def _check_unique_correct_mcq(item: Dict[str, Any]) -> CheckResult:
    if item.get("subtype") not in {"mcq_single", "tc", "qc", "rc_single"}:
        return (True, "")
    # Multi-blank TC items carry one correct option per blank, so the
    # shared "exactly 1 correct" rule does not apply. The subtopic
    # labels (tc_2_blank / tc_3_blank) encode the expected count.
    subtopic = (item.get("subtopic") or "").lower()
    expected = 1
    if subtopic == "tc_2_blank":
        expected = 2
    elif subtopic == "tc_3_blank":
        expected = 3
    options = item.get("options") or []
    correct = [o for o in options if o.get("is_correct")]
    if len(correct) != expected:
        return (False,
                f"expected exactly {expected} correct option(s), "
                f"got {len(correct)}")
    return (True, "")


# ── Subtype checkers ────────────────────────────────────────────────


def _check_se_two_correct(item: Dict[str, Any]) -> CheckResult:
    options = item.get("options") or []
    correct = [o for o in options if o.get("is_correct")]
    if len(options) != 6:
        return (False, f"SE expects 6 options, got {len(options)}")
    if len(correct) != 2:
        return (False, f"SE expects exactly 2 correct, got {len(correct)}")
    return (True, "")


def _check_qc_canonical_options(item: Dict[str, Any]) -> CheckResult:
    options = item.get("options") or []
    if len(options) != 4:
        return (False, f"QC expects 4 options, got {len(options)}")
    expected_labels = {"A", "B", "C", "D"}
    actual_labels = {(o.get("label") or "").upper() for o in options}
    if actual_labels != expected_labels:
        return (False, f"QC labels must be A,B,C,D; got {sorted(actual_labels)}")
    return (True, "")


def _check_qc_domain_declared(item: Dict[str, Any]) -> CheckResult:
    """Every single-letter algebraic variable in the stem must appear in
    `domain_assumptions`. We treat lone lower-case ASCII letters that
    are NOT common English words ("a", "I") as variables — generous,
    but a hard reject is appropriate for QC.
    """
    import re
    if item.get("subtype") != "qc":
        return (True, "")
    stem = item.get("stem", "") or ""
    # Lowercase single letters in math context (e.g., "x", "n", "k").
    candidates = set(re.findall(r"\b([a-z])\b", stem))
    candidates -= {"a", "i", "s", "o"}  # common English false positives
    if not candidates:
        return (True, "")
    declared = " ".join(item.get("domain_assumptions", []) or []).lower()
    missing = [v for v in candidates if v not in declared]
    if missing:
        return (False, f"undeclared QC variables: {missing}")
    return (True, "")


def _check_numeric_answer_finite(item: Dict[str, Any]) -> CheckResult:
    import math
    if item.get("subtype") != "numeric_entry":
        return (True, "")
    val = item.get("correct_value")
    if val is None:
        num = item.get("numerator")
        den = item.get("denominator")
        if num is None or den is None or den == 0:
            return (False, "numeric entry needs correct_value or fraction")
        return (True, "")
    try:
        if not math.isfinite(float(val)):
            return (False, "correct_value not finite")
    except (TypeError, ValueError):
        return (False, "correct_value not numeric")
    return (True, "")


# ── Default production registry ────────────────────────────────────


def _build_default_registry() -> DomainCheckRegistry:
    reg = DomainCheckRegistry()
    reg.register_common("stem_nonempty", _check_stem_nonempty)
    reg.register_common("no_self_reference", _check_no_self_reference)
    reg.register_common("unique_correct_mcq", _check_unique_correct_mcq)
    reg.register("se", "se_two_correct", _check_se_two_correct)
    reg.register("qc", "qc_canonical_options", _check_qc_canonical_options)
    reg.register("qc", "qc_domain_declared", _check_qc_domain_declared)
    reg.register("numeric_entry", "numeric_finite", _check_numeric_answer_finite)
    return reg


DEFAULT_REGISTRY = _build_default_registry()
