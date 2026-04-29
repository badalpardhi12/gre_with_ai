"""
Rubric judge — stage (e) of the pipeline.

Three-judge panel scores each draft on the 6-axis rubric (1-5 each),
then we aggregate per-axis median across judges and gate per the
refined thresholds in plan §7:

    fairness_bias median >= FAIRNESS_HARD_THRESHOLD (4) — HARD; and
    every other axis median >= MIN_AXIS_THRESHOLD     (3); and
    mean across axes >= MEAN_THRESHOLD                (3.8)

Beyond the binary pass/fail, `JudgeAggregate.gate_decision()` returns
one of {"auto_promote", "pass_to_sme", "marginal_revise", "reject"}
following the four-stage rule in plan §7. The pipeline orchestrator
uses that to route items to the critic-revise loop or directly to the
SME queue, instead of treating the judge as a binary filter.
"""
from __future__ import annotations

import copy
import json
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from services.log import get_logger
from services.synthetic.llm_client import LLMClient
from services.synthetic.prompts.judge import build_judge_prompt
from services.synthetic.types import (
    CalibrationAnchor,
    JudgeAxisScore,
    JudgeReport,
    PipelineResult,
    PipelineStage,
    RUBRIC_AXES,
    RUBRIC_AXIS_DESCRIPTIONS,
    load_calibration_anchors,
)

logger = get_logger("synthetic.judge")


# Refined thresholds — see plan §7 for derivation. These ship as the
# defaults *after* the rubric-anchor + calibration-item refit; the
# pre-refit defaults (4 / 4.5) drove a 7% pass rate on known-good
# Manhattan items because the rubric framed 5 = "indistinguishable
# from official ETS".
MIN_AXIS_THRESHOLD = 3
MEAN_THRESHOLD = 3.8
FAIRNESS_HARD_THRESHOLD = 4
# Auto-promote candidate if all axes >= 4 AND mean >= 4.3.
AUTO_PROMOTE_MIN_AXIS = 4
AUTO_PROMOTE_MEAN = 4.3
# Marginal-pass (route to SME) if mean >= 3.5 with at most one axis at 3.
MARGINAL_MIN_MEAN = 3.5


GateDecision = str  # {"auto_promote", "pass_to_sme", "marginal_revise", "reject"}


@dataclass
class JudgeAggregate:
    """Per-item aggregate of an N-judge panel."""
    item_id: str
    per_judge: List[JudgeReport]
    medians: Dict[str, float]
    mean_overall: float
    min_axis_median: float
    failing_axes: List[str]
    # Optional pre-aggregation: if bias offsets were applied, the
    # `raw_per_judge` list keeps the original (pre-offset) reports for
    # auditing. Empty when no offsets were applied.
    raw_per_judge: List[JudgeReport] = field(default_factory=list)

    def passed(
        self,
        min_axis: float = MIN_AXIS_THRESHOLD,
        mean_threshold: float = MEAN_THRESHOLD,
        fairness_hard_threshold: float = FAIRNESS_HARD_THRESHOLD,
    ) -> bool:
        """Hard binary gate.

        Fairness is enforced separately at `fairness_hard_threshold`;
        all other axes must clear `min_axis`; mean across axes must
        clear `mean_threshold`.
        """
        fairness = self.medians.get("fairness_bias", 0.0)
        if fairness < fairness_hard_threshold:
            return False
        for axis, m in self.medians.items():
            if axis == "fairness_bias":
                continue
            if m < min_axis:
                return False
        return self.mean_overall >= mean_threshold

    def gate_decision(
        self,
        min_axis: float = MIN_AXIS_THRESHOLD,
        mean_threshold: float = MEAN_THRESHOLD,
        fairness_hard_threshold: float = FAIRNESS_HARD_THRESHOLD,
        auto_promote_min_axis: float = AUTO_PROMOTE_MIN_AXIS,
        auto_promote_mean: float = AUTO_PROMOTE_MEAN,
        marginal_min_mean: float = MARGINAL_MIN_MEAN,
    ) -> GateDecision:
        """Four-state routing per plan §7.

        - "reject":           hard fail (any axis < min_axis besides
                              fairness, OR fairness < hard, OR mean <
                              marginal_min_mean).
        - "marginal_revise":  passes the hard floor but mean is in
                              [marginal_min_mean, mean_threshold) — the
                              critic-revise loop should attempt to
                              repair before the next judge call.
        - "pass_to_sme":      passes the gate but does not auto-promote
                              (i.e. mean in [mean_threshold,
                              auto_promote_mean) OR has at least one
                              axis at min_axis).
        - "auto_promote":     all axes >= auto_promote_min_axis AND
                              mean >= auto_promote_mean.
        """
        fairness = self.medians.get("fairness_bias", 0.0)
        if fairness < fairness_hard_threshold:
            return "reject"
        non_fairness_min = min(
            m for axis, m in self.medians.items() if axis != "fairness_bias"
        ) if self.medians else 0.0
        if non_fairness_min < min_axis:
            # Below the hard floor on a non-fairness axis. If mean is
            # still in the marginal band the reviser may still rescue
            # the item; otherwise reject outright.
            if self.mean_overall >= marginal_min_mean:
                return "marginal_revise"
            return "reject"
        if (
            non_fairness_min >= auto_promote_min_axis
            and self.mean_overall >= auto_promote_mean
        ):
            return "auto_promote"
        if self.mean_overall >= mean_threshold:
            return "pass_to_sme"
        if self.mean_overall >= marginal_min_mean:
            return "marginal_revise"
        return "reject"


