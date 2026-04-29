"""Vision-enabled expert review for items whose option grid (or figure)
lives in an image rather than structured text.

Mirrors ``services.expert_review_kaplan`` / the five-axis rubric, but
each judge call is multi-modal — the stem + marked correct answer +
explanation are sent as text and the associated image is inlined
alongside so the judge can actually read the option choices.

Public surface
--------------
* ``VISION_AXES`` — the same five axes the text panel uses.
* ``build_vision_user_message(question_dict)`` — text portion only;
  the caller assembles the full multi-modal message per judge.
* ``vision_expert_review(question_dict, *, image_bytes,
  media_type, judges=None)`` — returns a verdict dict identical in
  shape to ``aggregate_verdict`` output so downstream persistence
  logic works unchanged.

The module is intentionally self-contained (no circular import with
``services.expert_review``). A small ``JudgeReport`` dataclass + parse
helper are duplicated rather than imported to keep this module
callable in isolation (unit tests can mock every LLM path).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from services.log import get_logger

logger = get_logger(__name__)


# ── Configuration ────────────────────────────────────────────────────

VISION_AXES = (
    "correctness",
    "clarity",
    "distractor_quality",
    "difficulty_match",
    "gre_authenticity",
)

PROMOTE_MIN_SCORE = 4                # axis floor for live
PROMOTE_MIN_AGREE = 2                # N judges must hit the floor
DISAGREEMENT_SPREAD = 2              # spread > 2 => escalated to draft

# Per-call budget. 90s is generous for a 5-axis rubric + a 20 KB image.
JUDGE_CALL_TIMEOUT_SEC = float(
    os.environ.get("VISION_REVIEW_JUDGE_TIMEOUT", "90")
)


VISION_SYSTEM_PROMPT = """You are an expert GRE psychometrician
reviewing a single text-completion item. Unlike text-only items the
ANSWER OPTIONS for this one are supplied as an IMAGE — a small table
with one word per row, each row being a candidate blank-filler.

Your job is:

1. Read the stem (provided as text) and form an expected fill.
2. Read the image carefully — list each option letter (A, B, C, ...)
   you can see and its corresponding word. GIF compression + tiny
   fonts are common; if an option is genuinely unreadable, say so
   and score correctness=1.
3. Confirm whether the marked correct letter corresponds to the
   option that best fills the blank.
4. Score the item on five axes, each a 1-5 integer:

   correctness
     Does the marked correct letter truly answer the stem? (1=wrong,
     5=unambiguously correct.)
   clarity
     Is the stem unambiguous? (1=materially ambiguous, 5=ETS-grade.)
   distractor_quality
     Are the non-correct options plausible-but-wrong traps, or junk /
     off-topic / trivially eliminable? (1=junk, 5=every distractor is
     a tempting trap.)
   difficulty_match
     Does the actual cognitive load match the labeled difficulty band?
     (1=many bands off, 5=tight match.)
   gre_authenticity
     Does the item match real GRE TC conventions (single sentence, one
     blank, no more than six options, GRE vocabulary register)?
     (1=never ETS, 5=indistinguishable from ETS.)

Return ONLY a single JSON object with this exact shape:

  {
    "scores": {
      "correctness":         <int 1-5>,
      "clarity":             <int 1-5>,
      "distractor_quality":  <int 1-5>,
      "difficulty_match":    <int 1-5>,
      "gre_authenticity":    <int 1-5>
    },
    "defects": [<zero or more tag strings>],
    "notes": "<one short sentence explaining your lowest axis, or empty
              if everything is 4+>",
    "read_options": {"A": "...", "B": "...", ...}
  }

Defect tag vocabulary (use only these):
  wrong_correct_answer, ambiguous_stem, weak_distractor, off_register,
  difficulty_mislabelled, format_violation, missing_context,
  image_unreadable, other

