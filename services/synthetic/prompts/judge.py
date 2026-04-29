"""
Judge-prompt template — the 6-axis rubric in JSON-output form.

Two changes from the v1 prompt that drove the Phase 0 7% pass rate:

1. **Behavioural band anchors.** Per refinement plan §7, every axis
   ships with explicit 1/3/5 descriptors so the judge sees concrete
   language at each band rather than internalising the scale from a
   one-line definition.
2. **In-context calibration items.** 6 worked anchors (3 known-good,
   3 known-bad) are pinned to the user message so the judge sees what
   each band looks like before scoring the actual item. These items
   are explicitly marked as reference-only and must not appear in the
   judge's output.

R4 addition: option-order shuffling. The judge sees options in a
hash-deterministic but generation-order-independent permutation, so
"first option" and "letter A" position bias don't pollute scores.
The shuffle is reversible — `unshuffle_judge_payload` restores the
canonical letters before the aggregator stores anything.

Stable across stages so the same template can be reused on imported
items (Phase 0 calibration) and on synthetic drafts (Phase 1+).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from services.synthetic.types import (
    CalibrationAnchor,
    RUBRIC_AXES,
    RUBRIC_BAND_ANCHORS,
)


JUDGE_SYSTEM = """You are a senior GRE psychometrician reviewing a single
practice item for inclusion in a high-stakes mock test bank (think
Manhattan Prep, Kaplan, Princeton Review — *not* official ETS).

You will score the item on a 6-axis rubric, each axis 1-5. Use the FULL
scale. The intended operating point is:
  5 = excellent for a high-stakes prep mock; ships as-is.
  4 = good; ships as-is in most banks, with very minor copy edits at most.
  3 = adequate but uneven; ships only after targeted revisions.
  2 = poor; would not ship without major rewrite.
  1 = unacceptable; would harm score validity.

Important calibration notes:
- Above-average published prep items (Manhattan, Kaplan, Princeton)
  routinely earn 4s on most axes and a 5 on at least one. Do NOT
  reserve 5 only for items that look identical to official ETS — that
  drives systematic under-scoring.
- A single mild stylistic awkwardness is a 4 on language_clarity, not
  a 3. Reserve 3 for ambiguity that a careful reader has to resolve.
- For numeric_entry and other subtypes that have no distractors, score
  distractor_quality = 5 (vacuously satisfied) — do not penalise the
  absence of distractors.
- Score ONLY the item shown — do not assume context that isn't there.
- The item passed an answer-key sanity check before reaching you;
  treat the marked correct answer as authoritative when reasoning.
- VERY SHORT justification per axis (<= 15 words). No bullets, no
  headers, no markdown. Plain ASCII only inside JSON strings.

