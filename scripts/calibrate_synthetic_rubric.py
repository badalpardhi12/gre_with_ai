"""
Phase 0 calibration: run the 6-axis rubric judge on a stratified sample
of known-good imported items (`source='manhattan_5lb_2018'`) and report
whether the thresholds are sane before we start generating synthetic
items.

Goal:
    >= 90% of imported items pass the rubric gate. If not, the
    thresholds are too strict — recommend a relaxation.

Outputs:
    - A JSON report at data/synthetic/runs/phase0/calibration_report.json
    - A summary printed to stdout (per-axis median/mean across the
      sample, judge-agreement rates, recommended thresholds).

Cap: 30 items × 3 judges = 90 LLM calls. At ~$0.03/judge call → ~$2.70.
Cheap.

Usage:
    venv/bin/python scripts/calibrate_synthetic_rubric.py
    venv/bin/python scripts/calibrate_synthetic_rubric.py --limit 5
    venv/bin/python scripts/calibrate_synthetic_rubric.py --dry-run
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from models.database import init_db, Question, QuestionOption, Stimulus  # noqa: E402
from services.log import get_logger  # noqa: E402
from services.synthetic.judge import (  # noqa: E402
    RubricJudge, aggregate_judges, judge_agreement_rate,
)
from services.synthetic.llm_client import (  # noqa: E402
    LLMClient, LLMClientFactory, LLMResponse, register_backend,
)
from services.synthetic.types import RUBRIC_AXES  # noqa: E402

logger = get_logger("calibrate_rubric")


# ── Stub backend (used with --dry-run) ─────────────────────────────


class _DryRunClient(LLMClient):
    """Returns 5/5 across the board — for end-to-end smoke without spend."""

    def __init__(self, role: str, **_):
        self.role = role

    def complete(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def complete_json(self, *args, **kwargs):
        scores = {axis: {"score": 5, "justification": "dry-run"} for axis in RUBRIC_AXES}
        return LLMResponse(text=json.dumps({"scores": scores}),
                           parsed_json={"scores": scores})


def _register_dry_backend():
    register_backend("dry-run", lambda role, **kw: _DryRunClient(role=role))


def _register_local_backend():
    """Register the real local-only adapter, if present."""
    try:
        from services.synthetic import _llm_adapter  # noqa: F401  side-effect import
    except ImportError as exc:
        raise SystemExit(
            "local LLM adapter (services/synthetic/_llm_adapter.py) not "
            f"available: {exc}. Use --dry-run to validate plumbing."
        )


# ── Stratified sampling ────────────────────────────────────────────


SUBTYPES_TO_SAMPLE = (
    "tc", "se", "qc", "mcq_single", "numeric_entry", "rc_single", "rc_multi",
)


def _stratified_sample(n_target: int, source: str = "manhattan_5lb_2018",
                       seed: int = 7) -> List[Question]:
    import random
    rng = random.Random(seed)
    per_subtype = max(1, n_target // len(SUBTYPES_TO_SAMPLE))
    sample: List[Question] = []
    for st in SUBTYPES_TO_SAMPLE:
        rows = list(
            Question
            .select()
            .where((Question.source == source)
                   & (Question.status == "live")
                   & (Question.subtype == st))
        )
        rng.shuffle(rows)
        sample.extend(rows[:per_subtype])
    rng.shuffle(sample)
    return sample[:n_target]


def _serialize_question(q: Question) -> Dict[str, Any]:
    options = list(
        QuestionOption
        .select()
        .where(QuestionOption.question == q)
        .order_by(QuestionOption.option_label)
    )
    stim = None
    if q.stimulus_id:
        s = Stimulus.get_or_none(Stimulus.id == q.stimulus_id)
        if s:
            stim = {
                "type": s.stimulus_type,
                "title": s.title,
                "content": (s.content or "")[:1500],
            }
    correct = next(
        (o.option_label for o in options if o.is_correct), ""
    )
    return {
        "subtype": q.subtype,
        "subtopic": q.subtopic,
        "stem": q.prompt,
        "options": [
            {
                "label": o.option_label,
                "text": o.option_text,
                "is_correct": o.is_correct,
            }
            for o in options
        ],
        "correct_label": correct,
        "explanation": (q.explanation or "")[:1500],
        "claimed_difficulty": q.difficulty_target,
        "stimulus": stim,
    }


# ── Calibration ────────────────────────────────────────────────────


def calibrate(
    limit: int = 30,
    *,
    dry_run: bool = False,
    output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    init_db()

    if dry_run:
        _register_dry_backend()
        backend = "dry-run"
    else:
        _register_local_backend()
        backend = "local"

    factory = LLMClientFactory(
        backend=backend,
        roles={
            "judge_a": {"model": "opus", "temperature": 0.1},
            "judge_b": {"model": "sonnet", "temperature": 0.1},
            "judge_c": {"model": "gemini-pro", "temperature": 0.1},
        },
    )
    panel = {
        "judge_a": factory.for_role("judge_a"),
        "judge_b": factory.for_role("judge_b"),
        "judge_c": factory.for_role("judge_c"),
    }
    rj = RubricJudge(panel)

    sample = _stratified_sample(limit)
    logger.info("calibration sample: %d items", len(sample))

    per_axis_scores: Dict[str, List[float]] = defaultdict(list)
    per_subtype_pass: Dict[str, List[bool]] = defaultdict(list)
    agreement_per_axis: Dict[str, List[float]] = defaultdict(list)
    item_records: List[Dict[str, Any]] = []
    pass_count = 0
    pass_count_legacy = 0  # under the pre-refit (4 / 4.5) thresholds
    decision_counts: Dict[str, int] = defaultdict(int)
    started = time.time()

    for i, q in enumerate(sample, 1):
        payload = _serialize_question(q)
        item_id = f"qid-{q.id}"
        agg = rj.grade(item_id, payload)
        gate = rj.gate(agg)
        # Apples-to-apples: re-evaluate the same scores under the
        # pre-refit thresholds for the before/after delta.
        legacy_passed = agg.passed(
            min_axis=4,
            mean_threshold=4.5,
            fairness_hard_threshold=4,
        )
        decision = agg.gate_decision()
        agreement = judge_agreement_rate(agg.per_judge)
        for axis, m in agg.medians.items():
            per_axis_scores[axis].append(m)
        for axis, rate in agreement.items():
            agreement_per_axis[axis].append(rate)
        per_subtype_pass[q.subtype].append(gate.passed)
        if gate.passed:
            pass_count += 1
        if legacy_passed:
            pass_count_legacy += 1
        decision_counts[decision] += 1
        item_records.append({
            "qid": q.id,
            "subtype": q.subtype,
            "subtopic": q.subtopic,
            "claimed_difficulty": q.difficulty_target,
            "medians": agg.medians,
            "mean": agg.mean_overall,
            "min_axis": agg.min_axis_median,
            "passed_gate": gate.passed,
            "passed_gate_legacy": legacy_passed,
            "decision": decision,
            "failing_axes": agg.failing_axes,
            "agreement": agreement,
        })
        logger.info(
            "[%d/%d] qid=%d subtype=%s mean=%.2f min=%.1f pass=%s legacy=%s decision=%s",
            i, len(sample), q.id, q.subtype, agg.mean_overall,
            agg.min_axis_median, gate.passed, legacy_passed, decision,
        )

    elapsed = time.time() - started
    pass_rate = pass_count / max(1, len(sample))
    pass_rate_legacy = pass_count_legacy / max(1, len(sample))
    summary = {
        "total_items": len(sample),
        "pass_count": pass_count,
        "pass_rate": pass_rate,
        "pass_count_legacy": pass_count_legacy,
        "pass_rate_legacy": pass_rate_legacy,
        "decision_counts": dict(decision_counts),
        "thresholds_used": {
            "min_axis": 3, "mean": 3.8, "fairness_hard": 4,
        },
        "thresholds_legacy": {
            "min_axis": 4, "mean": 4.5, "fairness_hard": 4,
        },
        "elapsed_seconds": round(elapsed, 1),
        "per_axis_mean_of_medians": {
            axis: (sum(scores) / len(scores) if scores else 0.0)
            for axis, scores in per_axis_scores.items()
        },
        "per_axis_min_of_medians": {
            axis: (min(scores) if scores else 0.0)
            for axis, scores in per_axis_scores.items()
        },
        "per_subtype_pass_rate": {
            st: (sum(v) / len(v) if v else 0.0)
            for st, v in per_subtype_pass.items()
        },
        "per_axis_agreement_rate": {
            axis: (sum(v) / len(v) if v else 1.0)
            for axis, v in agreement_per_axis.items()
        },
    }
    summary["recommended_thresholds"] = _recommend_thresholds(summary)
    summary["items"] = item_records

    output_dir = output_dir or (ROOT / "data" / "synthetic" / "runs" / "phase0")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "calibration_report.json"
    with open(report_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    logger.info("wrote %s", report_path)
    return summary


def _recommend_thresholds(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Suggest threshold tweaks based on observed pass rate.

    The defaults below match the post-refit rubric (refinement plan §7,
    R1). The corpus is *known-good* Manhattan items; we expect the bulk
    to clear the gate now that the rubric uses behavioural anchors and
    in-context calibration items.

    Logic:
    - >= 80% pass rate: rubric is calibrated; ship the post-refit
      defaults (min_axis=3, mean=3.8, fairness hard at 4).
    - 60% <= pass rate < 80%: tighten slightly toward the auto-promote
      band (min_axis=3, mean=4.0).
    - < 60%: rubric is still too harsh — surface the report for
      manual anchor review (see plan §11 stop condition).
    """
    pr = summary["pass_rate"]
    notes = []
    if pr >= 0.80:
        rec = {"min_axis": 3, "mean": 3.8, "fairness_hard": 4}
        notes.append(
            "Pass rate >= 80% — refined defaults (min_axis=3, mean=3.8, "
            "fairness hard=4) are calibrated for the corpus."
        )
    elif pr >= 0.60:
        rec = {"min_axis": 3, "mean": 4.0, "fairness_hard": 4}
        notes.append(
            "Pass rate in [60%, 80%) — consider raising mean threshold "
            "to 4.0 to keep marginal items out, but the rubric is "
            "broadly calibrated."
        )
    else:
        rec = {"min_axis": 3, "mean": 3.8, "fairness_hard": 4}
        notes.append(
            "Pass rate < 60% — rubric anchors still too harsh on the "
            "corpus. Recommend manual review of "
            "services/synthetic/calibration/anchors.json before "
            "generating synthetic items (plan §11 stop condition)."
        )
    # Per-axis warning if any axis median averages < 3.5.
    for axis, mean in summary["per_axis_mean_of_medians"].items():
        if mean < 3.5:
            notes.append(
                f"Axis {axis} mean-of-medians is {mean:.2f}; "
                "consider rewording its rubric definition or anchors."
            )
    rec["notes"] = notes
    return rec


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=30,
                   help="Items to sample (default: 30)")
    p.add_argument("--dry-run", action="store_true",
                   help="Use a stub LLM that returns 5/5; no network spend.")
    p.add_argument("--output-dir", type=str, default=None,
                   help="Where to write the report JSON.")
    args = p.parse_args()
    out_dir = Path(args.output_dir) if args.output_dir else None
    summary = calibrate(limit=args.limit, dry_run=args.dry_run,
                        output_dir=out_dir)
    print()
    print("=" * 60)
    print("Phase 0 rubric calibration")
    print("=" * 60)
    print(f"Sample size       : {summary['total_items']}")
    print(f"Pass rate (refit) : {summary['pass_rate'] * 100:.1f}%  "
          f"({summary['pass_count']}/{summary['total_items']})  "
          f"thresholds=min_axis>=3, mean>=3.8, fairness_hard=4")
    print(f"Pass rate (legacy): {summary['pass_rate_legacy'] * 100:.1f}%  "
          f"({summary['pass_count_legacy']}/{summary['total_items']})  "
          f"thresholds=min_axis>=4, mean>=4.5, fairness_hard=4")
    print(f"Decision counts   : {summary.get('decision_counts')}")
    print(f"Elapsed           : {summary['elapsed_seconds']}s")
    print()
    print("Per-axis mean-of-medians:")
    for axis, mean in summary["per_axis_mean_of_medians"].items():
        print(f"  {axis:32s} {mean:5.2f}")
    print()
    print("Per-axis judge agreement (within ±1):")
    for axis, rate in summary["per_axis_agreement_rate"].items():
        print(f"  {axis:32s} {rate * 100:5.1f}%")
    print()
    print("Per-subtype pass rate:")
    for st, rate in summary["per_subtype_pass_rate"].items():
        print(f"  {st:24s} {rate * 100:5.1f}%")
    print()
    print("Recommended thresholds:")
    rec = summary["recommended_thresholds"]
    print(f"  min_axis_threshold = {rec['min_axis']}")
    print(f"  mean_threshold     = {rec['mean']}")
    for note in rec.get("notes", []):
        print(f"  - {note}")


if __name__ == "__main__":
    main()