def parse_judge_response(judge_name: str, item_id: str, raw: str) -> JudgeReport:
    """Best-effort parse of a judge's JSON output.

    Defensive: if the response is broken JSON or omits an axis, we
    record a 0 for the missing axis and log. The aggregator treats
    any 0 as a hard fail since the judge effectively didn't score it.
    """
    text = raw.strip()
    # Strip common code-fence wrapping.
    if text.startswith("```"):
        text = "\n".join(
            ln for ln in text.split("\n") if not ln.strip().startswith("```")
        )
    payload: Dict[str, Any]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(
            "judge %s returned non-JSON for item %s: %s", judge_name, item_id, e
        )
        payload = {}
    axes_payload = payload.get("scores", payload)
    axes: List[JudgeAxisScore] = []
    if not isinstance(axes_payload, dict):
        axes_payload = {}
    for axis in RUBRIC_AXES:
        entry = axes_payload.get(axis, {})
        if isinstance(entry, dict):
            score = entry.get("score", 0)
            justification = entry.get("justification", "") or entry.get("reason", "")
        elif isinstance(entry, (int, float)):
            score = entry
            justification = ""
        else:
            score = 0
            justification = ""
        try:
            score_int = int(round(float(score)))
        except (TypeError, ValueError):
            score_int = 0
        # Clamp to [0, 5]; 0 reserved for "judge didn't score".
        score_int = max(0, min(5, score_int))
        axes.append(
            JudgeAxisScore(axis=axis, score=score_int, justification=justification)
        )
    return JudgeReport(
        judge_name=judge_name,
        item_id=item_id,
        axes=axes,
        raw_response=raw,
    )


def apply_judge_offsets(
    reports: List[JudgeReport],
    judge_offsets: Optional[Dict[str, Dict[str, float]]],
) -> List[JudgeReport]:
    """Subtract per-judge per-axis bias offsets in place; return new list.

    `judge_offsets[judge_name][axis] = +0.5` means that judge has been
    over-scoring this axis by 0.5 on average against SME ground truth,
    so we subtract 0.5 before aggregation. Scores are clamped to [0, 5]
    and rounded to the nearest int.
    """
    if not judge_offsets:
        return list(reports)
    adjusted: List[JudgeReport] = []
    for r in reports:
        per_axis = judge_offsets.get(r.judge_name, {})
        if not per_axis:
            adjusted.append(r)
            continue
        new_axes: List[JudgeAxisScore] = []
        for a in r.axes:
            offset = per_axis.get(a.axis, 0.0)
            adjusted_score = max(0, min(5, int(round(a.score - offset))))
            new_axes.append(
                JudgeAxisScore(
                    axis=a.axis,
                    score=adjusted_score,
                    justification=a.justification,
                )
            )
        adjusted.append(
            JudgeReport(
                judge_name=r.judge_name,
                item_id=r.item_id,
                axes=new_axes,
                raw_response=r.raw_response,
            )
        )
    return adjusted


