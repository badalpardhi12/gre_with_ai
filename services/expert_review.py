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


# ════════════════════════════════════════════════════════════════════════
# Compatibility shim — names that used to live in the now-retired
# ``services.expert_review_kaplan`` module. The Kaplan agent ran a
# five-axis review with the same axes + thresholds as the synthetic gate
# but exported slightly different helper names; those names are kept
# alive here so downstream scripts (retroactive review, persist_princeton,
# synthetic scripts) all drive through a single module.
# ════════════════════════════════════════════════════════════════════════

import os
import re
import sys
import threading
import time
from typing import Sequence


# Alias the synthetic axes under the Kaplan name.
RUBRIC_AXES = EXPERT_AXES

# Kaplan callers sometimes referenced these constants directly.
PROMOTE_MIN_SCORE = LIVE_AXIS_THRESHOLD
PROMOTE_MIN_AGREE = LIVE_MIN_JUDGES_AT_THRESHOLD
DISAGREEMENT_SPREAD = SPREAD_TIEBREAKER_THRESHOLD

# Default 3-judge panel with gateway provider ids. The synthetic DEFAULT_PANEL
# above stays named by short alias for the synthetic factory; Kaplan callers
# want the full provider-id form so they can match against a drafter.
KAPLAN_DEFAULT_PANEL: List[Dict[str, str]] = [
    {"name": "opus_4_7",   "provider": "anthropic.claude-opus-4-7",   "kind": "anthropic"},
    {"name": "sonnet_4_6", "provider": "anthropic.claude-sonnet-4-6", "kind": "anthropic"},
    {"name": "gemini_3_1", "provider": "gcp:gemini-3.1-pro-preview", "kind": "gemini"},
]

# Defect vocabulary exposed as a tuple so downstream code can `in` test.
DEFECT_TAGS = (
    "wrong_correct_answer",
    "ambiguous_stem",
    "weak_distractor",
    "off_register",
    "difficulty_mislabelled",
    "format_violation",
    "missing_context",
    "other",
)

JUDGE_CALL_TIMEOUT_SEC = float(os.environ.get("EXPERT_REVIEW_JUDGE_TIMEOUT", "60"))


REVIEW_SYSTEM_PROMPT = """You are an expert GRE psychometrician reviewing a single GRE practice question.

You will be shown the full question (stem, options, marked correct answer,
explanation, declared subtype, declared difficulty, and source). Your
job is to score the item on five axes, each on a 1-5 integer scale.

Axes (1 = unacceptable, 3 = passable, 5 = ETS-grade):

  correctness
    Solve the question independently. Does the marked correct answer
    actually answer the question correctly AND is it the only
    fully-correct answer? Score 1 if the marked answer is wrong; 5 if
    the marked answer is unambiguously the only correct choice.

  clarity
    Is the stem unambiguous? Does it admit exactly one defensible
    reading? Score 1 for materially ambiguous wording; 5 for ETS-grade
    precision.

  distractor_quality
    Are the wrong options plausible-but-wrong (testing real
    misconceptions), or are they obviously nonsense / off-topic /
    trivially eliminable? Score 1 if multiple distractors are throwaway
    junk; 5 if every distractor is a defensible trap. (Numeric-entry
    items with no distractors auto-score 5 on this axis — note that and
    move on.)

  difficulty_match
    Does the actual cognitive load match the advertised difficulty tier
    (1=Basic, 3=Medium, 5=Hard typically; the source field tells you
    what the publisher claimed)? Score 1 for "labelled hard but trivial"
    or "labelled basic but graduate-level"; score 5 for a tight match.

  gre_authenticity
    Does this match published GRE conventions and register — sentence
    length, vocabulary level, math notation, answer format, content
    domains? Score 1 for content that would never appear on the actual
    test (e.g. wrong domain, wrong format); 5 for content
    indistinguishable from an ETS-published item.

You must return ONLY a single JSON object with this exact shape:

  {
    "scores": {
      "correctness": <int 1-5>,
      "clarity": <int 1-5>,
      "distractor_quality": <int 1-5>,
      "difficulty_match": <int 1-5>,
      "gre_authenticity": <int 1-5>
    },
    "defects": [<zero or more tag strings from the vocabulary below>],
    "notes": "<one short sentence explaining your lowest-scoring axis;
              empty string if every axis is 4+>"
  }

Defect tag vocabulary (use only these strings):
  wrong_correct_answer, ambiguous_stem, weak_distractor, off_register,
  difficulty_mislabelled, format_violation, missing_context, other

Be honest. Most items should score in the 3-5 range; reserve 1-2 for
items that genuinely fail. Do NOT pad scores up to be polite.

Return JSON only. No markdown fences, no commentary."""


# ── Kaplan-style per-judge report ───────────────────────────────────────


