"""LLM-based verification of extracted GRE questions.

Workflow:

1. Render a tightly cropped image of the source EPUB region around the
   question's anchor (publisher artwork + answer table + figures).
2. Send the image + the structured JSON of what we extracted to a vision
   model (Sonnet 4.6 by default) with a strict JSON schema asking
   "did we faithfully capture this question?".
3. The model returns one of:
     {"verified": true}
     {"verified": false, "defects": [...], "suggested_correction": {...}}
4. Caller decides whether to (a) auto-apply the suggested correction
   (cheap fixes like inline-fraction text) or (b) flag for human
   review (status = "draft", review_notes populated).

The module is publisher-agnostic; only the rendering helpers care about
EPUB layout. The :func:`verify_question` function takes a callable
``render_fn(question) -> bytes`` so it can be reused for Kaplan etc.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional

# Allow the worktree to import the local-only LLM gateway from the main
# checkout (it's gitignored there too, so the path is stable). We import
# lazily (inside :func:`verify_question`) so the module — and its tests —
# remain importable in environments without the gateway.
_MAIN_REPO = os.environ.get(
    "GRE_MAIN_REPO",
    "/Users/chiku/Documents/side_projects/gre_with_ai",
)
if _MAIN_REPO not in sys.path:
    sys.path.append(_MAIN_REPO)


# Verification verdict schema -------------------------------------------

# Defect tags the LLM may emit. We pin the vocabulary so reviewers can
# group failures and so :func:`apply_correction` knows what is safe to
# auto-fix vs. what must escalate to a human.
DEFECT_TAGS = (
    "missing_inline_math",        # e.g. fraction GIF lost as plain text
    "missing_superscript",        # x^2 rendered as x 2
    "missing_subscript",
    "wrong_option_text",          # OCR/HTML mismatch
    "wrong_option_count",
    "wrong_correct_label",
    "missing_figure",             # diagram referenced but not attached
    "missing_passage",            # RC/DI cluster missing stimulus
    "wrong_subtype",              # mcq_single labelled qc, etc.
    "stem_truncated",
    "stem_extra_text",            # marker/caption leaked into stem
    "other",                      # free-text in suggested_correction
)

# Defect tags that we never auto-apply. The LLM might be hallucinating
# correctness signals, so anything that flips an answer or restructures
# the question must go through a human.
NEVER_AUTO_APPLY = frozenset({
    "wrong_correct_label",
    "wrong_subtype",
    "wrong_option_count",
    "missing_passage",
})


VERIFY_SYSTEM_PROMPT = """You verify GRE question extractions against the publisher's source image.

You will receive:
  1. A cropped image of the source page rendered from the publisher's EPUB.
  2. A JSON object describing what our deterministic parser extracted from
     that page (stem, options, figure references, stimulus, correct answer).

Your task: decide whether the extracted JSON faithfully represents the question
shown in the image. Return ONLY a JSON object with one of two shapes:

  {"verified": true}

OR

  {
    "verified": false,
    "defects": ["missing_inline_math", "missing_superscript", ...],
    "suggested_correction": {
      "stem": "<corrected stem text>",         // omit if stem is fine
      "options": [                              // omit if options are fine
        {"label": "A", "text": "...", "is_correct": false},
        ...
      ],
      "correct_label": "C",                    // omit if unchanged
      "notes": "<free-text explanation>"
    }
  }

Defect-tag vocabulary (use only these strings):
  missing_inline_math, missing_superscript, missing_subscript,
  wrong_option_text, wrong_option_count, wrong_correct_label,
  missing_figure, missing_passage, wrong_subtype, stem_truncated,
  stem_extra_text, other