def aggregate_judges(
    item_id: str,
    reports: List[JudgeReport],
    *,
    min_axis_threshold: float = MIN_AXIS_THRESHOLD,
    raw_per_judge: Optional[List[JudgeReport]] = None,
) -> JudgeAggregate:
    """Per-axis median across judges, mean across axes."""
    medians: Dict[str, float] = {}
    for axis in RUBRIC_AXES:
        scores = [r.axis_score(axis) or 0 for r in reports]
        medians[axis] = float(statistics.median(scores)) if scores else 0.0
    mean_overall = (
        sum(medians.values()) / len(medians) if medians else 0.0
    )
    min_axis_median = min(medians.values()) if medians else 0.0
    failing = [
        axis for axis, m in medians.items() if m < min_axis_threshold
    ]
    return JudgeAggregate(
        item_id=item_id,
        per_judge=reports,
        medians=medians,
        mean_overall=mean_overall,
        min_axis_median=min_axis_median,
        failing_axes=failing,
        raw_per_judge=list(raw_per_judge or []),
    )


class RubricJudge:
    """Run a panel of judges against a draft and gate on the rubric.

    `judges` is a dict of `{name: LLMClient}`. The judge name is opaque
    — useful for logging and for downstream agreement-rate stats. Three
    is the plan-recommended size; one is fine for unit tests.

    `judge_offsets` is an optional `{judge_name: {axis: float}}` mapping
    of per-judge per-axis bias offsets, subtracted before aggregation.
    Per plan §8 these are refreshed periodically from a slice of
    SME-rated items.

    `calibration_anchors` is the list of in-context anchor items
    inlined into every judge prompt (plan §7). Pass `None` to skip the
    anchor block entirely (mostly for unit tests).

    `drafter_model_alias` enforces no-self-grading (plan §8): if a
    judge in this panel has the same model alias as the drafter, we
    raise at construction time so the orchestrator catches the misuse.
    """

    def __init__(
        self,
        judges: Dict[str, LLMClient],
        min_axis_threshold: int = MIN_AXIS_THRESHOLD,
        mean_threshold: float = MEAN_THRESHOLD,
        fairness_hard_threshold: int = FAIRNESS_HARD_THRESHOLD,
        judge_offsets: Optional[Dict[str, Dict[str, float]]] = None,
        calibration_anchors: Optional[List[CalibrationAnchor]] = None,
        drafter_model_alias: Optional[str] = None,
        shuffle_options: bool = False,
    ):
        if not judges:
            raise ValueError("RubricJudge needs at least one judge")
        if drafter_model_alias:
            for jname, client in judges.items():
                model_alias = getattr(client, "model_alias", None)
                if model_alias and model_alias == drafter_model_alias:
                    raise ValueError(
                        f"no-self-grade violation: judge {jname!r} uses "
                        f"the same model alias ({model_alias!r}) as the "
                        f"drafter; pick a different model for this judge."
                    )
        self.judges = judges
        self.min_axis_threshold = min_axis_threshold
        self.mean_threshold = mean_threshold
        self.fairness_hard_threshold = fairness_hard_threshold
        self.judge_offsets = judge_offsets or {}
        self.calibration_anchors = list(calibration_anchors or [])
        self.drafter_model_alias = drafter_model_alias
        self.shuffle_options = shuffle_options

    def _maybe_load_anchors(self) -> List[CalibrationAnchor]:
        """Allow late-load of anchors if the caller didn't pass any.

        We try the default file path; if it's missing (e.g. test env
        without the fixture), fall through silently to no-anchor mode.
        """
        if self.calibration_anchors:
            return self.calibration_anchors
        try:
            self.calibration_anchors = load_calibration_anchors()
        except FileNotFoundError:
            self.calibration_anchors = []
        return self.calibration_anchors

    def grade(
        self,
        item_id: str,
        item_payload: Dict[str, Any],
    ) -> JudgeAggregate:
        """Run every judge in the panel on `item_payload` and aggregate.

        `item_payload` is the structured draft (stem, options,
        explanation, subtype, claimed difficulty). The judge prompt
        wraps it with the rubric definitions, the in-context calibration
        anchors, and asks for JSON output.
        """
        anchors = self._maybe_load_anchors()
        reports: List[JudgeReport] = []
        for name, client in self.judges.items():
            prompt = build_judge_prompt(
                item_payload,
                RUBRIC_AXIS_DESCRIPTIONS,
                calibration_anchors=anchors,
                shuffle_options=self.shuffle_options,
                # Per-judge shuffle seed so judges see different
                # permutations — kills any residual position bias from
                # judges anchoring on the same first option.
                shuffle_seed=(name + "::" + item_id) if self.shuffle_options else None,
            )
            try:
                resp = client.complete_json(
                    messages=[{"role": "user", "content": prompt["user"]}],
                    system=prompt["system"],
                    max_tokens=2400,
                )
                raw = resp.text or json.dumps(resp.parsed_json or {})
            except Exception as exc:
                logger.exception(
                    "judge %s failed on item %s", name, item_id
                )
                raw = json.dumps({"error": str(exc)})
            reports.append(parse_judge_response(name, item_id, raw))
        # Apply per-judge bias offsets if configured. We keep the raw
        # reports on the aggregate so audit reports can show before/after.
        adjusted = apply_judge_offsets(reports, self.judge_offsets)
        raw_kept: List[JudgeReport] = []
        if self.judge_offsets:
            raw_kept = [copy.deepcopy(r) for r in reports]
        return aggregate_judges(
            item_id,
            adjusted,
            min_axis_threshold=self.min_axis_threshold,
            raw_per_judge=raw_kept,
        )

    def gate(
        self,
        aggregate: JudgeAggregate,
    ) -> PipelineResult:
        passed = aggregate.passed(
            min_axis=self.min_axis_threshold,
            mean_threshold=self.mean_threshold,
            fairness_hard_threshold=self.fairness_hard_threshold,
        )
        decision = aggregate.gate_decision(
            min_axis=self.min_axis_threshold,
            mean_threshold=self.mean_threshold,
            fairness_hard_threshold=self.fairness_hard_threshold,
        )
        reason = "" if passed else (
            f"failing_axes={aggregate.failing_axes} "
            f"mean={aggregate.mean_overall:.2f} "
            f"min_axis_median={aggregate.min_axis_median:.2f} "
            f"decision={decision}"
        )
        details = {
            "medians": aggregate.medians,
            "mean": aggregate.mean_overall,
            "min_axis": aggregate.min_axis_median,
            "failing_axes": aggregate.failing_axes,
            "decision": decision,
            "per_judge": [
                {
                    "name": r.judge_name,
                    "axes": {a.axis: a.score for a in r.axes},
                }
                for r in aggregate.per_judge
            ],
        }
        return PipelineResult(
            item_id=aggregate.item_id,
            stage=PipelineStage.JUDGE,
            passed=passed,
            reason=reason,
            details=details,
        )


