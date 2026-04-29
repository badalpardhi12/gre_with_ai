"""Expert-jury review for GRE question items.

This module implements a quality-gate retrofit that scores extracted /
authored items along five rubric axes using a panel of three frontier
LLMs ("judges"). Items that pass the bar are eligible for ``status='live'``;
items that fall below the bar are demoted to ``status='draft'`` with a
per-axis breakdown attached for SME triage.

The rubric (1-5 each, higher is better):

  1. ``correctness``          — judge solves the item independently and
                                 confirms the marked answer is the only
                                 fully correct option.
  2. ``clarity``              — the stem is unambiguous and admits a
                                 single defensible reading.
  3. ``distractor_quality``   — wrong options are plausible-but-wrong,
                                 not nonsense / off-topic / trivially
                                 eliminable.
  4. ``difficulty_match``     — the actual difficulty matches the
                                 advertised band (Basic / Medium / Hard
                                 in Kaplan parlance).
  5. ``gre_authenticity``     — register, length, content, and answer
                                 conventions match the published GRE.

Promotion rule (``verdict='live'``): for every axis, at least 2 of the
3 judges award a score >= 4. Otherwise ``verdict='draft'`` and the
per-axis breakdown is preserved in ``review_notes``.

Disagreement rule: if any axis shows a spread > 2 between judges, an
``escalated`` flag is set so the caller can route to SME tiebreaker
review (we don't auto-promote disagreeing items).

The module is network-free at import time. Real LLM calls flow through
the local-only ``services._llm_gateway`` (which stamps the project token
header) and only happen when :func:`expert_review` is invoked without a
``judges=...`` override. Tests inject stub judges so they stay offline.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence


# Wall-clock cap per judge call; if a model hangs (which the Floodgate
# client's per-request 180s timeout × built-in retry chain can stretch
# to >>10 min), we abandon the judge rather than blocking the whole
# bulk run. The judge's report becomes ``error="timeout"`` which still
# counts toward the verdict's ``judge_count``.
#
# Default 60s matches a single post-cap attempt (see
# FLOODGATE_POST_TIMEOUT_CAP below) plus a small slop for JSON parsing.
JUDGE_CALL_TIMEOUT_SEC = float(os.environ.get("EXPERT_REVIEW_JUDGE_TIMEOUT", "60"))


# ── Rubric ──────────────────────────────────────────────────────────────

RUBRIC_AXES = (
    "correctness",
    "clarity",
    "distractor_quality",
    "difficulty_match",
    "gre_authenticity",
)

# An item passes only when every axis has at least PROMOTE_MIN_AGREE
# judges scoring >= PROMOTE_MIN_SCORE.
PROMOTE_MIN_SCORE = 4
PROMOTE_MIN_AGREE = 2

# A spread larger than this on any axis triggers escalation rather than
# silent promotion / demotion.
DISAGREEMENT_SPREAD = 2

# Defect tags the rubric may surface. Tied loosely to the rubric axes
# so reviewers can group failures across the panel.
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


# ── Judge identity ──────────────────────────────────────────────────────

# Default panel: Opus 4.7 + Sonnet 4.6 + Gemini 3.1 Pro. The provider tag
# is what we compare against ``drafter_model`` so a synthetic-item drafter
# isn't on its own jury (relevant for Stage F LLM-generated items;
# imported items always retain the full panel).
DEFAULT_PANEL: List[Dict[str, str]] = [
    {"name": "opus_4_7",     "provider": "anthropic.claude-opus-4-7",
     "kind": "anthropic"},
    {"name": "sonnet_4_6",   "provider": "anthropic.claude-sonnet-4-6",
     "kind": "anthropic"},
    {"name": "gemini_3_1",   "provider": "gcp:gemini-3.1-pro-preview",
     "kind": "gemini"},
]


# ── Prompts ─────────────────────────────────────────────────────────────

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


def build_review_user_message(question: Dict[str, Any]) -> str:
    """Render the question into the user-message text the judges see."""
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
    options_rendered = "\n".join(options_block) if options_block else "(no multiple choice — numeric / free response)"

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
    parts.append("Score this item on the five rubric axes and return the JSON object.")
    return "\n\n".join(parts)


def _clip(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n].rstrip() + " …[truncated]"


def _derive_correct_label(options: Sequence[Dict[str, Any]]) -> Optional[str]:
    for o in options:
        if o.get("is_correct"):
            return o.get("label")
    return None


# ── Verdict aggregation ─────────────────────────────────────────────────


@dataclass
class JudgeReport:
    """One judge's per-axis scores + defect tags + free-text note."""
    judge: str
    scores: Dict[str, int] = field(default_factory=dict)
    defects: List[str] = field(default_factory=list)
    notes: str = ""
    raw: str = ""
    error: Optional[str] = None