Output STRICT JSON ONLY (no prose, no code fences, no trailing comma).
Schema:
{
  "scores": {
    "<axis>": {"score": <int 1-5>, "justification": "<= 15 words"},
    ...one entry per axis...
  }
}
"""


def _render_band_anchor_block(axis_descriptions: Dict[str, str]) -> str:
    """Render the per-axis behavioural-anchor block.

    Each axis gets its short definition plus the 1/3/5 descriptors so
    the judge sees concrete language for the low, middle, and high
    bands inline with the axis name.
    """
    lines: List[str] = []
    for axis in RUBRIC_AXES:
        lines.append(f"- {axis}: {axis_descriptions.get(axis, '')}")
        anchors = RUBRIC_BAND_ANCHORS.get(axis, {})
        # Render 5/3/1 in that order (high → low) so the anchor that
        # licenses the *full* scale appears first.
        for band in (5, 3, 1):
            descriptor = anchors.get(band)
            if descriptor:
                lines.append(f"    [{band}] {descriptor}")
    return "\n".join(lines)


def _render_calibration_block(anchors: List[CalibrationAnchor]) -> str:
    """Render the calibration anchor block for the user prompt.

    Each anchor is shown as item-payload + expected scores. Marked as
    reference-only so the judge does not score them.
    """
    if not anchors:
        return ""
    chunks: List[str] = [
        "CALIBRATION ANCHORS (do NOT score these — they are reference",
        "points only; use them to anchor your sense of each band):",
        "",
    ]
    for a in anchors:
        chunks.append(f"[{a.label}] {a.description}")
        chunks.append("```json")
        chunks.append(json.dumps(a.item, indent=2, ensure_ascii=False))
        chunks.append("```")
        score_pairs = ", ".join(
            f"{axis}={a.expected_scores.get(axis, '?')}"
            for axis in RUBRIC_AXES
        )
        chunks.append(f"Expected scores: {score_pairs}")
        if a.rationale:
            chunks.append(f"Why: {a.rationale}")
        chunks.append("")
    chunks.append("END CALIBRATION ANCHORS — now score the item below.")
    return "\n".join(chunks)


def build_judge_prompt(
    item_payload: Dict[str, Any],
    axis_descriptions: Dict[str, str],
    calibration_anchors: Optional[List[CalibrationAnchor]] = None,
    *,
    shuffle_options: bool = False,
    shuffle_seed: Optional[str] = None,
) -> Dict[str, str]:
    """Return {"system": str, "user": str} ready to hand to an LLM client.

    `item_payload` must contain at least {stem, options, correct_label,
    explanation, subtype, claimed_difficulty}. Extras (subtopic, vocab
    tier, etc.) are passed through verbatim and may bias the judge's
    construct_alignment / difficulty_plausibility scoring — that's
    intentional.

    `calibration_anchors` is the list returned by
    `load_calibration_anchors()`. Pass an empty list to disable the
    anchor block (mostly for unit tests that want a small prompt).

    `shuffle_options`: when True, MCQ-style options are shuffled
    deterministically based on `shuffle_seed` (or the stem hash if
    not given) before being shown to the judge. The `correct_label`
    in the prompt is also remapped so the judge sees the new letter
    that points at the correct option — but the *score axes* don't
    care about letter identity, so the shuffle has no semantic effect
    beyond defeating position bias. Caller never needs to unshuffle.
    """
    if shuffle_options:
        item_payload = shuffle_payload_options(
            item_payload, seed=shuffle_seed,
        )
    rubric_block = _render_band_anchor_block(axis_descriptions)
    item_block = json.dumps(item_payload, indent=2, ensure_ascii=False)
    calibration_block = _render_calibration_block(calibration_anchors or [])

    user_parts: List[str] = [
        "RUBRIC AXES (with 1/3/5 behavioural anchors):",
        rubric_block,
        "",
    ]
    if calibration_block:
        user_parts.extend([calibration_block, ""])
    user_parts.extend([
        "ITEM TO SCORE:",
        f"```json\n{item_block}\n```",
        "",
        "Return JSON in the schema described in the system prompt. "
        "Every axis listed above MUST appear under `scores`.",
    ])
    return {"system": JUDGE_SYSTEM, "user": "\n".join(user_parts)}


def shuffle_payload_options(
    item_payload: Dict[str, Any],
    *,
    seed: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a shallow copy of `item_payload` with options reordered
    deterministically and `correct_label` remapped accordingly.

    Position bias mitigation per refinement plan §8. The shuffle is
    deterministic per stem so the same item gets the same shuffled
    presentation across re-runs (debuggable). Non-MCQ payloads
    (numeric_entry, etc.) are returned unchanged.

    Important: this MUTATES neither the input dict nor its `options`
    list. The caller can safely log both versions side-by-side.
    """
    options = item_payload.get("options") or []
    if not options:
        return dict(item_payload)
    # Build a deterministic permutation by sorting on a per-option
    # stable hash. Using the stem as the seed makes the shuffle stable
    # across runs of the same draft.
    seed_str = seed or item_payload.get("stem", "") or ""
    seed_bytes = seed_str.encode("utf-8")

    def _key(opt: Dict[str, Any], i: int) -> str:
        h = hashlib.sha256(seed_bytes + str(i).encode() + (opt.get("text") or "").encode("utf-8"))
        return h.hexdigest()

    indexed = list(enumerate(options))
    indexed.sort(key=lambda pair: _key(pair[1], pair[0]))
    new_letters = [chr(ord("A") + i) for i in range(len(indexed))]
    shuffled_options: List[Dict[str, Any]] = []
    new_correct_label = item_payload.get("correct_label", "")
    old_label_to_new: Dict[str, str] = {}
    for new_idx, (old_idx, opt) in enumerate(indexed):
        new_label = new_letters[new_idx]
        old_label = opt.get("label", chr(ord("A") + old_idx))
        old_label_to_new[old_label] = new_label
        shuffled_options.append({
            **opt,
            "label": new_label,
        })
    if new_correct_label and new_correct_label in old_label_to_new:
        new_correct_label = old_label_to_new[new_correct_label]
    out = dict(item_payload)
    out["options"] = shuffled_options
    out["correct_label"] = new_correct_label
    return out
