"""
Figure-mispair audit.

Bug 2 context
-------------
Some live questions carry a stimulus image that belongs to a different
question entirely — e.g. a QC item whose "chart" is actually the answer-
option grid from a text-completion question, or a DI question whose
graph shows the wrong quantities. Because the image-attach step was done
by an earlier vision pipeline, the mispairings cluster around specific
batches and need a human-in-the-loop audit to surface.

Approach
--------
For every live question that has an image-bearing stimulus (graph /
table / figure with `data:image/…` content), ask two vision judges
(Opus 4.7 + Sonnet 4.6) whether the image plausibly belongs to the
stem. Each judge returns:

    {
      "matches": <bool>,
      "confidence": "low" | "medium" | "high",
      "reasoning": "<one short sentence>",
      "suspicious": ["<optional tag>", ...]
    }

A question is flagged as a **confirmed mispairing** when BOTH judges
return `matches=false` with `confidence="high"`. When only one judge
flags it, it's noted as a tier-2 disagreement for human review but
not auto-demoted.

Public surface
--------------
* ``MispairJudgment`` — dataclass for a single judge's verdict.
* ``MispairVerdict`` — aggregated two-judge verdict.
* ``extract_first_image(stimulus_content)`` — pull the first
  `data:image/…` blob out of HTML and return (bytes, media_type).
* ``judge_mispair(stem, image_bytes, media_type, judge_call)`` —
  run a single judge.
* ``audit_pair(question, stimulus, *, opus_call, sonnet_call)`` —
  run both judges in parallel (2-way max) and aggregate.
* ``MISPAIR_SYSTEM_PROMPT`` / ``build_user_message``.

Module is deliberately framework-free: ``judge_call`` is any callable
with signature ``(system, user, image_bytes, media_type) -> str``.
Tests inject stubs; the CLI runner wires in the Floodgate client.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# ── Config ───────────────────────────────────────────────────────────

# 60s per-call ceiling (spec says 60s, 2 strikes -> draft). We give
# each judge its own timer.
JUDGE_CALL_TIMEOUT_SEC = float(
    os.environ.get("MISPAIR_JUDGE_TIMEOUT", "60")
)

# When we aggregate: a confirmed mispair requires BOTH judges to say
# matches=False with confidence=="high".
CONFIRM_CONFIDENCE = "high"


# ── Prompt ───────────────────────────────────────────────────────────

MISPAIR_SYSTEM_PROMPT = """You are auditing a GRE question bank. Each
item was extracted from an ebook/publisher source along with an image
(chart, graph, table, or diagram). A prior pipeline sometimes
misattached an image that belongs to a DIFFERENT question — e.g. a
quantitative-comparison item whose "figure" is actually the answer-
option grid from a verbal text-completion item, or a data-interp
question whose bar chart shows sales data when the stem asks about
temperatures.

Your job: look at the image and the stem and decide whether the image
PLAUSIBLY belongs to this question.

Rules
-----
* The test-taker should be able to use the image to answer the stem.
  If the stem never references a figure, and the image contains data
  that is unrelated to the stem's content, that is a MISMATCH.
* If the image is an answer-option grid / a multiple-choice list /
  shows lettered options A-E that have nothing to do with the stem's
  quantities, that is a MISMATCH (suspicious: "looks_like_options").
* If the image shows a chart about topic X but the stem asks about
  topic Y with no connection, that is a MISMATCH (suspicious:
  "wrong_subject" or "wrong_quantities").
* If the image is a plausible companion to the stem — even if you
  can't verify every number — return matches=true. Be CONSERVATIVE:
  only flag obvious mispairings.
* If the image is low-resolution / unreadable but looks generally
  topic-adjacent, return matches=true with confidence=low.
* Output confidence=high ONLY when you are certain. Output
  confidence=low when the image is unreadable or ambiguous.

Output shape (JSON only, no code fences, no prose):

  {
    "matches": <true|false>,
    "confidence": "low" | "medium" | "high",
    "reasoning": "<one short sentence>",
    "suspicious": ["<zero or more short tags>"]
  }

Suspicious tag vocabulary (use subset, or empty list):
  looks_like_options, looks_like_answer_grid, wrong_subject,
  wrong_quantities, wrong_units, unreadable, different_item,
  text_only_image, other
