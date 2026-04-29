"""
End-to-end synthetic-pipeline orchestrator.

Wires the stages laid out in refinement plan §4:

    diversity sampler (R3)
        -> retrieval grounding (R3, optional)
            -> drafter
                -> critic           (different model from drafter)
                    -> reviser      (same model family as drafter)
                        loop ≤ 2 cycles
                -> domain checks
                    -> adversarial solvers
                        -> triage judge       (R4)
                            -> senior jury    (R4, no-self-grade)
                                -> ambiguity probe
                                    -> persist (status='candidate')

Until R4 lands, the orchestrator runs a single judge stage instead of
the triage + senior split, but it already enforces the no-self-grade
constraint at the panel-construction level (RubricJudge raises if any
judge shares the drafter's model alias).

Until R3 lands, the orchestrator uses the simpler `build_seeds` from
the existing seeder; the new DiversitySampler will plug into the same
entry point.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.log import get_logger
from services.synthetic.ambiguity import AmbiguityChecker
from services.synthetic.critic import Critic, CriticReview
from services.synthetic.domain_checks import DEFAULT_REGISTRY, run_checks
from services.synthetic.generator import Generator
from services.synthetic.judge import (
    JudgeAggregate, RubricJudge,
)
from services.synthetic.reviser import Reviser
from services.synthetic.solver import (
    AdversarialSolver, apply_se_key_swap, reconcile_se_key,
)
from services.synthetic.types import (
    DraftItem, PipelineResult, PipelineStage, Seed,
)

logger = get_logger("synthetic.pipeline")


# Loop budget for the critic/revise cycle. Self-Refine literature
# (Madaan et al. 2023) shows diminishing returns past 2 rounds.
DEFAULT_REVISE_BUDGET = 2


@dataclass
class PipelineOutcome:
    """Per-item trace of one end-to-end run."""
    item_id: str
    seed: Seed
    final_status: str                   # "persisted" | "rejected" | "marginal_revise"
    decision: str                       # judge gate_decision or "domain_failed" etc.
    revise_rounds: int = 0
    final_judge: Optional[JudgeAggregate] = None
    domain_result: Optional[PipelineResult] = None
    solver_result: Optional[PipelineResult] = None
    ambiguity_result: Optional[PipelineResult] = None
    reject_reason: str = ""
    final_draft: Optional[DraftItem] = None
    critic_history: List[CriticReview] = field(default_factory=list)


def _draft_to_payload(draft: DraftItem) -> Dict[str, Any]:
    """Mirror the reviser-side helper for sending to graders/solvers."""
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
        "subtopic": draft.seed.subtopic if draft.seed else "",
        "claimed_difficulty": draft.difficulty_target,
        "vocab_tier": draft.vocab_tier,
        "domain_assumptions": list(draft.domain_assumptions),
        "stimulus": draft.stimulus,
        "correct_value": draft.correct_value,
        "numerator": draft.numerator,
        "denominator": draft.denominator,
        "tolerance": draft.tolerance,
    }


@dataclass
class SyntheticPipeline:
    """Orchestrates one item through every stage.

    The pipeline owns the no-self-grade check at construction time:
    if `drafter_model_alias` is supplied, both the critic and judge
    panel are validated to exclude that alias. The reviser is allowed
    (and encouraged) to share the drafter's family for voice
    consistency.
    """
    generator: Generator
    critic: Critic
    reviser: Reviser
    judge: RubricJudge
    solver: AdversarialSolver
    ambiguity: Optional[AmbiguityChecker] = None
    domain_registry: Any = DEFAULT_REGISTRY
    revise_budget: int = DEFAULT_REVISE_BUDGET
    drafter_model_alias: Optional[str] = None

    def __post_init__(self):
        # Verify the critic isn't the same family as the drafter.
        if self.drafter_model_alias:
            critic_alias = getattr(self.critic.client, "model_alias", None)
            if critic_alias and critic_alias == self.drafter_model_alias:
                raise ValueError(
                    f"no-self-grade violation: critic uses the same "
                    f"model alias ({critic_alias!r}) as the drafter."
                )
            # Judge enforces its own alias check internally; but only
            # when constructed with drafter_model_alias. Re-attach
            # here for safety.
            if not self.judge.drafter_model_alias:
                # Re-validate with our drafter alias.
                for jname, client in self.judge.judges.items():
                    alias = getattr(client, "model_alias", None)
                    if alias and alias == self.drafter_model_alias:
                        raise ValueError(
                            f"no-self-grade violation: judge {jname!r} "
                            f"uses the drafter alias ({alias!r})."
                        )

    def run_one(
        self,
        seed: Seed,
        subtopic_display: Optional[str] = None,
        item_id: Optional[str] = None,
    ) -> PipelineOutcome:
        """Run a single seed all the way to a final outcome."""
        item_id = item_id or f"{seed.subtopic}-{int(datetime.now().timestamp())}"
        subtopic_display = subtopic_display or seed.subtopic

        # 1. Drafter
        try:
            draft = self.generator.draft(seed, subtopic_display)
        except Exception as exc:
            logger.exception("drafter failed for %s", item_id)
            return PipelineOutcome(
                item_id=item_id, seed=seed,
                final_status="rejected", decision="drafter_failed",
                reject_reason=str(exc),
            )
        outcome = PipelineOutcome(
            item_id=item_id, seed=seed,
            final_status="rejected",  # tentative
            decision="",
            final_draft=draft,
        )

        # 2-4. Critic-revise loop. Each cycle:
        #   - Domain check: cheap, deterministic; reject early if it fails
        #     and there is no useful note to feed to the reviser.
        #   - Judge the draft: shared with the loop so we know if we
        #     should revise.
        #   - Critic: produce localised notes if the judge says
        #     "marginal_revise" or "pass_to_sme".
        #   - Reviser: apply notes; loop again.
        #
        # The loop terminates when (a) the draft auto-promotes,
        # (b) the budget is exhausted, or (c) the draft is rejected and
        # cannot be salvaged.
        domain_result = run_checks(item_id, _draft_to_payload(draft),
                                   self.domain_registry)
        outcome.domain_result = domain_result
        if not domain_result.passed:
            # Hard structural failure — no point grading or revising.
            outcome.decision = "domain_failed"
            outcome.reject_reason = domain_result.reason
            return outcome

        for cycle in range(self.revise_budget + 1):
            payload = _draft_to_payload(draft)
            judge_agg = self.judge.grade(item_id, payload)
            outcome.final_judge = judge_agg
            decision = judge_agg.gate_decision()
            outcome.decision = decision
            logger.info(
                "pipeline %s cycle=%d decision=%s mean=%.2f min_axis=%.1f",
                item_id, cycle, decision, judge_agg.mean_overall,
                judge_agg.min_axis_median,
            )
            if decision == "auto_promote":
                outcome.final_status = "persisted_pending"
                break
            if decision == "reject":
                outcome.final_status = "rejected"
                outcome.reject_reason = "judge_reject"
                break
            if cycle >= self.revise_budget:
                # Budget exhausted; the draft will be marked
                # marginal_revise / pass_to_sme as terminal status.
                outcome.final_status = (
                    "persisted_pending"
                    if decision == "pass_to_sme"
                    else "marginal_revise"
                )
                break
            # decision in {pass_to_sme, marginal_revise} → revise.
            review = self.critic.review(item_id, payload, judge_aggregate=judge_agg)
            outcome.critic_history.append(review)
            if not review.notes:
                # Critic had nothing to say — exit the loop early.
                outcome.final_status = (
                    "persisted_pending"
                    if decision == "pass_to_sme"
                    else "marginal_revise"
                )
                break
            new_draft = self.reviser.revise(draft, review)
            if new_draft is draft:
                # Reviser punted (drift guard, malformed JSON, etc.) —
                # no point looping further.
                outcome.final_status = (
                    "persisted_pending"
                    if decision == "pass_to_sme"
                    else "marginal_revise"
                )
                break
            draft = new_draft
            outcome.revise_rounds += 1
            outcome.final_draft = draft

            # Re-run cheap domain checks on the revised draft; if the
            # reviser broke it, fall back to the previous draft.
            re_domain = run_checks(item_id, _draft_to_payload(draft),
                                   self.domain_registry)
            outcome.domain_result = re_domain
            if not re_domain.passed:
                logger.warning(
                    "pipeline %s revision %d failed domain checks: %s",
                    item_id, outcome.revise_rounds, re_domain.reason,
                )
                outcome.final_status = "rejected"
                outcome.decision = "domain_failed_post_revision"
                outcome.reject_reason = re_domain.reason
                return outcome

        # 5. Adversarial solvers — only if we'd otherwise persist.
        if outcome.final_status not in {"persisted_pending", "marginal_revise"}:
            return outcome
        solver_attempts = self.solver.attempt(
            _draft_to_payload(draft), draft.correct_label
        )
        solver_res = self.solver.gate(item_id, solver_attempts)
        outcome.solver_result = solver_res
        if not solver_res.passed:
            # SE reconciliation: the drafter's single worst failure mode
            # is labelling a grammatically-fitting-but-meaning-shifting
            # pair as correct while the TRUE synonym pair sits among the
            # distractors. When both cold solvers agree on a pair that
            # is not the drafter's key, swap the key to the solver-
            # agreed pair and re-gate — that's the authoritative reading
            # and we'd rather salvage the item than reject it.
            reconcile = reconcile_se_key(
                _draft_to_payload(draft), draft.correct_label,
                solver_attempts,
            )
            if reconcile.should_swap:
                logger.info(
                    "pipeline %s SE key swap: %s -> %s (%s)",
                    item_id, draft.correct_label, reconcile.new_label,
                    reconcile.reason,
                )
                apply_se_key_swap(draft, reconcile.new_label)
                outcome.final_draft = draft
                # Re-gate against the swapped key. The stored attempts
                # already contain the solver-agreed letters, so we can
                # just recompute `matches_key` locally.
                for att in solver_attempts:
                    from services.synthetic.solver import answers_match
                    att.matches_key = answers_match(
                        reconcile.new_label, att.chosen
                    )
                solver_res = self.solver.gate(item_id, solver_attempts)
                outcome.solver_result = solver_res
            if not solver_res.passed:
                outcome.final_status = "rejected"
                outcome.decision = "solver_disagreement"
                outcome.reject_reason = solver_res.reason
                return outcome

        # 6. Ambiguity probe — optional but recommended.
        if self.ambiguity:
            probes = self.ambiguity.probe(_draft_to_payload(draft),
                                          draft.correct_label)
            amb_res = self.ambiguity.gate(item_id, probes, draft.correct_label)
            outcome.ambiguity_result = amb_res
            if not amb_res.passed:
                outcome.final_status = "rejected"
                outcome.decision = "ambiguous"
                outcome.reject_reason = amb_res.reason
                return outcome

        return outcome
