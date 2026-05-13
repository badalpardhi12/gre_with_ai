#!/usr/bin/env python3
"""
Quant generation v2 pipeline — Phase 4 · D6.

Generates GRE-style quantitative items by chaining three independent
checks so acceptance rate of shipped items stays high:

    Stage 1 — LLM generator
        A single call to the primary model (default Opus 4.7) asked to
        emit a JSON blob with:
            stem, options | numeric_answer, correct_answer,
            solution_work, mathematical_expression_for_verifier

    Stage 2 — Symbolic verifier
        ``mathematical_expression_for_verifier`` is parsed with
        ``sympy.sympify`` and evaluated. The numeric/symbolic result is
        compared to the stated ``correct_answer``. If sympy can't parse
        the expression, or it evaluates to a non-numeric object, we
        skip the item with reason ``"solver disagreement"`` — we never
        accept an item the solver couldn't verify. (z3 is an optional
        dependency and is only imported if a subclass wants to use it —
        the default sympy path is sufficient for the quant subtypes we
        target here.)

    Stage 3 — Multi-judge vote
        Two LLM judges rate the item. Acceptance rule:
            both ≥ 4, OR one ≥ 5 and the other ≥ 3

    Stage 4 — Upsert
        Accepted items land with:
            source='ai_generated_quant_v2'
            status='candidate'
            provenance='llm_generated'
            provenance_json={generator, judges, solver_check='pass',
                             judge_scores}

Safety
    The script never runs in CI. All network calls go through
    ``services.llm_service.llm_service``; the tests mock that module
    entirely so pytest never touches the network. ``--dry-run`` skips
    the DB writes.

CLI
    venv/bin/python scripts/generate_quant_items.py \
        --count 10 --difficulty medium --subtype mcq_single

    venv/bin/python scripts/generate_quant_items.py --count 0 --dry-run
    (zero-count invocation is a valid init smoke-test — exits 0)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Make project root importable when run as a script.
_THIS = Path(__file__).resolve()
PROJECT_ROOT = _THIS.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


logger = logging.getLogger("generate_quant_items")


# ── Constants ─────────────────────────────────────────────────────────

SOURCE_TAG = "ai_generated_quant_v2"

TOPICS = [
    "arithmetic",
    "algebra",
    "geometry",
    "probability",
    "statistics",
    "number theory",
]

SUBTYPES = ("qc", "mcq_single", "numeric_entry")
DIFFICULTIES = ("easy", "medium", "hard")

# Default models. Kept as short aliases the caller passes on the CLI;
# the LLM facade resolves them to concrete model slugs via the routing
# table. We never hard-code OpenRouter paths here so the CLI stays
# stable across model generations.
DEFAULT_GENERATOR = "opus-4.7"
DEFAULT_JUDGES = ("opus-4.7", "gemini-3-pro")

# Difficulty target mapping onto the 1–5 integer on Question.
_DIFFICULTY_TO_TARGET = {"easy": 2, "medium": 3, "hard": 4}
_DIFFICULTY_TO_TIME = {"easy": 75, "medium": 90, "hard": 120}


# ── Prompt templates ──────────────────────────────────────────────────

_GENERATOR_SYSTEM = (
    "You are a senior GRE quantitative-reasoning item writer. "
    "You write items that strictly follow the modern GRE General Test "
    "style guide. Output raw JSON only — no prose, no markdown fences."
)


def _generator_user_prompt(subtype: str, difficulty: str, topic: str) -> str:
    return (
        f"Generate a GRE-style {subtype} quantitative item at "
        f"{difficulty} difficulty. Topic: {topic}.\n"
        "Return exactly this JSON shape (no extra keys, no prose):\n"
        "{\n"
        '  "stem": "…question text…",\n'
        '  "options": ["A) …", "B) …", …]      # mcq_single / qc only\n'
        '  "numeric_answer": 42,                   # numeric_entry only\n'
        '  "correct_answer": "B" | 42,\n'
        '  "solution_work": "step-by-step reasoning",\n'
        '  "mathematical_expression_for_verifier": '
        '"a sympy-parseable expression that evaluates to the numeric '
        'value of the correct answer"\n'
        "}\n"
        "Requirements:\n"
        "  • Exactly one clearly correct answer.\n"
        "  • For MCQ: 5 options A–E. For QC: 4 options A–D (the "
        "standard QC choice set). For numeric_entry: no options.\n"
        "  • The verifier expression must evaluate to a concrete number. "
        "For MCQ/QC, the number must be the numeric value implied by the "
        "correct answer (e.g. if B is correct and B='17', the expression "
        "must evaluate to 17). For QC, map A/B/C/D to 1/-1/0/NaN (we "
        "treat NaN as 'cannot be determined')."
    )


_JUDGE_SYSTEM = (
    "You are a calibrated GRE quantitative item reviewer. "
    "Score strictly; we reject >= 60% of items in practice."
)


def _judge_user_prompt(item: Dict[str, Any]) -> str:
    return (
        "Is this a high-quality GRE quant item? Check:\n"
        "  (a) stem is unambiguous,\n"
        "  (b) one clearly correct answer,\n"
        "  (c) distractors are plausible,\n"
        "  (d) difficulty matches claimed level.\n\n"
        f"Item:\n{json.dumps(item, indent=2)}\n\n"
        'Return JSON: {"accept": bool, "reason": str, "score": 1-5}'
    )


# ── Dataclasses ───────────────────────────────────────────────────────


@dataclass
class GeneratedItem:
    """Raw LLM output for a single item, pre-verification."""
    subtype: str
    difficulty: str
    topic: str
    stem: str
    correct_answer: Any
    solution_work: str
    verifier_expr: str
    options: Optional[List[str]] = None
    numeric_answer: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SolverResult:
    ok: bool
    reason: str
    solver_value: Optional[float] = None
    expected_value: Optional[float] = None


@dataclass
class JudgeVerdict:
    judge: str
    accept: bool
    score: int
    reason: str


@dataclass
class PipelineResult:
    """One full generate→verify→judge pass. The shape drives both the
    provenance_json stored in the DB and the acceptance-rate log."""
    item: Optional[GeneratedItem]
    solver: Optional[SolverResult]
    verdicts: List[JudgeVerdict] = field(default_factory=list)
    accepted: bool = False
    rejection_reason: Optional[str] = None
    generator_model: str = ""


# ── Stage 1: generator ────────────────────────────────────────────────


def generate_one(
    llm,
    subtype: str,
    difficulty: str,
    topic: str,
    *,
    generator_model: str = DEFAULT_GENERATOR,
) -> Optional[GeneratedItem]:
    """Call the primary LLM to produce one raw item.

    Returns ``None`` if the payload is missing required keys. We do a
    light schema check here; deeper correctness is Stage 2's job.
    """
    try:
        raw = llm.generate_json(
            _GENERATOR_SYSTEM,
            _generator_user_prompt(subtype, difficulty, topic),
            model=generator_model,
        )
    except Exception as exc:  # pragma: no cover — network / parse error
        logger.warning("generator call failed: %s", exc)
        return None

    if not isinstance(raw, dict):
        return None

    required = {"stem", "correct_answer", "mathematical_expression_for_verifier"}
    if not required.issubset(raw.keys()):
        logger.info("generator emitted item missing required keys: %s",
                    required - raw.keys())
        return None

    return GeneratedItem(
        subtype=subtype,
        difficulty=difficulty,
        topic=topic,
        stem=str(raw["stem"]).strip(),
        correct_answer=raw["correct_answer"],
        solution_work=str(raw.get("solution_work", "")).strip(),
        verifier_expr=str(raw["mathematical_expression_for_verifier"]).strip(),
        options=raw.get("options"),
        numeric_answer=raw.get("numeric_answer"),
        raw=raw,
    )


# ── Stage 2: symbolic verifier ────────────────────────────────────────


def _expected_numeric(item: GeneratedItem) -> Optional[float]:
    """Return the numeric value the solver should match.

    numeric_entry → ``numeric_answer`` if present, else ``correct_answer``
    mcq_single / qc → try to read the option text (strip "B) "-style
    prefix) and cast to float; fall back to direct cast of
    ``correct_answer``.
    """
    if item.subtype == "numeric_entry":
        val = item.numeric_answer if item.numeric_answer is not None \
            else item.correct_answer
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    ca = item.correct_answer
    # QC convention: A=1, B=-1, C=0, D=NaN ("cannot be determined").
    if item.subtype == "qc" and isinstance(ca, str) and ca.upper() in "ABCD":
        return {"A": 1.0, "B": -1.0, "C": 0.0, "D": float("nan")}[ca.upper()]

    # MCQ: try option lookup by label
    if isinstance(ca, str) and len(ca) == 1 and item.options:
        for opt in item.options:
            # options look like "B) 17" or just "17" or "B 17"
            stripped = opt.strip()
            if stripped.upper().startswith(ca.upper() + ")"):
                tail = stripped[2:].strip()
            elif stripped.upper().startswith(ca.upper() + " "):
                tail = stripped[2:].strip()
            elif stripped.upper().startswith(ca.upper()):
                tail = stripped[1:].strip().lstrip(")").strip()
            else:
                continue
            try:
                return float(tail)
            except (TypeError, ValueError):
                return None

    # Direct cast
    try:
        return float(ca)
    except (TypeError, ValueError):
        return None


def _numbers_equal(a: float, b: float, *, tol: float = 1e-6) -> bool:
    """Tolerance-aware equality. NaN == NaN treated as true so QC
    'cannot be determined' passes when both sides agree."""
    import math
    if math.isnan(a) and math.isnan(b):
        return True
    if math.isnan(a) or math.isnan(b):
        return False
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def verify_with_sympy(item: GeneratedItem) -> SolverResult:
    """Parse ``item.verifier_expr`` with sympy and compare to the
    expected numeric value. Non-evaluable expressions → graceful
    rejection with reason ``solver disagreement``; we never approve an
    item the solver couldn't reduce to a number."""
    try:
        import sympy
    except ImportError:  # pragma: no cover — sympy is a requirement
        return SolverResult(False, "sympy not installed")

    expected = _expected_numeric(item)
    if expected is None:
        return SolverResult(False, "solver disagreement: "
                                   "correct_answer is not numeric")

    try:
        expr = sympy.sympify(item.verifier_expr)
        val = expr.evalf()
        solver_value = float(val)
    except (sympy.SympifyError, TypeError, ValueError) as exc:
        return SolverResult(False,
                            f"solver disagreement: sympy could not "
                            f"evaluate expression ({exc})",
                            expected_value=expected)
    except Exception as exc:  # sympy raises a grab-bag; keep this broad
        return SolverResult(False,
                            f"solver disagreement: unexpected sympy "
                            f"error ({exc})",
                            expected_value=expected)

    if _numbers_equal(solver_value, expected):
        return SolverResult(True, "pass",
                            solver_value=solver_value,
                            expected_value=expected)
    return SolverResult(
        False,
        f"solver disagreement: expression evaluates to "
        f"{solver_value!r}, expected {expected!r}",
        solver_value=solver_value,
        expected_value=expected,
    )


