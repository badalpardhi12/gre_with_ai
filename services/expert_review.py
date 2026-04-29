"""
Expert review — the FINAL gate before promoting a synthetic question to
``status='live'``.

This is *additional* to the existing six-axis rubric judge that gates
extraction/generation quality. The expert review is intentionally narrow
and focused on the five axes the user identified as load-bearing for
production use:

- ``correctness``           — Solve it independently. Does the marked
                              correct answer actually answer the question
                              correctly?
- ``clarity``               — Is the stem unambiguous? Multiple valid
                              interpretations?
- ``distractor_quality``    — Are wrong options plausible-but-wrong, or
                              obviously wrong / nonsense / off-topic?
- ``difficulty_match``      — Does actual difficulty match the labeled
                              ``difficulty`` tier?
- ``gre_authenticity``      — Does this match GRE conventions and
                              register?

Three judges are polled (Opus 4.7 + Sonnet 4.6 + Gemini 3.1 Pro by
default), with a critical exception: if the drafter model is supplied,
that model is *removed* from the panel. A two-judge panel is acceptable
in that mode (no synthetic self-grading is allowed at this gate).

Promotion rule (verdict='live'):
    every axis must score >= 4 from at least 2 judges.
Otherwise verdict='draft'.

Disagreement rule:
    if the score spread on any axis exceeds 2, the item is downgraded to
    'draft' regardless of the average. The ``review_notes`` field then
    explains which axis disagreed.

The module is reusable across the Princeton, Kaplan, and synthetic
agents — they all consume `expert_review(question_dict, drafter_model)`.
"""
from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Public API surface ───────────────────────────────────────────────


# Axes scored by the expert review panel. Ordered so the verdict
# rationale reads top-to-bottom in audit logs.
EXPERT_AXES: Tuple[str, ...] = (
    "correctness",
    "clarity",
    "distractor_quality",
    "difficulty_match",
    "gre_authenticity",
)


# Default model panel. Aliases match `services.synthetic._llm_adapter`
# so the same factory wiring can serve both gates.
DEFAULT_PANEL: Tuple[str, ...] = ("opus", "sonnet", "gemini-pro")


# Promotion thresholds. Tuned by `tests/synthetic/test_expert_review.py`
# fixtures — change these only with a corresponding test refit.
LIVE_AXIS_THRESHOLD = 4              # axis score floor
LIVE_MIN_JUDGES_AT_THRESHOLD = 2     # at least N judges at-or-above floor
SPREAD_TIEBREAKER_THRESHOLD = 2      # spread > 2 on any axis triggers route-to-draft


EXPERT_SYSTEM_PROMPT = """You are a senior GRE psychometrician performing
the FINAL pre-promotion review of a single practice item. You are NOT
scoring the item against an aspirational ideal — you are deciding whether
this item is fit to ship to a paying GRE prep customer TODAY.

Score the item on FIVE axes, each on a 1-5 integer scale:

- correctness         — Solve the item independently from scratch. Does
                        the marked correct answer ACTUALLY answer the
                        question correctly? (5 = unambiguously yes;
                        1 = the marked answer is wrong.)
- clarity             — Is the stem unambiguous? Could a careful reader
                        plausibly interpret it in multiple ways?
                        (5 = crystal clear; 1 = self-contradictory.)
- distractor_quality  — Are the wrong options plausible-but-wrong (each
                        tied to a real misconception), or are they
                        obviously wrong / nonsense / off-topic?
                        (5 = every distractor is a tempting trap;
                        1 = nonsense distractors. For numeric_entry or
                        no-options items, score 5 vacuously.)
- difficulty_match    — Does the ACTUAL difficulty of solving the item
                        match the labeled difficulty band on a 1-5
                        scale? (5 = exact match; 1 = several bands off.)
- gre_authenticity    — Does the item match real GRE conventions and
                        register? (5 = indistinguishable from official
                        ETS; 1 = wrong register, wrong subtype shape.)

OUTPUT JSON ONLY, NO PREAMBLE, NO MARKDOWN, NO CODE FENCES.

Required exact shape (every axis must appear, justification <= 20 words):
{
  "scores": {
    "correctness":         {"score": <1-5>, "justification": "..."},
    "clarity":             {"score": <1-5>, "justification": "..."},
    "distractor_quality":  {"score": <1-5>, "justification": "..."},
    "difficulty_match":    {"score": <1-5>, "justification": "..."},
    "gre_authenticity":    {"score": <1-5>, "justification": "..."}
  },
  "defects": ["short bullet 1", "short bullet 2"]
}
"""


# ── Data structures ──────────────────────────────────────────────────