@dataclass
class JudgeReport:
    """Kaplan-compatible per-judge result (structured slightly differently
    from `JudgeScores` to preserve the error field + deprecated ``raw``)."""
    judge: str
    scores: Dict[str, int] = field(default_factory=dict)
    defects: List[str] = field(default_factory=list)
    notes: str = ""
    raw: str = ""
    error: Optional[str] = None


def _clip(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n].rstrip() + " …[truncated]"


def _derive_correct_label(options: Sequence[Dict[str, Any]]) -> Optional[str]:
    for o in options:
        if o.get("is_correct"):
            return o.get("label")
    return None


def build_review_user_message(question: Dict[str, Any]) -> str:
    """Render the question into the user-message text the judges see.

    Kaplan-style structured format (SUBTYPE, DECLARED_DIFFICULTY, etc.).
    Retained for callers that persisted this exact layout in prompt
    caches; prefer `build_expert_user_prompt` for new code.
    """
    stem = (question.get("stem") or question.get("prompt") or "").strip()
    options = question.get("options") or []
    correct_label = (
        question.get("correct_label")
        or question.get("correct_answer")
        or _derive_correct_label(options)
        or ""
    )
    explanation = (question.get("explanation") or "").strip()
    subtype = question.get("subtype") or ""
    difficulty = question.get("difficulty")
    if difficulty is None:
        difficulty = question.get("difficulty_target")
    source = question.get("source") or ""
    stimulus = (
        question.get("stimulus_text")
        or question.get("passage")
        or question.get("stimulus")
        or ""
    )
    if isinstance(stimulus, dict):
        stimulus = stimulus.get("content") or stimulus.get("text") or ""

    options_block = []
    for o in options:
        label = o.get("label") or ""
        text = o.get("text") or ""
        marker = "  *" if (label == correct_label) or o.get("is_correct") else "   "
        options_block.append(f"{marker} {label}. {text}")
    options_rendered = (
        "\n".join(options_block) if options_block
        else "(no multiple choice — numeric / free response)"
    )

    parts = [
        f"SUBTYPE: {subtype}",
        f"DECLARED_DIFFICULTY: {difficulty}",
        f"SOURCE: {source}",
    ]
    if stimulus:
        parts.append("STIMULUS / PASSAGE:")
        parts.append(_clip(str(stimulus), 4000))
    parts.append("STEM:")
    parts.append(_clip(stem, 3000))
    parts.append("OPTIONS:")
    parts.append(options_rendered)
    parts.append(f"MARKED_CORRECT_LABEL: {correct_label}")
    if explanation:
        parts.append("AUTHOR_EXPLANATION:")
        parts.append(_clip(explanation, 2500))
    parts.append("")
    parts.append(
        "Score this item on the five rubric axes and return the JSON object."
    )
    return "\n\n".join(parts)


def _parse_judge_response(judge: str, raw: str) -> JudgeReport:
    """Tolerantly parse a judge's JSON response into a `JudgeReport`."""
    report = JudgeReport(judge=judge, raw=raw)
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = [ln for ln in text.split("\n") if not ln.strip().startswith("```")]
        text = "\n".join(lines)
    obj: Optional[Dict[str, Any]] = None
    # Brace-balanced scan to skip leading prose.
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            for j in range(i, len(text)):
                ch = text[j]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(text[i:j + 1])
                            break
                        except json.JSONDecodeError:
                            i = j + 1
                            break
            if obj is not None:
                break
            continue
        i += 1
    if obj is None:
        report.error = "no_json"
        return report
    raw_scores = obj.get("scores") or {}
    if not isinstance(raw_scores, dict):
        report.error = "scores_not_object"
        return report
    for axis in RUBRIC_AXES:
        v = raw_scores.get(axis)
        try:
            iv = int(v)
        except (TypeError, ValueError):
            report.error = f"axis_{axis}_not_int"
            return report
        if iv < 1 or iv > 5:
            iv = max(1, min(5, iv))
        report.scores[axis] = iv
    defects = obj.get("defects") or []
    if isinstance(defects, list):
        report.defects = [str(d) for d in defects if isinstance(d, str)]
    notes = obj.get("notes") or ""
    if isinstance(notes, str):
        report.notes = notes
    return report