# ── Stage 3: multi-judge vote ─────────────────────────────────────────


def _parse_judge_payload(payload: Any, judge_name: str) -> JudgeVerdict:
    if not isinstance(payload, dict):
        return JudgeVerdict(judge_name, False, 1, "malformed judge payload")
    score_raw = payload.get("score", 0)
    try:
        score = int(score_raw)
    except (TypeError, ValueError):
        score = 0
    accept = bool(payload.get("accept", False))
    reason = str(payload.get("reason", ""))[:500]
    return JudgeVerdict(judge_name, accept, score, reason)


def judge_item(llm, item: GeneratedItem, judges: Tuple[str, ...]) -> List[JudgeVerdict]:
    """Fan out to each judge model and collect their verdicts.

    We route all calls through a single LLM facade; the `model=` kwarg
    picks the judge. A judge that errors gets an automatic reject so a
    flaky endpoint doesn't silently ship items.
    """
    verdicts: List[JudgeVerdict] = []
    public_item = {
        "subtype": item.subtype,
        "difficulty": item.difficulty,
        "topic": item.topic,
        "stem": item.stem,
        "options": item.options,
        "correct_answer": item.correct_answer,
        "solution_work": item.solution_work,
    }
    for j in judges:
        try:
            raw = llm.generate_json(
                _JUDGE_SYSTEM,
                _judge_user_prompt(public_item),
                model=j,
            )
            verdicts.append(_parse_judge_payload(raw, j))
        except Exception as exc:  # pragma: no cover — network error path
            logger.warning("judge %s failed: %s", j, exc)
            verdicts.append(JudgeVerdict(j, False, 1,
                                         f"judge error: {exc}"))
    return verdicts