"""


def build_user_message(
    stem: str,
    *,
    subtype: str = "",
    source: str = "",
    stimulus_title: str = "",
) -> str:
    """Text body that accompanies the image in each judge call."""
    parts = []
    if subtype:
        parts.append(f"SUBTYPE: {subtype}")
    if source:
        parts.append(f"SOURCE: {source}")
    if stimulus_title:
        parts.append(f"STIMULUS_TITLE: {stimulus_title}")
    if parts:
        parts.append("")
    parts.append("STEM:")
    parts.append(stem.strip())
    parts.append("")
    parts.append(
        "The image attached is the figure currently paired with this "
        "stem. Does it plausibly belong? Return JSON only."
    )
    return "\n".join(parts)


# ── Image extraction ─────────────────────────────────────────────────

_DATA_URI_RE = re.compile(
    r'data:image/(?P<fmt>png|jpeg|jpg|gif|webp);base64,(?P<b64>[A-Za-z0-9+/=]+)',
    re.IGNORECASE,
)


def extract_first_image(content: str) -> Optional[Tuple[bytes, str]]:
    """Return (bytes, media_type) for the first data URI in `content`.

    Returns None if the stimulus has no embedded image.
    """
    if not content:
        return None
    m = _DATA_URI_RE.search(content)
    if not m:
        return None
    fmt = m.group("fmt").lower()
    media_type = "image/jpeg" if fmt in ("jpg", "jpeg") else f"image/{fmt}"
    try:
        raw = base64.b64decode(m.group("b64"))
    except Exception:
        return None
    return raw, media_type


# ── Dataclasses ──────────────────────────────────────────────────────


@dataclass
class MispairJudgment:
    judge: str
    matches: Optional[bool] = None
    confidence: str = ""  # "low" | "medium" | "high"
    reasoning: str = ""
    suspicious: List[str] = field(default_factory=list)
    raw: str = ""
    error: Optional[str] = None


@dataclass
class MispairVerdict:
    question_id: int
    stimulus_id: int
    judgments: List[MispairJudgment] = field(default_factory=list)
    # Derived:
    confirmed_mispair: bool = False   # both judges: matches=false @ high
    tier2_disagreement: bool = False  # exactly one judge: matches=false @ high

    def as_dict(self) -> Dict[str, Any]:
        return {
            "question_id": self.question_id,
            "stimulus_id": self.stimulus_id,
            "confirmed_mispair": self.confirmed_mispair,
            "tier2_disagreement": self.tier2_disagreement,
            "judgments": [
                {
                    "judge": j.judge,
                    "matches": j.matches,
                    "confidence": j.confidence,
                    "reasoning": j.reasoning,
                    "suspicious": j.suspicious,
                    "error": j.error,
                }
                for j in self.judgments
            ],
        }


# ── Response parsing ────────────────────────────────────────────────


def parse_mispair_response(judge: str, raw: str) -> MispairJudgment:
    """Parse a judge's raw text reply into a MispairJudgment."""
    report = MispairJudgment(judge=judge, raw=raw or "")
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = [ln for ln in text.split("\n")
                 if not ln.strip().startswith("```")]
        text = "\n".join(lines)

    # Brace-balanced scan (tolerant to stray prose before/after).
    obj: Optional[Dict[str, Any]] = None
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        matched = False
        for j in range(i, n):
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[i:j + 1])
                        matched = True
                    except json.JSONDecodeError:
                        i = j + 1
                    break
        if obj is not None:
            break
        if not matched:
            break

    if obj is None:
        report.error = "no_json"
        return report

    if "matches" not in obj:
        report.error = "missing_matches"
        return report
    try:
        report.matches = bool(obj["matches"])
    except Exception:
        report.error = "matches_not_bool"
        return report

    conf = str(obj.get("confidence", "")).strip().lower()
    if conf not in ("low", "medium", "high"):
        # Tolerate — downgrade to "low" rather than erroring out.
        conf = "low"
    report.confidence = conf

    reasoning = obj.get("reasoning", "")
    if isinstance(reasoning, str):
        report.reasoning = reasoning.strip()

    suspicious = obj.get("suspicious") or []
    if isinstance(suspicious, list):
        report.suspicious = [str(s) for s in suspicious if isinstance(s, str)]

    return report


# ── Judge runners ───────────────────────────────────────────────────


JudgeCall = Callable[[str, str, bytes, str], str]