def aggregate_verdict(reports: Sequence[JudgeReport]) -> Dict[str, Any]:
    """Combine Kaplan-style `JudgeReport`s into a verdict dict.

    Uses the same thresholds as `aggregate_expert_panel` but returns a
    Kaplan-shaped dict with ``axis_min``/``axis_max``/``axis_mean``/
    ``failures``/``judge_notes``/``escalated`` keys preserved for
    downstream report renderers.
    """
    valid = [r for r in reports if not r.error and r.scores]
    panel = [r.judge for r in reports]
    if not valid:
        return {
            "verdict": "draft",
            "scores": {},
            "axis_min": {}, "axis_max": {}, "axis_mean": {},
            "defects": ["other"],
            "judge_notes": [{"judge": r.judge, "note": r.notes,
                             "error": r.error or "no_scores"}
                            for r in reports],
            "escalated": True,
            "judge_count": 0,
            "panel": panel,
            "failures": [{"axis": "all", "agree_count": 0, "scores": []}],
        }

    per_axis: Dict[str, List[int]] = {ax: [] for ax in RUBRIC_AXES}
    for r in valid:
        for ax in RUBRIC_AXES:
            per_axis[ax].append(r.scores[ax])

    failures = []
    escalated = False
    for ax, scores in per_axis.items():
        spread = max(scores) - min(scores)
        if spread > DISAGREEMENT_SPREAD:
            escalated = True
        agree_high = sum(1 for s in scores if s >= PROMOTE_MIN_SCORE)
        if agree_high < PROMOTE_MIN_AGREE:
            failures.append({
                "axis": ax,
                "agree_count": agree_high,
                "scores": scores,
            })

    verdict = "live" if (not failures and not escalated) else "draft"

    defects: List[str] = []
    seen = set()
    for r in valid:
        for d in r.defects:
            if d not in seen:
                defects.append(d)
                seen.add(d)

    return {
        "verdict": verdict,
        "scores": per_axis,
        "axis_min": {ax: min(per_axis[ax]) for ax in RUBRIC_AXES},
        "axis_max": {ax: max(per_axis[ax]) for ax in RUBRIC_AXES},
        "axis_mean": {ax: sum(per_axis[ax]) / len(per_axis[ax])
                      for ax in RUBRIC_AXES},
        "defects": defects,
        "judge_notes": [{"judge": r.judge, "note": r.notes,
                         "error": r.error}
                        for r in reports],
        "escalated": escalated,
        "judge_count": len(valid),
        "panel": panel,
        "failures": failures,
    }


def render_reviewer_notes(verdict: Dict[str, Any]) -> str:
    """Render a Kaplan-style verdict dict as a human-readable string."""
    lines = [
        f"Verdict: {verdict.get('verdict', '?').upper()}"
        + (" (ESCALATED)" if verdict.get("escalated") else ""),
        f"Panel: {', '.join(verdict.get('panel') or [])}"
        f" (effective judges: {verdict.get('judge_count', 0)})",
    ]
    means = verdict.get("axis_mean") or {}
    if means:
        ax_summary = "; ".join(
            f"{ax}={means.get(ax, 0):.1f} "
            f"({verdict['axis_min'].get(ax, '?')}-"
            f"{verdict['axis_max'].get(ax, '?')})"
            for ax in RUBRIC_AXES
        )
        lines.append(f"Scores: {ax_summary}")
    failures = verdict.get("failures") or []
    if failures:
        lines.append("Failing axes (need >= 2 judges at >= 4):")
        for f in failures:
            lines.append(
                f"  - {f['axis']}: {f['agree_count']} judge(s) at >=4, "
                f"raw scores {f['scores']}"
            )
    if verdict.get("defects"):
        lines.append(f"Defects: {', '.join(verdict['defects'])}")
    judge_notes = [n for n in (verdict.get("judge_notes") or [])
                   if n.get("note") or n.get("error")]
    if judge_notes:
        lines.append("Judge notes:")
        for n in judge_notes:
            tag = n.get("error") or "ok"
            note = n.get("note") or ""
            lines.append(f"  - {n['judge']} [{tag}]: {note}")
    return "\n".join(lines)


# ── Gateway-backed judge callables (Kaplan path) ────────────────────────


def _import_gateway():
    """Lazy import of the local-only LLM gateway (gitignored)."""
    try:
        from services import _llm_gateway as gw  # type: ignore
        return gw
    except Exception:
        repo = os.environ.get(
            "GRE_MAIN_REPO",
            "/Users/chiku/Documents/side_projects/gre_with_ai",
        )
        if repo not in sys.path:
            sys.path.append(repo)
        from services import _llm_gateway as gw  # type: ignore
        return gw


def _make_anthropic_judge(model_id: str, max_tokens: int = 900,
                          max_retries: int = 1) -> JudgeCallable:
    gw = _import_gateway()
    client = gw.FloodgateClient()

    def _call(system: str, user: str) -> str:
        return client.call_anthropic(
            model=model_id,
            system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
            max_retries=max_retries,
        )
    return _call


def _make_gemini_judge(model_id: str, max_tokens: int = 1200,
                       max_retries: int = 1) -> JudgeCallable:
    gw = _import_gateway()
    client = gw.FloodgateClient()

    def _call(system: str, user: str) -> str:
        return client.call_gemini(
            model=model_id,
            contents=[{"role": "user", "parts": [{"text": user}]}],
            system_instruction=system,
            max_output_tokens=max_tokens,
            max_retries=max_retries,
        )
    return _call