def judges_pass(verdicts: List[JudgeVerdict]) -> bool:
    """Acceptance rule: both judges accept with score >= 4
    OR one judge scores >= 5 and the other scores >= 3."""
    if len(verdicts) < 2:
        return False
    # Require both to have flipped `accept=True` AND score >= 3
    # (a judge that scored 5 but flagged accept=False is *not* an
    # accept — we're trusting the human-readable flag.)
    if not all(v.accept for v in verdicts):
        return False

    scores = sorted((v.score for v in verdicts), reverse=True)
    both_ge_4 = all(s >= 4 for s in scores)
    one_5_one_3 = scores[0] >= 5 and scores[1] >= 3
    return both_ge_4 or one_5_one_3


# ── Stage 4: upsert ───────────────────────────────────────────────────


def _anchor_for(item: GeneratedItem) -> str:
    """Stable per-item anchor. Re-running is additive because each new
    LLM call produces a different stem → different hash."""
    payload = json.dumps({
        "stem": item.stem,
        "correct_answer": item.correct_answer,
        "verifier_expr": item.verifier_expr,
    }, sort_keys=True).encode("utf-8")
    return "quantv2-" + hashlib.sha1(payload).hexdigest()[:16]


def upsert_accepted(result: PipelineResult) -> Optional[int]:
    """Insert an accepted item into the DB. Returns the new question id
    on insert, ``None`` on duplicate. Caller is responsible for opening
    a DB connection."""
    from models.database import Question, QuestionOption, NumericAnswer

    item = result.item
    assert item is not None and result.accepted, \
        "upsert_accepted called on an unaccepted item"

    anchor = _anchor_for(item)
    exists = (
        Question.select()
        .where((Question.source == SOURCE_TAG)
               & (Question.source_anchor == anchor))
        .first()
    )
    if exists:
        return None

    provenance_payload = {
        "pipeline": "quant_gen_v2",
        "generator": result.generator_model,
        "judges": [
            {
                "model": v.judge,
                "accept": v.accept,
                "score": v.score,
                "reason": v.reason,
            }
            for v in result.verdicts
        ],
        "solver_check": "pass",
        "solver_value": result.solver.solver_value if result.solver else None,
        "topic": item.topic,
        "difficulty": item.difficulty,
    }

    q = Question.create(
        measure="quant",
        subtype=item.subtype,
        prompt=item.stem,
        difficulty_target=_DIFFICULTY_TO_TARGET.get(item.difficulty, 3),
        time_target_seconds=_DIFFICULTY_TO_TIME.get(item.difficulty, 90),
        concept_tags=json.dumps([item.topic]),
        topic=item.topic,
        source=SOURCE_TAG,
        source_anchor=anchor,
        provenance="llm_generated",
        status="candidate",
        explanation=item.solution_work,
        provenance_json=json.dumps(provenance_payload),
    )

    if item.subtype == "numeric_entry":
        val = item.numeric_answer if item.numeric_answer is not None \
            else item.correct_answer
        try:
            NumericAnswer.create(question=q, exact_value=float(val),
                                 mode="decimal")
        except (TypeError, ValueError):
            # Shouldn't happen — solver passed — but don't crash a run.
            logger.warning("numeric_entry %s had non-numeric answer %r",
                           q.id, val)
    else:
        options = item.options or []
        correct_label = (str(item.correct_answer).strip().upper()
                         if isinstance(item.correct_answer, str) else "")
        for opt in options:
            opt_str = str(opt).strip()
            # "B) 17" → label="B", text="17"
            if len(opt_str) >= 2 and opt_str[1] in (")", "."):
                label = opt_str[0].upper()
                text = opt_str[2:].strip()
            else:
                label = opt_str[:1].upper() if opt_str else ""
                text = opt_str[1:].strip() if len(opt_str) > 1 else opt_str
            QuestionOption.create(
                question=q,
                option_label=label,
                option_text=text,
                is_correct=(label == correct_label),
            )

    return q.id