Rules:
  - Be strict about MATH and SUPERSCRIPTS: if the source shows ``a^2`` and
    the JSON shows ``a 2``, that is missing_superscript. If the source
    shows a fraction GIF and the JSON shows ``[img:...]`` placeholder,
    that is missing_inline_math.
  - Be lenient about cosmetic whitespace, em-dash vs hyphen, and curly
    vs straight quotes — those are NOT defects.
  - The correct answer label in the JSON comes from the publisher's
    answer key; do not flag it unless the option text itself is wrong.
  - For DI / RC clusters: the stimulus (chart or passage) may NOT be
    visible in your image — you can ignore the ``stimulus_text`` field
    when it is empty in the JSON.
  - Output JSON only. No markdown fences. No commentary."""


def _import_gateway():
    """Locate the local-only :mod:`_llm_gateway` module.

    The gateway lives in the main checkout (``/Users/chiku/.../services/``)
    rather than in any tracked worktree, so we walk both that path and the
    PYTHONPATH before giving up. Returns the imported module or raises.
    """
    try:
        from services import _llm_gateway as gw
        return gw
    except Exception:
        pass
    candidates = [
        os.path.join(_MAIN_REPO, "services"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ]
    for base in candidates:
        if base not in sys.path:
            sys.path.append(base)
    try:
        import _llm_gateway as gw  # type: ignore
        return gw
    except Exception:
        # Final attempt: import via package after path tweak.
        for base in (_MAIN_REPO,):
            if base not in sys.path:
                sys.path.append(base)
        from services import _llm_gateway as gw  # type: ignore
        return gw


def _make_default_client():
    gw = _import_gateway()
    return gw.FloodgateClient()


def _default_model_id() -> str:
    gw = _import_gateway()
    return gw.MODEL_SONNET


def _encode_image_for_anthropic(image_bytes: bytes,
                                media_type: str = "image/png") -> Dict[str, Any]:
    """Build an Anthropic image content block. Inlined so the verifier
    module is import-safe even when the local-only gateway isn't on the
    path (e.g. inside unit tests)."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
        },
    }


def build_user_message(question: Dict[str, Any], image_bytes: bytes,
                       media_type: str = "image/png") -> List[Dict[str, Any]]:
    """Build the Anthropic Messages API ``content`` for one verification call."""
    image_block = _encode_image_for_anthropic(image_bytes, media_type=media_type)
    # Both Princeton and Kaplan extractors land in this module; cover the
    # union of figure-tracking field names.
    has_figure = bool(
        question.get("figure_refs")
        or question.get("figure_image")
        or question.get("has_figure")
    )
    figure_targets = (
        question.get("figure_refs")
        or ([question.get("figure_image")] if question.get("figure_image") else [])
        or []
    )
    inline_targets = (
        question.get("inline_gif_targets")
        or question.get("inline_glyph_files")
        or []
    )
    summary = {
        "qst_id": question.get("qst_id") or question.get("source_ref"),
        "subtype": question.get("subtype"),
        "stem": question.get("prompt"),
        "options": [
            {"label": o.get("label"), "text": o.get("text"),
             "is_correct": bool(o.get("is_correct"))}
            for o in (question.get("options") or [])
        ],
        "correct_label": question.get("correct_label"),
        "stimulus_anchor": question.get("stimulus_anchor"),
        "has_figure": has_figure,
        "figure_targets": figure_targets,
        "inline_gif_targets": inline_targets,
    }
    return [
        image_block,
        {"type": "text", "text":
            "Verify this extraction. Source image is above. Extraction JSON:\n"
            + json.dumps(summary, ensure_ascii=False, indent=2)},
    ]


def _parse_verdict(raw_text: str) -> Dict[str, Any]:
    """Tolerant JSON parser — strips markdown fences and trailing prose.

    Falls back to a "verified=true" inference when the model wrote
    narrative prose like "The image matches the JSON" / "The extracted
    JSON correctly represents this" without producing JSON. We err on
    the side of trusting an explicit positive narrative over a JSON
    parse error (the alternative — counting it as FAIL with `defects:
    ['other']` — was producing false-positive verdicts that wasted
    reviewer attention).
    """
    text = (raw_text or "").strip()
    if text.startswith("```"):
        # strip first/last fence lines
        lines = text.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines)
    # Find candidate top-level JSON objects with a brace-balancing scan.
    candidates: List[str] = []
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
                        candidates.append(text[i:j + 1])
                        i = j + 1
                        break
            else:
                break  # unterminated brace; bail
        else:
            i += 1
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict) and (
                    "verified" in obj or "defects" in obj
                    or "verdict" in obj):
                return obj
        except json.JSONDecodeError:
            continue
    # Narrative-only response: classify based on positive vs negative
    # phrasing. Conservative: only PASS on an explicit positive signal.
    lower = text.lower()
    positive = (
        "correctly represents" in lower
        or "faithfully represents" in lower
        or "accurately represents" in lower
        or "the extracted json correctly" in lower
        or "the extraction is correct" in lower
        or "matches the source" in lower
        or "matches the image" in lower
    )
    negative = (
        "does not match" in lower
        or "incorrect" in lower
        or "wrong" in lower
        or "missing" in lower
        or "should be" in lower
    )
    if positive and not negative:
        return {"verified": True, "defects": [],
                "narrative_inference": True, "raw": text[:300]}
    return {"verified": False, "defects": ["other"],
            "parse_error": "no_json_or_unparseable",
            "raw": text[:300]}