def build_default_judges(
    *,
    drafter_model: Optional[str] = None,
    panel: Optional[Sequence[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Construct the default 3-judge panel (Kaplan-style dicts).

    Each returned entry is ``{"name": <str>, "call": JudgeCallable}``.
    If ``drafter_model`` matches one panel member's ``provider`` id, it
    is excluded (no self-grading).
    """
    panel = list(panel or KAPLAN_DEFAULT_PANEL)
    judges: List[Dict[str, Any]] = []
    for spec in panel:
        if drafter_model and spec["provider"] == drafter_model:
            continue
        if spec["kind"] == "anthropic":
            call = _make_anthropic_judge(spec["provider"])
        elif spec["kind"] == "gemini":
            call = _make_gemini_judge(spec["provider"])
        else:
            raise ValueError(f"unknown judge kind: {spec['kind']}")
        judges.append({"name": spec["name"], "call": call})
    return judges


def expert_review_kaplan(
    question_dict: Dict[str, Any],
    *,
    drafter_model: Optional[str] = None,
    judges: Optional[Sequence[Dict[str, Any]]] = None,
    system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Kaplan-style 3-judge expert review (threaded, with timeout).

    Returns a verdict dict (see `aggregate_verdict`) plus
    ``reviewer_notes`` + ``cost_estimate_usd``. This mirrors the legacy
    `services.expert_review_kaplan.expert_review` entrypoint.
    """
    sys_prompt = system_prompt or REVIEW_SYSTEM_PROMPT
    user_msg = build_review_user_message(question_dict)
    if judges is None:
        judges = build_default_judges(drafter_model=drafter_model)

    result_boxes: List[Dict[str, Any]] = []
    threads: List[threading.Thread] = []

    def _runner_factory(j: Dict[str, Any], box: Dict[str, Any]):
        def _runner():
            try:
                box["text"] = j["call"](sys_prompt, user_msg)
            except Exception as e:  # pragma: no cover - network path
                box["error"] = f"call_failed: {e!r}"
        return _runner

    for j in judges:
        box: Dict[str, Any] = {"judge": j["name"]}
        th = threading.Thread(target=_runner_factory(j, box), daemon=True)
        result_boxes.append(box)
        threads.append(th)
        th.start()

    deadline = time.time() + JUDGE_CALL_TIMEOUT_SEC
    for th in threads:
        remaining = max(0.1, deadline - time.time())
        th.join(remaining)

    reports: List[JudgeReport] = []
    for box, th in zip(result_boxes, threads):
        name = box["judge"]
        if th.is_alive():
            reports.append(JudgeReport(
                judge=name, raw="",
                error=f"timeout_after_{int(JUDGE_CALL_TIMEOUT_SEC)}s",
            ))
        elif "error" in box:
            reports.append(JudgeReport(
                judge=name, raw="", error=box["error"],
            ))
        else:
            reports.append(_parse_judge_response(name, box.get("text", "")))

    verdict = aggregate_verdict(reports)
    verdict["reviewer_notes"] = render_reviewer_notes(verdict)
    verdict.setdefault("cost_estimate_usd",
                       0.05 * max(1, len(reports)))
    return verdict


# ── Review-block embedding (Kaplan compat) ──────────────────────────────


REVIEW_BLOCK_RE = re.compile(
    r"\n*<!--\s*expert_review:\s*\n.*?\n-->\s*\n*",
    re.DOTALL,
)


def embed_review_in_explanation(explanation: str,
                                verdict: Dict[str, Any]) -> str:
    """Idempotently inline the verdict into a trailing HTML comment."""
    body = REVIEW_BLOCK_RE.sub("\n", explanation or "").rstrip()
    payload = {
        "verdict": verdict.get("verdict"),
        "escalated": bool(verdict.get("escalated")),
        "scores": verdict.get("scores"),
        "axis_mean": verdict.get("axis_mean"),
        "defects": verdict.get("defects"),
        "panel": verdict.get("panel"),
        "judge_count": verdict.get("judge_count"),
        "failures": verdict.get("failures"),
        "judge_notes": verdict.get("judge_notes"),
    }
    block = (
        "\n\n<!-- expert_review:\n"
        + json.dumps(payload, indent=2, ensure_ascii=False)
        + "\n-->\n"
    )
    return body + block


def extract_review_from_explanation(
    explanation: str,
) -> Optional[Dict[str, Any]]:
    """Reverse of `embed_review_in_explanation`."""
    if not explanation:
        return None
    m = re.search(r"<!--\s*expert_review:\s*\n(.*?)\n-->",
                  explanation, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