def _parse_judge_response(judge: str, raw: str) -> JudgeReport:
    """Tolerantly parse a judge's JSON response into a :class:`JudgeReport`."""
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
    """Combine judge reports into a single verdict dict.

    Returns::

      {
        "verdict": "live" | "draft",
        "scores": {axis: [score per judge]},
        "axis_min": {axis: int},
        "axis_max": {axis: int},
        "axis_mean": {axis: float},
        "defects": [<deduped tags across judges>],
        "judge_notes": [{"judge": ..., "note": ..., "error": ...}],
        "escalated": bool,        # true when spread > 2 anywhere
        "judge_count": int,
        "panel": [judge_name, ...],
        "failures": [{"axis": ..., "agree_count": ..., "scores": [...]}, ...]
      }
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


# ── Default judge callable wiring ──────────────────────────────────────


JudgeCallable = Callable[[str, str], str]
"""Signature: ``judge(system_prompt, user_message) -> raw_text``."""


def _import_gateway():
    """Lazy import of the local-only LLM gateway. Tests bypass this."""
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


def build_default_judges(*, drafter_model: Optional[str] = None,
                          panel: Optional[Sequence[Dict[str, str]]] = None,
                          ) -> List[Dict[str, Any]]:
    """Construct the default 3-judge panel.

    If ``drafter_model`` is provided and matches one of the panel
    members' ``provider`` ids, that judge is excluded (no self-grading).
    """
    panel = list(panel or DEFAULT_PANEL)
    judges: List[Dict[str, Any]] = []
    for spec in panel:
        if drafter_model and spec["provider"] == drafter_model:
            continue
        if spec["kind"] == "anthropic":
            call = _make_anthropic_judge(spec["provider"])
        elif spec["kind"] == "gemini":
            call = _make_gemini_judge(spec["provider"])
        else:  # pragma: no cover - defensive
            raise ValueError(f"unknown judge kind: {spec['kind']}")
        judges.append({"name": spec["name"], "call": call})
    return judges


# ── Public entrypoint ──────────────────────────────────────────────────


def expert_review(
    question_dict: Dict[str, Any],
    *,
    drafter_model: Optional[str] = None,
    judges: Optional[Sequence[Dict[str, Any]]] = None,
    system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the 3-judge expert review on a single question.

    Args:
      question_dict: must include at least ``stem`` (or ``prompt``) and
        ``options`` (list of {label, text, is_correct}). Optional:
        ``correct_label`` / ``correct_answer``, ``explanation``,
        ``subtype``, ``difficulty``, ``source``, ``stimulus_text``.
      drafter_model: when set, exclude the judge whose ``provider`` id
        matches (no self-grading; relevant for synthetic items).
      judges: optional override of the judge panel. Each judge is a
        ``{"name": str, "call": JudgeCallable}`` dict. Used by tests.
      system_prompt: optional system-prompt override (tests).

    Returns:
      verdict dict — see :func:`aggregate_verdict` for schema, plus a
      ``reviewer_notes`` rendered string suitable for stashing in the
      DB row's ``review_notes`` field.
    """
    sys_prompt = system_prompt or REVIEW_SYSTEM_PROMPT
    user_msg = build_review_user_message(question_dict)
    if judges is None:
        judges = build_default_judges(drafter_model=drafter_model)

    # Fire all judges in parallel (≤3 threads). Each thread enforces its
    # own wall-clock cap via JUDGE_CALL_TIMEOUT_SEC. The outer join
    # budget is the same cap + a small slop so a single hung judge
    # doesn't gate the others.
    reports_by_judge: Dict[str, JudgeReport] = {}
    result_boxes: List[Dict[str, Any]] = []
    threads: List[threading.Thread] = []

    def _runner_factory(j: Dict[str, Any], box: Dict[str, Any]):
        def _runner():
            try:
                box["text"] = j["call"](sys_prompt, user_msg)
            except Exception as e:  # pragma: no cover - network/rate limit path
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
    # Rough cost estimate: 3 judges × ~500 tokens out + ~1k tokens in
    # ≈ $0.05-0.15 per item depending on model mix. The exact spend is
    # whatever the project quota records; this is for monitoring only.
    verdict.setdefault("cost_estimate_usd",
                       0.05 * max(1, len(reports)))
    return verdict


def render_reviewer_notes(verdict: Dict[str, Any]) -> str:
    """Render the verdict as a short human-readable string for storage."""
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


# ── Persistence helper for explanation footer ──────────────────────────

REVIEW_BLOCK_RE = re.compile(
    r"\n*<!--\s*expert_review:\s*\n.*?\n-->\s*\n*",
    re.DOTALL,
)


def embed_review_in_explanation(explanation: str, verdict: Dict[str, Any]) -> str:
    """Inline the verdict into an HTML comment at the end of *explanation*.

    Idempotent: a second call with a new verdict replaces the previous
    block rather than appending. The persisted footer carries enough
    structure to round-trip into JSON for the cache.
    """
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


def extract_review_from_explanation(explanation: str) -> Optional[Dict[str, Any]]:
    """Reverse of :func:`embed_review_in_explanation` — returns the
    payload dict if a previous block is present."""
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