VERIFY_TEXT_ONLY_SYSTEM_PROMPT = """You audit GRE question extractions for self-consistency.

You will receive ONLY the structured JSON our deterministic parser
produced (no source image). Decide whether the JSON is internally
coherent enough to ship as a live practice question.

Pass criteria:
  - The stem is a complete, well-formed GRE prompt (no truncation marker,
    no leftover heading/caption text bleeding in).
  - The options (when present) are plausible answer choices for the stem
    — each one is a distinct, complete option string.
  - For TC / SE: blank markers (i)/(ii) in the stem are matched by the
    blank-prefixed option labels.
  - For numeric_entry: there is a `correct_label` or `numeric_value`.
  - The correct_label points to a label that actually exists in the
    options array (when options are non-empty).
  - The explanation, if present, isn't obviously contradictory to the
    correct_label (you don't need to fully verify the math, just spot
    glaring mismatches).

Return ONLY a JSON object, one of:

  {"verified": true}

OR

  {"verified": false,
   "defects": [<one or more tag strings from the vocabulary below>],
   "suggested_correction": {"notes": "<short free-text>"}}

Defect-tag vocabulary:
  missing_inline_math, missing_superscript, missing_subscript,
  wrong_option_text, wrong_option_count, wrong_correct_label,
  missing_figure, missing_passage, wrong_subtype, stem_truncated,
  stem_extra_text, other

No markdown, no commentary, no explanation outside the JSON object."""


def verify_question_text_only(
    question: Dict[str, Any],
    *,
    client=None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """LLM self-consistency check that does not require a source image.

    Used when the source figure isn't available (most TC/SE/RC text-only
    items don't have an option-table glyph or diagram). Cheaper than the
    image path because no image tokens.
    """
    if client is None:
        client = _make_default_client()
        model = model or _default_model_id()
    if model is None:
        model = _default_model_id()
    summary = {
        "qst_id": question.get("qst_id") or question.get("source_ref"),
        "subtype": question.get("subtype"),
        "stem": question.get("prompt"),
        "options": [
            {"label": o.get("label"), "text": o.get("text"),
             "is_correct": bool(o.get("is_correct"))}
            for o in (question.get("options") or [])
        ],
        "correct_label": question.get("correct_label"),
        "numeric_value": question.get("numeric_value"),
        "explanation": (question.get("explanation") or "")[:1500],
    }
    raw = client.call_anthropic(
        model=model,
        system=VERIFY_TEXT_ONLY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content":
                   "Audit this extraction:\n"
                   + json.dumps(summary, ensure_ascii=False, indent=2)}],
        max_tokens=512,
    )
    verdict = _parse_verdict(raw)
    verdict.setdefault("verified", False)
    if verdict.get("verified") is True:
        verdict.setdefault("defects", [])
    else:
        verdict.setdefault("defects", ["other"])
    verdict["source"] = "text_only_check"
    verdict.setdefault("cost_estimate_usd", 0.003)
    return verdict


def verify_question(
    question: Dict[str, Any],
    render_fn: Callable[[Dict[str, Any]], Optional[bytes]],
    *,
    client=None,
    model: Optional[str] = None,
    media_type: str = "image/png",
    fallback_text_only: bool = True,
) -> Dict[str, Any]:
    """Run the LLM verification pass on one extracted question.

    Args:
      question: extracted question dict (must include ``qst_id``,
        ``subtype``, ``prompt``, ``options``, ``correct_label``).
      render_fn: callable that takes the question and returns image bytes
        of the source region — None if rendering failed.
      client: optional :class:`FloodgateClient`. If omitted, a default
        singleton is constructed.
      model: model id (defaults to Sonnet 4.6).
      media_type: MIME type of bytes returned by ``render_fn``.
      fallback_text_only: when ``render_fn`` returns None (no image),
        run :func:`verify_question_text_only` instead of returning a
        skipped verdict. Default True so coverage stays high.

    Returns:
      dict with keys::
        verified : bool
        defects  : list[str]                    (only if verified=False)
        suggested_correction : dict             (only if verified=False)
        skipped  : bool                         (True when no image and
                                                 fallback disabled)
        cost_estimate_usd : float (very rough)
    """
    image = render_fn(question)
    if image is None:
        if fallback_text_only:
            return verify_question_text_only(question, client=client, model=model)
        return {"verified": False, "skipped": True,
                "defects": ["other"],
                "skipped_reason": "no_image_rendered"}

    if client is None:
        # Lazy import — keeps the module testable without the gateway.
        client = _make_default_client()
        model = model or _default_model_id()
    if model is None:
        model = _default_model_id()

    content = build_user_message(question, image, media_type=media_type)
    raw = client.call_anthropic(
        model=model,
        system=VERIFY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        max_tokens=1024,
    )
    verdict = _parse_verdict(raw)
    verdict.setdefault("verified", False)
    if verdict.get("verified") is True:
        verdict.setdefault("defects", [])
    else:
        verdict.setdefault("defects", ["other"])
    # Very rough cost estimate: ~$0.005 per Sonnet vision call (image +
    # ~1k input + ~256 output tokens).
    verdict.setdefault("cost_estimate_usd", 0.005)
    return verdict