def judge_agreement_rate(reports: List[JudgeReport]) -> Dict[str, float]:
    """Inter-judge agreement (within ±1 score) per axis.

    Returns a {axis: fraction_in_agreement} dict. We use the
    "all-pairs within ±1" rule rather than Cohen's kappa because
    five-point Likert items with 3 judges make kappa noisy.
    """
    agreement: Dict[str, float] = {}
    if len(reports) < 2:
        return {axis: 1.0 for axis in RUBRIC_AXES}
    for axis in RUBRIC_AXES:
        scores = [r.axis_score(axis) or 0 for r in reports]
        # Count pairs that disagree by more than 1.
        pairs_total = 0
        pairs_ok = 0
        for i in range(len(scores)):
            for j in range(i + 1, len(scores)):
                pairs_total += 1
                if abs(scores[i] - scores[j]) <= 1:
                    pairs_ok += 1
        agreement[axis] = pairs_ok / pairs_total if pairs_total else 1.0
    return agreement


# ── Triage judge + jury composition ──────────────────────────────────


# A triage judge filters obvious junk before the senior jury runs.
# Threshold is intentionally lenient — we only want to short-circuit
# items that no judge could plausibly defend (e.g., mean < TRIAGE_MEAN
# or any axis = 1).
TRIAGE_MIN_AXIS = 2
TRIAGE_MIN_MEAN = 2.5