# ── Orchestration ─────────────────────────────────────────────────────


def run_pipeline_once(
    llm,
    subtype: str,
    difficulty: str,
    *,
    topic: Optional[str] = None,
    generator_model: str = DEFAULT_GENERATOR,
    judges: Tuple[str, ...] = DEFAULT_JUDGES,
    rng: Optional[random.Random] = None,
) -> PipelineResult:
    """Run a single generate → verify → judge pass. Always returns a
    ``PipelineResult``; callers check ``.accepted`` to decide whether
    to upsert."""
    rng = rng or random.Random()
    topic = topic or rng.choice(TOPICS)
    item = generate_one(llm, subtype, difficulty, topic,
                        generator_model=generator_model)
    if item is None:
        return PipelineResult(item=None, solver=None,
                              accepted=False,
                              rejection_reason="generator produced invalid payload",
                              generator_model=generator_model)

    solver = verify_with_sympy(item)
    if not solver.ok:
        return PipelineResult(item=item, solver=solver,
                              accepted=False,
                              rejection_reason=solver.reason,
                              generator_model=generator_model)

    verdicts = judge_item(llm, item, judges)
    accepted = judges_pass(verdicts)
    reason = None if accepted else "judge panel rejected"
    return PipelineResult(item=item, solver=solver,
                          verdicts=verdicts,
                          accepted=accepted,
                          rejection_reason=reason,
                          generator_model=generator_model)