@dataclass
class JudgeScores:
    """Scores from one judge."""
    judge: str                              # opaque label, e.g. "opus"
    scores: Dict[str, int] = field(default_factory=dict)
    defects: List[str] = field(default_factory=list)
    justifications: Dict[str, str] = field(default_factory=dict)
    raw_response: str = ""

    def axis(self, axis: str) -> int:
        return int(self.scores.get(axis, 0))


@dataclass
class ExpertReviewResult:
    """Aggregate result returned to callers."""
    verdict: str                            # "live" | "draft"
    scores: Dict[str, List[int]] = field(default_factory=dict)  # axis -> per-judge ints
    defects: List[str] = field(default_factory=list)
    reviewer_notes: str = ""
    per_judge: List[JudgeScores] = field(default_factory=list)
    excluded_drafter: Optional[str] = None
    spread: Dict[str, int] = field(default_factory=dict)
    means: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "scores": self.scores,
            "defects": self.defects,
            "reviewer_notes": self.reviewer_notes,
            "per_judge": [
                {
                    "judge": j.judge,
                    "scores": j.scores,
                    "defects": j.defects,
                    "justifications": j.justifications,
                }
                for j in self.per_judge
            ],
            "excluded_drafter": self.excluded_drafter,
            "spread": self.spread,
            "means": self.means,
        }


# ── Prompt building ──────────────────────────────────────────────────


def _format_options(options: List[Dict[str, Any]], correct_label: str) -> str:
    if not options:
        return "(no options — likely numeric entry)"
    lines: List[str] = []
    for o in options:
        lab = o.get("label") or o.get("letter") or "?"
        text = o.get("text") or o.get("option_text") or ""
        marker = "  <-- MARKED CORRECT" if lab == correct_label else ""
        lines.append(f"  {lab}. {text}{marker}")
    return "\n".join(lines)


def build_expert_user_prompt(question_dict: Dict[str, Any]) -> str:
    """Assemble the per-judge user prompt from a question payload."""
    stem = question_dict.get("stem") or question_dict.get("prompt") or ""
    options = question_dict.get("options") or []
    correct_label = question_dict.get("correct_label", "")
    explanation = question_dict.get("explanation") or ""
    subtype = question_dict.get("subtype", "")
    difficulty = question_dict.get("difficulty") or question_dict.get(
        "difficulty_target", "?"
    )
    source = question_dict.get("source", "synthetic")
    passage = ""
    stim = question_dict.get("stimulus") or {}
    if isinstance(stim, dict):
        passage = (
            stim.get("content")
            or stim.get("passage")
            or stim.get("text")
            or stim.get("body")
            or ""
        )

    parts: List[str] = []
    parts.append(f"SUBTYPE: {subtype}")
    parts.append(f"LABELED DIFFICULTY: {difficulty} (1-5 scale)")
    parts.append(f"SOURCE: {source}")
    parts.append("")
    if passage:
        parts.append("PASSAGE / STIMULUS:")
        parts.append(passage)
        parts.append("")
    parts.append("STEM:")
    parts.append(stem)
    parts.append("")
    parts.append("OPTIONS:")
    parts.append(_format_options(options, correct_label))
    parts.append("")
    if explanation:
        parts.append("AUTHOR EXPLANATION (do NOT take this as authoritative — "
                     "verify the marked answer is actually correct):")
        parts.append(explanation)
        parts.append("")
    parts.append(
        "Score the item now. Output JSON only, no preamble, exactly the "
        "schema described in the system prompt."
    )
    return "\n".join(parts)


# ── Response parsing ─────────────────────────────────────────────────


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return text


def parse_expert_response(judge_label: str, raw: str) -> JudgeScores:
    """Parse one judge's JSON output into a `JudgeScores` instance.

    Defensive: missing axes default to 0 (which trips the spread check
    and routes the item to draft). Invalid JSON also yields zeros so the
    aggregator can detect a panel failure.
    """
    text = _strip_fences(raw)
    payload: Dict[str, Any]
    try:
        payload = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        # Fallback: try to extract a JSON object substring.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(text[start: end + 1])
            except (ValueError, json.JSONDecodeError):
                payload = {}
        else:
            payload = {}
    scores_block = payload.get("scores") or payload
    if not isinstance(scores_block, dict):
        scores_block = {}
    scores: Dict[str, int] = {}
    justifs: Dict[str, str] = {}
    for axis in EXPERT_AXES:
        entry = scores_block.get(axis, {})
        if isinstance(entry, dict):
            raw_score = entry.get("score", 0)
            justifs[axis] = (entry.get("justification")
                             or entry.get("reason") or "")
        elif isinstance(entry, (int, float)):
            raw_score = entry
            justifs[axis] = ""
        else:
            raw_score = 0
            justifs[axis] = ""
        try:
            score_int = int(round(float(raw_score)))
        except (TypeError, ValueError):
            score_int = 0
        score_int = max(0, min(5, score_int))
        scores[axis] = score_int
    defects = payload.get("defects") or []
    if not isinstance(defects, list):
        defects = [str(defects)]
    defects = [str(d).strip() for d in defects if str(d).strip()]
    return JudgeScores(
        judge=judge_label,
        scores=scores,
        defects=defects,
        justifications=justifs,
        raw_response=raw,
    )