@dataclass
class TriageOutcome:
    """Result of the triage stage."""
    item_id: str
    aggregate: JudgeAggregate
    proceed_to_jury: bool
    reason: str = ""


class TriageJudge:
    """Single fast model that filters obvious rejects.

    Plan §8: a Haiku-tier judge runs first; only items that clear the
    triage thresholds proceed to the (expensive) senior jury. Items
    that fail triage skip the jury entirely.
    """

    def __init__(
        self,
        client: LLMClient,
        min_axis: int = TRIAGE_MIN_AXIS,
        min_mean: float = TRIAGE_MIN_MEAN,
        calibration_anchors: Optional[List[CalibrationAnchor]] = None,
    ):
        self.client = client
        self.min_axis = min_axis
        self.min_mean = min_mean
        self.calibration_anchors = calibration_anchors

    def screen(self, item_id: str, item_payload: Dict[str, Any]) -> TriageOutcome:
        rj = RubricJudge(
            {"triage": self.client},
            min_axis_threshold=self.min_axis,
            mean_threshold=self.min_mean,
            calibration_anchors=self.calibration_anchors,
        )
        agg = rj.grade(item_id, item_payload)
        proceed = agg.mean_overall >= self.min_mean and agg.min_axis_median >= self.min_axis
        reason = "" if proceed else (
            f"triage_reject mean={agg.mean_overall:.2f} "
            f"min_axis={agg.min_axis_median:.1f}"
        )
        return TriageOutcome(
            item_id=item_id,
            aggregate=agg,
            proceed_to_jury=proceed,
            reason=reason,
        )


def make_jury(
    factory: Any,  # LLMClientFactory; declared Any to avoid an import cycle.
    drafter_model_alias: str,
    *,
    jury_size: int = 3,
    preferred_models: Optional[Tuple[str, ...]] = None,
) -> Dict[str, LLMClient]:
    """Construct a `{name: LLMClient}` jury that excludes the drafter.

    Picks `jury_size` clients from `preferred_models` skipping any
    whose alias matches `drafter_model_alias`. The pipeline checks
    no-self-grade at construction; this helper guarantees it upstream.

    `preferred_models` defaults to ("opus", "sonnet", "gemini-pro").
    The factory must already have role configs for the slots it
    receives (slot names: "judge_a", "judge_b", "judge_c", ...). The
    helper picks the first jury_size aliases that aren't the drafter
    and binds them to consecutive slot names.
    """
    if not drafter_model_alias:
        raise ValueError("drafter_model_alias is required for jury composition")
    candidates = list(preferred_models or ("opus", "sonnet", "gemini-pro"))
    candidates = [c for c in candidates if c != drafter_model_alias]
    if len(candidates) < jury_size:
        raise ValueError(
            f"only {len(candidates)} jury candidates after excluding "
            f"drafter alias {drafter_model_alias!r}; need {jury_size}"
        )
    panel: Dict[str, LLMClient] = {}
    slot_names = [f"judge_{chr(ord('a') + i)}" for i in range(jury_size)]
    for slot, alias in zip(slot_names, candidates[:jury_size]):
        # The factory must already have this slot configured. If not,
        # surface a helpful error.
        if slot not in factory.roles:
            raise KeyError(
                f"factory has no role config for {slot!r}; expected "
                f"slots {slot_names!r} bound to non-drafter aliases."
            )
        client = factory.for_role(slot)
        panel[slot] = client
    return panel