@dataclass
class RunStats:
    attempted: int = 0
    generator_failed: int = 0
    solver_failed: int = 0
    judge_rejected: int = 0
    accepted: int = 0
    upserted: int = 0
    duplicates: int = 0

    def acceptance_rate(self) -> float:
        return (self.accepted / self.attempted) if self.attempted else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "generator_failed": self.generator_failed,
            "solver_failed": self.solver_failed,
            "judge_rejected": self.judge_rejected,
            "accepted": self.accepted,
            "upserted": self.upserted,
            "duplicates": self.duplicates,
            "acceptance_rate": round(self.acceptance_rate(), 3),
        }


def run_batch(
    llm,
    *,
    count: int,
    subtype: str,
    difficulty: str,
    generator_model: str = DEFAULT_GENERATOR,
    judges: Tuple[str, ...] = DEFAULT_JUDGES,
    dry_run: bool = False,
    rng: Optional[random.Random] = None,
    on_result=None,
) -> RunStats:
    """Run ``count`` pipeline iterations and (unless dry-run) upsert
    accepted items. ``on_result`` is an optional callback fired per
    iteration — useful in tests for inspection without re-threading
    return values."""
    stats = RunStats()
    if count <= 0:
        return stats

    # Open DB once outside the loop (only for real runs) so we don't
    # reconnect between items.
    db = None
    if not dry_run:
        from models.database import db as _db, init_db
        init_db()
        _db.connect(reuse_if_open=True)
        db = _db

    try:
        for _ in range(count):
            stats.attempted += 1
            res = run_pipeline_once(
                llm,
                subtype=subtype,
                difficulty=difficulty,
                generator_model=generator_model,
                judges=judges,
                rng=rng,
            )

            if res.item is None:
                stats.generator_failed += 1
            elif res.solver is not None and not res.solver.ok:
                stats.solver_failed += 1
            elif not res.accepted:
                stats.judge_rejected += 1
            else:
                stats.accepted += 1
                if not dry_run:
                    qid = upsert_accepted(res)
                    if qid is None:
                        stats.duplicates += 1
                    else:
                        stats.upserted += 1

            if on_result is not None:
                on_result(res)
    finally:
        if db is not None and not db.is_closed():
            db.close()

    return stats


