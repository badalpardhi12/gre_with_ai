"""
Adversarial solver — stage (c).

Each surviving draft is solved cold by N independent solvers. Their
chosen letter is compared to the drafter's `correct_label`; if any
solver disagrees, the item routes to "needs review" and skips the
remaining (expensive) gates.

For Sentence Equivalence specifically, the drafter has a well-known
failure mode where its claimed correct pair is a grammatically-fitting
but meaning-shifting pair while the TRUE synonym pair sits among the
distractors. When both adversarial solvers cold-agree on a pair
different from the claimed key, `reconcile_se_key` surfaces that pair
so the pipeline can swap the key in place of rejecting the item.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from services.log import get_logger
from services.synthetic.llm_client import LLMClient
from services.synthetic.prompts.solver import build_solver_prompt
from services.synthetic.types import PipelineResult, PipelineStage

logger = get_logger("synthetic.solver")


_ANSWER_RE = re.compile(r"ANSWER:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


@dataclass
class SolveAttempt:
    solver_name: str
    chosen: str                     # raw extracted answer string
    matches_key: bool
    reasoning_trace_chars: int
    raw_response: str = ""


def parse_solver_answer(raw: str) -> str:
    """Extract the value after the final `ANSWER:` line, normalized."""
    matches = _ANSWER_RE.findall(raw or "")
    if not matches:
        return ""
    return matches[-1].strip().rstrip(".").strip()


def _normalize_letters(answer: str) -> str:
    """Sort + uppercase comma-separated letters for a stable equality test."""
    parts = [p.strip().upper() for p in answer.split(",") if p.strip()]
    return ",".join(sorted(parts))


def answers_match(claimed: str, observed: str) -> bool:
    """Compare answers, treating SE multi-correct as a set."""
    if not observed:
        return False
    if "," in claimed or "," in observed:
        return _normalize_letters(claimed) == _normalize_letters(observed)
    return claimed.strip().upper() == observed.strip().upper()


class AdversarialSolver:
    """Run a panel of cold-attempt solvers against a draft."""

    def __init__(self, solvers: Dict[str, LLMClient]):
        if not solvers:
            raise ValueError("AdversarialSolver needs at least one solver")
        self.solvers = solvers

    def attempt(
        self,
        item_payload: Dict[str, Any],
        claimed_correct: str,
    ) -> List[SolveAttempt]:
        attempts: List[SolveAttempt] = []
        for name, client in self.solvers.items():
            prompt = build_solver_prompt(item_payload)
            try:
                resp = client.complete(
                    messages=[{"role": "user", "content": prompt["user"]}],
                    system=prompt["system"],
                    max_tokens=1500,
                )
                raw = resp.text or ""
            except Exception as exc:
                logger.exception("solver %s failed", name)
                raw = f"ERROR: {exc}"
            chosen = parse_solver_answer(raw)
            attempts.append(SolveAttempt(
                solver_name=name,
                chosen=chosen,
                matches_key=answers_match(claimed_correct, chosen),
                reasoning_trace_chars=len(raw),
                raw_response=raw,
            ))
        return attempts

    def gate(
        self,
        item_id: str,
        attempts: List[SolveAttempt],
    ) -> PipelineResult:
        passed = bool(attempts) and all(a.matches_key for a in attempts)
        disagreements = [
            {"solver": a.solver_name, "chose": a.chosen}
            for a in attempts
            if not a.matches_key
        ]
        return PipelineResult(
            item_id=item_id,
            stage=PipelineStage.SOLVE,
            passed=passed,
            reason="" if passed else f"disagreement: {disagreements}",
            details={
                "attempts": [
                    {
                        "solver": a.solver_name,
                        "chose": a.chosen,
                        "matches_key": a.matches_key,
                        "trace_chars": a.reasoning_trace_chars,
                    }
                    for a in attempts
                ],
                "trace_chars_mean": (
                    sum(a.reasoning_trace_chars for a in attempts)
                    / max(1, len(attempts))
                ),
            },
        )


# ── SE solver reconciliation ─────────────────────────────────────────


@dataclass
class SEReconcileResult:
    """Outcome of trying to reconcile a disagreement on an SE item.

    `should_swap` tells the caller to overwrite the draft's
    `correct_label` (and `is_correct` flags) with `new_label`. If
    `should_swap` is False the caller should stay with the rejection.
    """
    should_swap: bool
    new_label: str = ""              # sorted "X,Y" form, empty if not swapping
    solver_agreed_pair: str = ""     # raw agreed pair as reported by solvers
    reason: str = ""                 # human-readable explanation for audit


def _canonical_pair(raw: str) -> str:
    """Normalize a raw ANSWER string to `X,Y` in sorted order.

    Empty / malformed / non-two-letter responses return ''.
    """
    if not raw:
        return ""
    parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    # Keep only single uppercase A-F letters (SE has options A..F).
    parts = [p for p in parts if len(p) == 1 and "A" <= p <= "F"]
    if len(parts) != 2 or parts[0] == parts[1]:
        return ""
    return ",".join(sorted(parts))


def reconcile_se_key(
    item_payload: Dict[str, Any],
    claimed_correct: str,
    attempts: List[SolveAttempt],
) -> SEReconcileResult:
    """Decide whether to swap the drafter's claimed SE key.

    We swap only when ALL of the following hold:
      1. The item is SE (6 options).
      2. Every solver returned a well-formed two-letter SE answer.
      3. Every solver's canonical pair is identical.
      4. That pair is NOT the drafter's claimed pair.
      5. Both letters in the agreed pair map to options present on the
         item (defensive — guards against solver hallucinating letters
         outside A-F that we didn't ship).

    This is the exact failure mode the phase-1 prod run surfaced:
    solvers cold-agree, drafter mislabel. Swapping the key (instead of
    rejecting outright) salvages the item with high confidence — two
    independent solvers reading only the stem+options converged on the
    same synonym pair.
    """
    if (item_payload.get("subtype") or "").lower() != "se":
        return SEReconcileResult(should_swap=False, reason="not SE")
    options = item_payload.get("options") or []
    if len(options) != 6:
        return SEReconcileResult(
            should_swap=False,
            reason=f"SE expects 6 options, got {len(options)}",
        )
    if not attempts:
        return SEReconcileResult(should_swap=False, reason="no solver attempts")

    claimed_canon = _canonical_pair(claimed_correct)
    solver_pairs = [_canonical_pair(a.chosen) for a in attempts]
    if any(not p for p in solver_pairs):
        return SEReconcileResult(
            should_swap=False,
            reason=f"solver output malformed: {[a.chosen for a in attempts]}",
        )
    if len(set(solver_pairs)) != 1:
        return SEReconcileResult(
            should_swap=False,
            reason=f"solvers disagree with each other: {solver_pairs}",
        )

    agreed = solver_pairs[0]
    if agreed == claimed_canon:
        return SEReconcileResult(
            should_swap=False,
            reason="solvers already match key",
        )

    available = {str(o.get("label") or "").upper() for o in options}
    agreed_letters = set(agreed.split(","))
    if not agreed_letters.issubset(available):
        return SEReconcileResult(
            should_swap=False,
            solver_agreed_pair=agreed,
            reason=f"agreed pair {agreed!r} includes letters outside options",
        )

    return SEReconcileResult(
        should_swap=True,
        new_label=agreed,
        solver_agreed_pair=agreed,
        reason=(
            f"solvers cold-agree on {agreed!r}; drafter labeled "
            f"{claimed_canon!r}. Swapping key to the solver-agreed pair."
        ),
    )


def apply_se_key_swap(draft: Any, new_label: str) -> None:
    """Mutate `draft` in place so its SE correct_label matches `new_label`.

    Updates both the top-level `correct_label` and each option's
    `is_correct` flag. Safe to call even if `new_label` is empty — the
    function no-ops.

    `draft` is either a `DraftItem` (dataclass with `.options`) or a
    dict-shaped payload (has `options` list). Both paths are supported
    because the pipeline uses DraftItem while tests sometimes use the
    payload form.
    """
    if not new_label:
        return
    winners = {p.strip().upper() for p in new_label.split(",") if p.strip()}
    options = getattr(draft, "options", None)
    if options is None:
        options = draft.get("options") if isinstance(draft, dict) else None
    if not options:
        return
    for opt in options:
        label = (getattr(opt, "label", None)
                 if not isinstance(opt, dict)
                 else opt.get("label"))
        label = str(label or "").upper()
        is_correct = label in winners
        if isinstance(opt, dict):
            opt["is_correct"] = is_correct
        else:
            opt.is_correct = is_correct
    if isinstance(draft, dict):
        draft["correct_label"] = new_label
    else:
        draft.correct_label = new_label