# ── Aggregation + verdict ────────────────────────────────────────────


def aggregate_expert_panel(
    per_judge: List[JudgeScores],
) -> ExpertReviewResult:
    """Combine N judge reports into a verdict.

    Verdict='live' iff for EVERY axis at least
    `LIVE_MIN_JUDGES_AT_THRESHOLD` judges report a score >=
    `LIVE_AXIS_THRESHOLD`, AND no axis has a spread strictly greater
    than `SPREAD_TIEBREAKER_THRESHOLD`.

    Spread is `max(scores) - min(scores)` across the panel for that
    axis. A spread > 2 means the panel disagrees too sharply to safely
    promote — those items are routed to 'draft' even if the average
    looks good (the user gets to inspect them).
    """
    if not per_judge:
        return ExpertReviewResult(
            verdict="draft",
            reviewer_notes="No judges responded.",
        )
    scores: Dict[str, List[int]] = {axis: [] for axis in EXPERT_AXES}
    for j in per_judge:
        for axis in EXPERT_AXES:
            scores[axis].append(j.axis(axis))
    spread: Dict[str, int] = {}
    means: Dict[str, float] = {}
    for axis, vals in scores.items():
        spread[axis] = (max(vals) - min(vals)) if vals else 0
        means[axis] = (sum(vals) / len(vals)) if vals else 0.0
    # Promotion check.
    failing_axes: List[str] = []
    for axis in EXPERT_AXES:
        at_threshold = sum(
            1 for s in scores[axis] if s >= LIVE_AXIS_THRESHOLD
        )
        if at_threshold < LIVE_MIN_JUDGES_AT_THRESHOLD:
            failing_axes.append(axis)
    spread_violations = [
        axis for axis, sp in spread.items()
        if sp > SPREAD_TIEBREAKER_THRESHOLD
    ]
    verdict = "live"
    notes_bits: List[str] = []
    if failing_axes:
        verdict = "draft"
        notes_bits.append(
            f"Failing axes (need >={LIVE_MIN_JUDGES_AT_THRESHOLD} judges "
            f">={LIVE_AXIS_THRESHOLD}): " + ", ".join(failing_axes)
        )
    if spread_violations:
        verdict = "draft"
        notes_bits.append(
            f"Panel disagreement (spread > {SPREAD_TIEBREAKER_THRESHOLD}): "
            + ", ".join(
                f"{axis}={spread[axis]}" for axis in spread_violations
            )
        )
    # Collect defects across judges (deduped, preserve order).
    seen: set = set()
    all_defects: List[str] = []
    for j in per_judge:
        for d in j.defects:
            d_norm = d.strip()
            if d_norm and d_norm.lower() not in seen:
                seen.add(d_norm.lower())
                all_defects.append(d_norm)
    if not notes_bits:
        notes_bits.append("All axes passed the live-promotion gate.")
    reviewer_notes = " ; ".join(notes_bits)
    return ExpertReviewResult(
        verdict=verdict,
        scores=scores,
        defects=all_defects,
        reviewer_notes=reviewer_notes,
        per_judge=per_judge,
        spread=spread,
        means=means,
    )


# ── Entry point ──────────────────────────────────────────────────────


# A judge callable accepts (system, user) and returns the raw string
# response. We accept a dependency-injection style so unit tests can
# stub the panel without a network roundtrip.
JudgeCallable = Callable[[str, str], str]