def run_single_judge(
    judge_name: str,
    judge_call: JudgeCall,
    system: str,
    user: str,
    image_bytes: bytes,
    media_type: str,
    *,
    timeout_sec: float = JUDGE_CALL_TIMEOUT_SEC,
    retry_once: bool = True,
) -> MispairJudgment:
    """Invoke one judge with a wall-clock timeout + single retry.

    On first timeout, we retry once. On second timeout, return an
    error-tagged MispairJudgment with `error="llm_timeout"`.
    """
    attempts = 2 if retry_once else 1
    last_err: Optional[str] = None
    for attempt in range(attempts):
        box: Dict[str, Any] = {}

        def _runner():
            try:
                box["text"] = judge_call(system, user, image_bytes, media_type)
            except Exception as e:
                box["error"] = repr(e)

        th = threading.Thread(target=_runner, daemon=True)
        th.start()
        th.join(timeout_sec)
        if th.is_alive():
            last_err = f"timeout_after_{int(timeout_sec)}s"
            # Can't actually kill the thread; leave it daemon and retry.
            continue
        if "error" in box:
            last_err = box["error"]
            # Transient errors worth retrying — retry path already covers it.
            continue
        return parse_mispair_response(judge_name, box.get("text", ""))

    return MispairJudgment(judge=judge_name, error=last_err or "llm_timeout")


def audit_pair(
    *,
    question_id: int,
    stimulus_id: int,
    stem: str,
    image_bytes: bytes,
    media_type: str,
    opus_call: JudgeCall,
    sonnet_call: JudgeCall,
    subtype: str = "",
    source: str = "",
    stimulus_title: str = "",
    parallel: bool = True,
    timeout_sec: float = JUDGE_CALL_TIMEOUT_SEC,
) -> MispairVerdict:
    """Run Opus 4.7 + Sonnet 4.6 vision judges and aggregate.

    Judges run in parallel (2-way max) unless ``parallel=False``.
    """
    user_msg = build_user_message(
        stem,
        subtype=subtype,
        source=source,
        stimulus_title=stimulus_title,
    )

    if parallel:
        results: Dict[str, MispairJudgment] = {}

        def _run(name: str, call: JudgeCall):
            results[name] = run_single_judge(
                name, call, MISPAIR_SYSTEM_PROMPT, user_msg,
                image_bytes, media_type,
                timeout_sec=timeout_sec,
            )

        t1 = threading.Thread(target=_run, args=("opus_4_7_vision", opus_call),
                              daemon=True)
        t2 = threading.Thread(target=_run, args=("sonnet_4_6_vision", sonnet_call),
                              daemon=True)
        t1.start(); t2.start()
        # Slack buffer on top of per-call timeout so the runners (which
        # already enforce their own timeouts + one retry) can finish.
        wall_budget = timeout_sec * 2 + 30
        deadline = time.time() + wall_budget
        for t in (t1, t2):
            remaining = max(1.0, deadline - time.time())
            t.join(remaining)
        judgments = [
            results.get(
                "opus_4_7_vision",
                MispairJudgment(judge="opus_4_7_vision", error="no_result"),
            ),
            results.get(
                "sonnet_4_6_vision",
                MispairJudgment(judge="sonnet_4_6_vision", error="no_result"),
            ),
        ]
    else:
        judgments = [
            run_single_judge(
                "opus_4_7_vision", opus_call, MISPAIR_SYSTEM_PROMPT, user_msg,
                image_bytes, media_type, timeout_sec=timeout_sec,
            ),
            run_single_judge(
                "sonnet_4_6_vision", sonnet_call, MISPAIR_SYSTEM_PROMPT, user_msg,
                image_bytes, media_type, timeout_sec=timeout_sec,
            ),
        ]

    verdict = MispairVerdict(
        question_id=question_id,
        stimulus_id=stimulus_id,
        judgments=judgments,
    )

    # Aggregation: confirmed = both judges say matches=false @ high.
    high_nomatches = [
        j for j in judgments
        if j.matches is False and j.confidence == CONFIRM_CONFIDENCE
    ]
    if len(high_nomatches) == 2:
        verdict.confirmed_mispair = True
    elif len(high_nomatches) == 1:
        verdict.tier2_disagreement = True

    return verdict


__all__ = [
    "JUDGE_CALL_TIMEOUT_SEC",
    "MISPAIR_SYSTEM_PROMPT",
    "MispairJudgment",
    "MispairVerdict",
    "audit_pair",
    "build_user_message",
    "extract_first_image",
    "parse_mispair_response",
    "run_single_judge",
]