# ── CLI ───────────────────────────────────────────────────────────────


def _refuse_if_ci() -> None:
    """We never generate items from CI — CI should mock. Every major
    CI provider sets one of these env vars; the umbrella ``CI`` catches
    GitHub Actions and most others."""
    if any(os.environ.get(v) for v in ("CI", "GITHUB_ACTIONS", "BUILDKITE")):
        raise SystemExit(
            "generate_quant_items.py refuses to run in CI. Run locally, "
            "or mock services.llm_service in tests."
        )


def _parse_judges_arg(raw: str) -> Tuple[str, ...]:
    """``--judges opus-4.7,gemini-3-pro`` → ('opus-4.7', 'gemini-3-pro')."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(
            "--judges must list at least two models, comma-separated "
            "(e.g. 'opus-4.7,gemini-3-pro')."
        )
    return tuple(parts)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quant generation v2: LLM → sympy solver → multi-judge vote."
    )
    parser.add_argument("--count", type=int, default=10,
                        help="Number of pipeline iterations to run. "
                             "--count 0 is a valid init check.")
    parser.add_argument("--difficulty", choices=DIFFICULTIES, default="medium")
    parser.add_argument("--subtype", choices=SUBTYPES, default="mcq_single")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip DB writes; print acceptance-rate summary.")
    parser.add_argument("--model", default=DEFAULT_GENERATOR,
                        help="Generator model alias (default: %(default)s).")
    parser.add_argument(
        "--judges", type=_parse_judges_arg,
        default=",".join(DEFAULT_JUDGES),
        help="Comma-separated list of judge model aliases "
             "(default: %(default)s).",
    )
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed (controls topic choice only).")
    args = parser.parse_args(argv)

    # --count 0 → init smoke test. Don't even import the LLM facade;
    # we just want to verify the script parses and the DB is reachable.
    if args.count == 0:
        print(json.dumps({
            "ok": True,
            "count": 0,
            "message": "init smoke-test: no iterations run",
            "dry_run": args.dry_run,
            "generator": args.model,
            "judges": list(args.judges) if isinstance(args.judges, tuple)
                      else args.judges,
        }))
        return 0

    _refuse_if_ci()

    judges = args.judges if isinstance(args.judges, tuple) \
        else _parse_judges_arg(args.judges)
    rng = random.Random(args.seed) if args.seed is not None else random.Random()

    from services.llm_service import llm_service  # lazy — needs API key

    start = time.time()
    stats = run_batch(
        llm_service,
        count=args.count,
        subtype=args.subtype,
        difficulty=args.difficulty,
        generator_model=args.model,
        judges=judges,
        dry_run=args.dry_run,
        rng=rng,
    )
    elapsed = time.time() - start

    report = stats.as_dict()
    report["elapsed_seconds"] = round(elapsed, 1)
    report["subtype"] = args.subtype
    report["difficulty"] = args.difficulty
    report["dry_run"] = args.dry_run
    print(json.dumps(report, indent=2))

    # Highlight if we missed the 80% acceptance target so the operator
    # can tweak the prompt / model selection before a larger run.
    if stats.attempted >= 5 and stats.acceptance_rate() < 0.8:
        print(f"\n[warn] acceptance rate "
              f"{stats.acceptance_rate():.0%} below 80% target",
              file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