def expert_review(
    question_dict: Dict[str, Any],
    drafter_model: Optional[str] = None,
    *,
    panel: Optional[Dict[str, JudgeCallable]] = None,
    factory: Any = None,
    panel_aliases: Tuple[str, ...] = DEFAULT_PANEL,
) -> Dict[str, Any]:
    """Run the expert review panel and return the verdict dict.

    Parameters
    ----------
    question_dict : dict
        Required keys (best-effort): stem, options (list of {label, text,
        is_correct}), correct_label, explanation, subtype, difficulty
        (or difficulty_target), source. Optional ``stimulus`` (dict with
        ``content``/``passage``/``text``/``body``) for RC items.
    drafter_model : str | None
        If set, the matching alias is excluded from the jury. This is
        the "no self-grading" rule (critical for synthetic items).
    panel : dict[str, JudgeCallable] | None
        Optional pre-bound panel (mostly for unit tests). When set,
        ``factory`` and ``panel_aliases`` are ignored.
    factory : LLMClientFactory | None
        When ``panel`` is None, the factory is used to bind judge
        clients per alias. Each alias is bound to a role named
        ``expert_<alias>`` if present in ``factory.roles``; otherwise
        the alias is used to construct a fresh client at temperature
        0.1.
    panel_aliases : tuple[str, ...]
        Default panel aliases. Drafter alias (if any) is filtered out.

    Returns
    -------
    dict
        ``{verdict, scores, defects, reviewer_notes, per_judge,
        excluded_drafter, spread, means}``
    """
    panel_built = panel
    excluded_drafter: Optional[str] = None
    if panel_built is None:
        panel_built = _build_panel(
            factory=factory,
            panel_aliases=panel_aliases,
            drafter_model=drafter_model,
        )
        if drafter_model and drafter_model in panel_aliases:
            excluded_drafter = drafter_model
    user_prompt = build_expert_user_prompt(question_dict)
    per_judge: List[JudgeScores] = []
    for label, judge_call in panel_built.items():
        raw = ""
        try:
            raw = judge_call(EXPERT_SYSTEM_PROMPT, user_prompt)
        except Exception as exc:
            logger.warning("expert judge %s raised: %s", label, exc)
            raw = json.dumps({"error": str(exc)})
        report = parse_expert_response(label, raw)
        # If the parsed report has a zero on any axis, retry once with a
        # cleaner reformat instruction.
        if any(report.scores.get(axis, 0) == 0 for axis in EXPERT_AXES):
            retry_user = (
                user_prompt
                + "\n\nIMPORTANT: your previous response did not parse. "
                "Output a single JSON object, no markdown fences, no "
                "preamble, no trailing prose. Schema is exactly as in "
                "the system prompt with the five axes listed."
            )
            try:
                raw_retry = judge_call(EXPERT_SYSTEM_PROMPT, retry_user)
                retry_report = parse_expert_response(label, raw_retry)
                # Adopt the retry only if it improved coverage.
                non_zero_orig = sum(
                    1 for axis in EXPERT_AXES
                    if report.scores.get(axis, 0) > 0
                )
                non_zero_retry = sum(
                    1 for axis in EXPERT_AXES
                    if retry_report.scores.get(axis, 0) > 0
                )
                if non_zero_retry > non_zero_orig:
                    report = retry_report
            except Exception as exc:
                logger.warning(
                    "expert judge %s retry raised: %s", label, exc
                )
        per_judge.append(report)
    aggregate = aggregate_expert_panel(per_judge)
    aggregate.excluded_drafter = excluded_drafter
    return aggregate.to_dict()


# ── Panel construction helpers ───────────────────────────────────────


def _build_panel(
    *,
    factory: Any,
    panel_aliases: Tuple[str, ...],
    drafter_model: Optional[str],
) -> Dict[str, JudgeCallable]:
    """Bind each surviving alias to a `JudgeCallable`.

    Pulls a client per alias from the factory; if the factory is None,
    falls back to direct adapter wiring via the local backend.
    """
    aliases = tuple(a for a in panel_aliases if a != drafter_model)
    if len(aliases) < 2:
        raise ValueError(
            f"expert_review needs at least 2 judges after excluding "
            f"drafter alias {drafter_model!r}; got {aliases!r}"
        )
    panel: Dict[str, JudgeCallable] = {}
    for alias in aliases:
        client = _resolve_client(factory, alias)
        panel[alias] = _make_judge_callable(client)
    return panel


def _resolve_client(factory: Any, alias: str):
    """Get an LLMClient for the given alias (factory-aware)."""
    if factory is not None:
        # Convention: callers may pre-register roles "expert_<alias>".
        role_name = f"expert_{alias}"
        if hasattr(factory, "roles") and role_name in factory.roles:
            return factory.for_role(role_name)
        # Fall back: register the role on the fly.
        if hasattr(factory, "roles"):
            factory.roles[role_name] = {
                "model": alias, "temperature": 0.1, "max_tokens": 1500,
            }
            return factory.for_role(role_name)
    # Last resort: build a one-off client via the local backend.
    from services.synthetic.llm_client import get_backend
    factory_fn = get_backend("local")
    client = factory_fn(role=f"expert_{alias}", model=alias,
                       temperature=0.1, max_tokens=1500)
    client.model_alias = alias
    return client


def _make_judge_callable(client) -> JudgeCallable:
    """Wrap an LLMClient into a (system, user) -> str function."""
    def _call(system: str, user: str) -> str:
        resp = client.complete(
            messages=[{"role": "user", "content": user}],
            system=system,
            max_tokens=1500,
        )
        return getattr(resp, "text", "") or ""
    return _call