# Auto-apply -----------------------------------------------------------


def apply_correction(question: Dict[str, Any], verdict: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate *question* in place with the verdict's suggested correction.

    Only safe fields (stem text, option text) are auto-applied. Anything
    in :data:`NEVER_AUTO_APPLY` causes the question to be marked
    ``status='draft'`` with the verdict stashed in ``review_notes``.

    Returns the (mutated) question for chaining.
    """
    if verdict.get("verified"):
        question["verification_status"] = "verified"
        return question

    defects = set(verdict.get("defects") or [])
    correction = verdict.get("suggested_correction") or {}
    log = []

    if defects & NEVER_AUTO_APPLY:
        question["verification_status"] = "draft"
        question["review_notes"] = json.dumps(verdict, ensure_ascii=False)
        return question

    # Safe-to-apply: stem text rewrites
    if "stem" in correction and isinstance(correction["stem"], str):
        old = question.get("prompt")
        question["prompt"] = correction["stem"]
        log.append({"field": "prompt", "from": old,
                    "to": correction["stem"]})

    # Safe-to-apply: option text rewrites (must keep same labels & count)
    if "options" in correction and isinstance(correction["options"], list):
        new_opts = correction["options"]
        old_opts = question.get("options") or []
        if len(new_opts) != len(old_opts):
            # Length mismatch is risky — escalate to a human.
            question["verification_status"] = "draft"
            question["review_notes"] = json.dumps(verdict, ensure_ascii=False)
            return question
        same_labels = all(
            no.get("label") == oo.get("label")
            for no, oo in zip(new_opts, old_opts)
        )
        if same_labels:
            # Only rewrite the visible text, never is_correct.
            for no, oo in zip(new_opts, old_opts):
                if "text" in no and no["text"] != oo.get("text"):
                    log.append({"field": f"option_{oo['label']}",
                                "from": oo.get("text"), "to": no["text"]})
                    oo["text"] = no["text"]
        else:
            # Label mismatch is risky — escalate.
            question["verification_status"] = "draft"
            question["review_notes"] = json.dumps(verdict, ensure_ascii=False)
            return question

    if log:
        existing = question.get("correction_log") or []
        existing.extend(log)
        question["correction_log"] = existing
        question["verification_status"] = "auto_corrected"
    else:
        question["verification_status"] = "review_needed"
        question["review_notes"] = json.dumps(verdict, ensure_ascii=False)
    return question


# Convenience: bulk verifier with a budget ------------------------------


def verify_many(
    questions: List[Dict[str, Any]],
    render_fn: Callable[[Dict[str, Any]], Optional[bytes]],
    *,
    client=None,
    model: Optional[str] = None,
    apply: bool = True,
    budget_usd: float = 25.0,
    on_progress: Optional[Callable[[int, int, Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """Verify a list of questions and (optionally) auto-apply fixes.

    Stops early if the running cost estimate exceeds ``budget_usd``.

    Returns a list of verdict dicts, one per question (same order).
    """
    verdicts = []
    spent = 0.0
    for i, q in enumerate(questions):
        if spent >= budget_usd:
            verdicts.append({"verified": False, "skipped": True,
                             "skipped_reason": "budget_exhausted",
                             "defects": ["other"]})
            continue
        v = verify_question(q, render_fn, client=client, model=model)
        spent += v.get("cost_estimate_usd", 0.0)
        if apply:
            apply_correction(q, v)
        verdicts.append(v)
        if on_progress is not None:
            on_progress(i + 1, len(questions), v)
    return verdicts


# ── Figure-question alignment check ────────────────────────────────────

FIGURE_ALIGNMENT_SYSTEM_PROMPT = """You judge whether a figure (diagram / chart / graph)
plausibly belongs to a GRE question.

You receive:
  1. The image of the figure that the parser attached to this question.
  2. The question's stem text and (when relevant) options.

Decide one of three verdicts:
  - "matches": the figure is clearly the one referenced by the stem
    (e.g. stem says "in the diagram above" and the figure shows the
    geometry the stem talks about).
  - "mismatch": the figure clearly belongs to a different question
    (e.g. stem talks about a triangle but the figure is a bar chart).
  - "unsure": the figure is ambiguous or the stem doesn't strongly
    reference any specific figure.

Return ONLY a compact JSON object:

  {"verdict": "matches"|"mismatch"|"unsure",
   "rationale": "<1-2 sentence explanation>",
   "stem_references": ["the diagram", ...],   // phrases in the stem that mention a figure
   "figure_summary": "<one-sentence description of what the figure shows>"}

Be strict about geometric / numerical mismatches: if the stem says
"triangle" and the figure shows a circle, verdict="mismatch".
If the stem doesn't reference any visual element at all (no "the
chart", "the figure", "above", "below", "diagram"), verdict="unsure"
unless the figure clearly shows the same data the stem describes."""


def check_figure_alignment(
    question: Dict[str, Any],
    figure_image_bytes: bytes,
    *,
    client=None,
    model: Optional[str] = None,
    media_type: str = "image/jpeg",
) -> Dict[str, Any]:
    """LLM-judged check: does the attached figure match the stem?

    Designed for Kaplan's defect-(b) class of bugs where the
    figure-to-question linker pulls the wrong adjacent figure. Returns
    a verdict dict::

        {"verdict": "matches"|"mismatch"|"unsure",
         "rationale": "...",
         "stem_references": [...],
         "figure_summary": "...",
         "cost_estimate_usd": float}

    A `verdict="mismatch"` should route the item to `status='draft'` for
    human review (never auto-detach, since the LLM's "no" might itself
    be wrong).
    """
    if client is None:
        client = _make_default_client()
        model = model or _default_model_id()
    if model is None:
        model = _default_model_id()

    image_block = _encode_image_for_anthropic(
        figure_image_bytes, media_type=media_type,
    )
    summary = {
        "stem": question.get("prompt") or question.get("stem"),
        "subtype": question.get("subtype"),
        "options": [
            {"label": o.get("label"), "text": o.get("text")}
            for o in (question.get("options") or [])[:6]
        ],
    }
    content = [
        image_block,
        {"type": "text", "text":
            "Does this figure belong to the following GRE question? "
            "Question JSON:\n"
            + json.dumps(summary, ensure_ascii=False, indent=2)},
    ]
    raw = client.call_anthropic(
        model=model,
        system=FIGURE_ALIGNMENT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        max_tokens=512,
    )
    verdict = _parse_verdict(raw)
    if "verdict" not in verdict:
        verdict["verdict"] = "unsure"
    verdict.setdefault("rationale", "")
    verdict.setdefault("stem_references", [])
    verdict.setdefault("figure_summary", "")
    verdict.setdefault("cost_estimate_usd", 0.005)
    return verdict


# ── Multi-model agreement gate ─────────────────────────────────────────


def cross_check(
    question: Dict[str, Any],
    render_fn: Callable[[Dict[str, Any]], Optional[bytes]],
    *,
    client=None,
    primary_model: Optional[str] = None,
    secondary_model: Optional[str] = None,
    media_type: str = "image/png",
) -> Dict[str, Any]:
    """Run :func:`verify_question` with two models; return agreement-aware verdict.

    Both models must agree the extraction is verified for the result to
    promote. If they disagree, the verdict is `verified=False` with an
    `agreement: "disagree"` field so the caller can route to draft.

    Use this only on items the primary model already flagged or on a
    sampled subset — running every item through two models doubles the
    spend.
    """
    if client is None:
        client = _make_default_client()
    gw = _import_gateway()
    if primary_model is None:
        primary_model = gw.MODEL_SONNET
    if secondary_model is None:
        secondary_model = gw.MODEL_OPUS

    v1 = verify_question(question, render_fn, client=client,
                         model=primary_model, media_type=media_type)
    v2 = verify_question(question, render_fn, client=client,
                         model=secondary_model, media_type=media_type)
    agree = bool(v1.get("verified")) == bool(v2.get("verified"))
    return {
        "verified": bool(v1.get("verified") and v2.get("verified")),
        "agreement": "agree" if agree else "disagree",
        "primary": v1,
        "secondary": v2,
        "cost_estimate_usd": (v1.get("cost_estimate_usd", 0.0)
                              + v2.get("cost_estimate_usd", 0.0)),
    }