Be honest. Do NOT inflate scores. Return JSON only — no fences, no
commentary.
"""


# ── Judge report + parsing (duplicated from expert_review for isolation) ─


@dataclass
class JudgeReport:
    judge: str
    scores: Dict[str, int] = field(default_factory=dict)
    defects: List[str] = field(default_factory=list)
    notes: str = ""
    read_options: Dict[str, str] = field(default_factory=dict)
    raw: str = ""
    error: Optional[str] = None


def _parse_vision_response(judge: str, raw: str) -> JudgeReport:
    report = JudgeReport(judge=judge, raw=raw)
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = [
            ln for ln in text.split("\n") if not ln.strip().startswith("```")
        ]
        text = "\n".join(lines)
    obj: Optional[Dict[str, Any]] = None
    # Brace-balanced scan so stray prose before/after doesn't sink us.
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
    for axis in VISION_AXES:
        v = raw_scores.get(axis)
        try:
            iv = int(v)
        except (TypeError, ValueError):
            report.error = f"axis_{axis}_not_int"
            return report
        iv = max(1, min(5, iv))
        report.scores[axis] = iv
    defects = obj.get("defects") or []
    if isinstance(defects, list):
        report.defects = [str(d) for d in defects if isinstance(d, str)]
    notes = obj.get("notes") or ""
    if isinstance(notes, str):
        report.notes = notes
    read_options = obj.get("read_options") or {}
    if isinstance(read_options, dict):
        report.read_options = {
            str(k): str(v) for k, v in read_options.items()
        }
    return report


# ── User-message construction ────────────────────────────────────────


def build_vision_user_message(question: Dict[str, Any]) -> str:
    """Return the text body that accompanies the image in each judge call."""
    stem = (question.get("stem") or question.get("prompt") or "").strip()
    correct_label = (
        question.get("correct_label")
        or question.get("correct_answer")
        or ""
    )
    explanation = (question.get("explanation") or "").strip()
    subtype = question.get("subtype") or ""
    difficulty = question.get("difficulty")
    if difficulty is None:
        difficulty = question.get("difficulty_target")
    source = question.get("source") or ""

    parts = [
        f"SUBTYPE: {subtype}",
        f"DECLARED_DIFFICULTY: {difficulty}",
        f"SOURCE: {source}",
        "",
        "STEM:",
        stem,
        "",
        f"MARKED_CORRECT_LABEL: {correct_label}",
    ]
    if explanation:
        parts.append("")
        parts.append("AUTHOR_EXPLANATION:")
        parts.append(explanation[:2500])
    parts.append("")
    parts.append(
        "The answer options are in the attached image. Read them, then "
        "score the item as the system prompt specifies. JSON only."
    )
    return "\n".join(parts)


# ── Aggregation ─────────────────────────────────────────────────────


def aggregate_vision_panel(reports: Sequence[JudgeReport]) -> Dict[str, Any]:
    """Combine N `JudgeReport`s into the Kaplan-shaped verdict dict."""
    valid = [r for r in reports if not r.error and r.scores]
    panel = [r.judge for r in reports]
    if not valid:
        return {
            "verdict": "draft",
            "scores": {},
            "axis_min": {}, "axis_max": {}, "axis_mean": {},
            "defects": ["other"],
            "judge_notes": [
                {"judge": r.judge, "note": r.notes,
                 "error": r.error or "no_scores"}
                for r in reports
            ],
            "escalated": True,
            "judge_count": 0,
            "panel": panel,
            "failures": [{"axis": "all", "agree_count": 0, "scores": []}],
        }

    per_axis: Dict[str, List[int]] = {ax: [] for ax in VISION_AXES}
    for r in valid:
        for ax in VISION_AXES:
            per_axis[ax].append(r.scores[ax])

    failures = []
    escalated = False
    for ax, scores in per_axis.items():
        spread = max(scores) - min(scores)
        if spread > DISAGREEMENT_SPREAD:
            escalated = True
        agree_high = sum(1 for s in scores if s >= PROMOTE_MIN_SCORE)
        if agree_high < PROMOTE_MIN_AGREE:
            failures.append(
                {"axis": ax, "agree_count": agree_high, "scores": scores}
            )
    verdict = "live" if (not failures and not escalated) else "draft"

    defects: List[str] = []
    seen: set = set()
    for r in valid:
        for d in r.defects:
            if d not in seen:
                defects.append(d)
                seen.add(d)

    return {
        "verdict": verdict,
        "scores": per_axis,
        "axis_min": {ax: min(per_axis[ax]) for ax in VISION_AXES},
        "axis_max": {ax: max(per_axis[ax]) for ax in VISION_AXES},
        "axis_mean": {
            ax: sum(per_axis[ax]) / len(per_axis[ax]) for ax in VISION_AXES
        },
        "defects": defects,
        "judge_notes": [
            {"judge": r.judge, "note": r.notes, "error": r.error,
             "read_options": r.read_options}
            for r in reports
        ],
        "escalated": escalated,
        "judge_count": len(valid),
        "panel": panel,
        "failures": failures,
    }


def render_reviewer_notes(verdict: Dict[str, Any]) -> str:
    lines = [
        f"Verdict: {verdict.get('verdict', '?').upper()}"
        + (" (ESCALATED)" if verdict.get("escalated") else ""),
        f"Panel: {', '.join(verdict.get('panel') or [])} "
        f"(effective judges: {verdict.get('judge_count', 0)})",
    ]
    means = verdict.get("axis_mean") or {}
    if means:
        ax = "; ".join(
            f"{a}={means[a]:.1f} "
            f"({verdict['axis_min'].get(a, '?')}-{verdict['axis_max'].get(a, '?')})"
            for a in VISION_AXES
        )
        lines.append(f"Scores: {ax}")
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
    judge_notes = [
        n for n in (verdict.get("judge_notes") or [])
        if n.get("note") or n.get("error")
    ]
    if judge_notes:
        lines.append("Judge notes:")
        for n in judge_notes:
            tag = n.get("error") or "ok"
            note = n.get("note") or ""
            lines.append(f"  - {n['judge']} [{tag}]: {note}")
    return "\n".join(lines)


# ── Multi-modal judge callables ──────────────────────────────────────


VisionJudgeCall = Callable[[str, str, bytes, str], str]


def _import_gateway():
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


def _media_type_for(path: str) -> str:
    p = path.lower()
    if p.endswith(".gif"):
        return "image/gif"
    if p.endswith(".jpg") or p.endswith(".jpeg"):
        return "image/jpeg"
    if p.endswith(".png"):
        return "image/png"
    return "image/png"


def _make_anthropic_vision_judge(
    model_id: str, max_tokens: int = 1200,
) -> VisionJudgeCall:
    gw = _import_gateway()
    client = gw.FloodgateClient()

    def _call(system: str, user: str, image_bytes: bytes,
              media_type: str) -> str:
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
            },
        }
        return client.call_anthropic(
            model=model_id,
            system=system,
            messages=[{
                "role": "user",
                "content": [
                    image_block,
                    {"type": "text", "text": user},
                ],
            }],
            max_tokens=max_tokens,
            max_retries=5,
        )
    return _call


def _make_gemini_vision_judge(
    model_id: str, max_tokens: int = 1500,
) -> VisionJudgeCall:
    gw = _import_gateway()
    client = gw.FloodgateClient()

    def _call(system: str, user: str, image_bytes: bytes,
              media_type: str) -> str:
        image_part = {
            "inlineData": {
                "mimeType": media_type,
                "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
            },
        }
        return client.call_gemini(
            model=model_id,
            contents=[{
                "role": "user",
                "parts": [image_part, {"text": user}],
            }],
            system_instruction=system,
            max_output_tokens=max_tokens,
            max_retries=5,
        )
    return _call


DEFAULT_VISION_PANEL = [
    {"name": "opus_4_7_vision",
     "provider": "anthropic.claude-opus-4-7", "kind": "anthropic"},
    {"name": "sonnet_4_6_vision",
     "provider": "anthropic.claude-sonnet-4-6", "kind": "anthropic"},
    {"name": "gemini_3_1_pro_vision",
     "provider": "gcp:gemini-3.1-pro-preview", "kind": "gemini"},
]


def build_default_vision_judges(
    *, panel: Optional[Sequence[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    panel = list(panel or DEFAULT_VISION_PANEL)
    judges: List[Dict[str, Any]] = []
    for spec in panel:
        if spec["kind"] == "anthropic":
            call = _make_anthropic_vision_judge(spec["provider"])
        elif spec["kind"] == "gemini":
            call = _make_gemini_vision_judge(spec["provider"])
        else:
            raise ValueError(f"unknown judge kind: {spec['kind']}")
        judges.append({"name": spec["name"], "call": call})
    return judges


# ── Public entry point ───────────────────────────────────────────────


def vision_expert_review(
    question_dict: Dict[str, Any],
    *,
    image_bytes: bytes,
    media_type: str = "image/gif",
    judges: Optional[Sequence[Dict[str, Any]]] = None,
    system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the 3-judge vision panel and return a verdict dict.

    Each judge receives the stem + marked correct label + (optional)
    explanation as text, and the attached image as a second content
    block. Calls are threaded so the wall time is bounded by the slowest
    judge + the per-call timeout, not the sum.

    Returns the same shape as ``services.expert_review.aggregate_verdict``
    plus a ``reviewer_notes`` string and a per-judge ``read_options``
    map showing what each judge actually saw in the image (useful for
    catching mis-paired figures).
    """
    sys_prompt = system_prompt or VISION_SYSTEM_PROMPT
    user_msg = build_vision_user_message(question_dict)
    if judges is None:
        judges = build_default_vision_judges()

    result_boxes: List[Dict[str, Any]] = []
    threads: List[threading.Thread] = []

    def _runner_factory(j: Dict[str, Any], box: Dict[str, Any]):
        def _runner():
            try:
                box["text"] = j["call"](
                    sys_prompt, user_msg, image_bytes, media_type
                )
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
            reports.append(_parse_vision_response(name, box.get("text", "")))

    verdict = aggregate_vision_panel(reports)
    verdict["reviewer_notes"] = render_reviewer_notes(verdict)
    verdict.setdefault("cost_estimate_usd", 0.08 * max(1, len(reports)))
    return verdict


__all__ = [
    "VISION_AXES",
    "VISION_SYSTEM_PROMPT",
    "JudgeReport",
    "JUDGE_CALL_TIMEOUT_SEC",
    "PROMOTE_MIN_SCORE",
    "PROMOTE_MIN_AGREE",
    "DISAGREEMENT_SPREAD",
    "build_vision_user_message",
    "_parse_vision_response",
    "aggregate_vision_panel",
    "render_reviewer_notes",
    "vision_expert_review",
    "build_default_vision_judges",
    "_media_type_for",
]
