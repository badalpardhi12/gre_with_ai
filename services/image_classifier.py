"""Image classifier for Princeton (and future) EPUB-extracted GRE questions.

Every image in an extracted question must be assigned to one of these
buckets so the renderer knows what to do with it:

    inline_math   — fraction glyphs, radicals, small operator/expression
                     GIFs. Vision-render to LaTeX + plain text and
                     substitute inline. **Image ref dropped.**
    answer_table  — TC / SE answer-choice tables (the publisher renders
                     them as one GIF per question). Vision-render to a
                     structured option list + fallback HTML table.
                     **Image ref dropped.**
    numeric_box   — the publisher's numeric-entry input glyph. Already
                     captured deterministically as ``has_numeric_box``;
                     listed here for completeness.
    bullet        — radio / checkbox glyph next to each MCQ option.
                     Stripped at parse time.
    diagram       — geometry figures, number lines, function-plot
                     diagrams that belong in the stem. **Kept.**
    chart         — DI bar / pie / line charts, multi-row data tables,
                     stimulus assets that get shown to the user. **Kept.**

The classifier is deterministic-first. Filename patterns and (where
available) image dimensions classify the long tail (~95% of refs in
Princeton). For the rest, a Sonnet 4.6 vision call produces the bucket
label; on borderline cases an Opus 4.7 cross-check runs and a 3-model
jury (Gemini 3.1 Pro) breaks ties.

All results are content-hash cached at ``data/extracted/princeton/
image_classification_cache.json`` so re-runs are cheap.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import Any, Callable, Dict, Optional

# Allow import of the local-only LLM gateway that lives in the main
# checkout. Mirrors :mod:`services.extraction_verification`.
_MAIN_REPO = "/Users/chiku/Documents/side_projects/gre_with_ai"
if _MAIN_REPO not in sys.path:
    sys.path.append(_MAIN_REPO)


# Bucket vocabulary -----------------------------------------------------

BUCKET_INLINE_MATH = "inline_math"
BUCKET_ANSWER_TABLE = "answer_table"
BUCKET_NUMERIC_BOX = "numeric_box"
BUCKET_BULLET = "bullet"
BUCKET_DIAGRAM = "diagram"
BUCKET_CHART = "chart"
BUCKET_UNKNOWN = "unknown"

ALL_BUCKETS = (
    BUCKET_INLINE_MATH, BUCKET_ANSWER_TABLE, BUCKET_NUMERIC_BOX,
    BUCKET_BULLET, BUCKET_DIAGRAM, BUCKET_CHART, BUCKET_UNKNOWN,
)


# Deterministic signals -------------------------------------------------

# Princeton bullet glyphs (radio + checkbox). Stripped before classifier
# ever runs, but listed for completeness and so a stray reference still
# routes correctly.
_BULLET_FILES = {
    "Revi_9780307945396_epub_420_r1.jpg",
    "Revi_9780307945396_epub_421_r1.jpg",
}

# Numeric-entry box glyphs.
_NUMERIC_BOX_FILES = {
    "Revi_9780307945396_epub_419_r1.jpg",
    "Revi_9780307945396_epub_412_r1.jpg",
    # File 351 is an empty-box numeric entry that gets emitted as a
    # figure_ref because it lives outside the obvious <p class="img_hang">
    # context — it's universally a 78×26 blank rectangle, never a chart.
    "Revi_9780307945396_epub_351_r1.jpg",
    # File 112 is the stacked numerator/denominator entry box (108x60).
    # Same story — without the rule, it leaks into figure_refs and the
    # extractor types the question as mcq_single with zero options.
    "Revi_9780307945396_epub_112_r1.jpg",
}

# Filename-encoded fraction GIFs (Revi_..._epub_frac<num>-<den>_r1.gif).
_FRAC_FILE_RE = re.compile(r"Revi_\d+_epub_frac.+?_r1\.gif$", re.I)

# TC / SE answer-choice table GIFs (Revi_..._fi\d+_r1.gif).
_ANSWER_TABLE_FILE_RE = re.compile(r"Revi_\d+_fi\d+_r1\.gif$", re.I)

# Plain inline GIFs (Revi_..._epub_<digits>_r1.gif and the L-prefixed
# letter glyphs e.g. L09 / L11). Once filtered against bullets / fraction
# / answer-table, these are always math/operator artwork.
_INLINE_GIF_RE = re.compile(r"^Revi_\d+_epub_(?:L)?\d+_r1\.gif$", re.I)


# Inline-math size threshold (Princeton's biggest inline radical/operator
# glyph in the corpus is ~118×39; biggest fraction is ~36×40; smallest
# real chart is ~378×370 in the corpus). 150×100 gives plenty of headroom
# without ever capturing a real chart/diagram.
_INLINE_MAX_W = 150
_INLINE_MAX_H = 100


def _parse_dim(v):
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def deterministic_classify(filename: str,
                           width=None, height=None,
                           html_class: Optional[str] = None,
                           context: Optional[str] = None) -> Optional[str]:
    """Classify an image purely by filename + dimensions + HTML hints.

    Returns one of :data:`ALL_BUCKETS` or ``None`` if no deterministic
    rule matched (caller should fall back to vision).
    """
    if not filename:
        return None
    bare = filename.rsplit("/", 1)[-1]

    # Hard filename matches first.
    if bare in _BULLET_FILES:
        return BUCKET_BULLET
    if bare in _NUMERIC_BOX_FILES:
        return BUCKET_NUMERIC_BOX
    if _FRAC_FILE_RE.match(bare):
        return BUCKET_INLINE_MATH
    if _ANSWER_TABLE_FILE_RE.match(bare):
        return BUCKET_ANSWER_TABLE

    w = _parse_dim(width)
    h = _parse_dim(height)
    is_inline_html = (html_class or "").lower().strip() == "inline"

    # Inline GIF naming convention + small dims OR explicit class="inline".
    if _INLINE_GIF_RE.match(bare):
        if is_inline_html:
            return BUCKET_INLINE_MATH
        if w is not None and h is not None:
            if 0 < w <= _INLINE_MAX_W and 0 < h <= _INLINE_MAX_H:
                return BUCKET_INLINE_MATH
            # GIFs with the publisher's number-only naming, larger than
            # the inline budget: real diagrams (rare; e.g. number lines).
            return BUCKET_DIAGRAM

    # Big JPGs are charts/diagrams (Princeton renders DI bar/pie/line
    # charts and geometry figures as JPGs). Distinguishing chart vs
    # diagram needs vision.
    if bare.lower().endswith((".jpg", ".jpeg")):
        if w is not None and h is not None:
            if w >= 200 or h >= 200:
                # Diagram or chart — defer to vision unless the context
                # screams DI (then chart).
                if context and "data interp" in context.lower():
                    return BUCKET_CHART
                return None
            if w <= _INLINE_MAX_W and h <= _INLINE_MAX_H:
                return BUCKET_INLINE_MATH

    return None


# Cache -----------------------------------------------------------------


_CACHE_PATH_DEFAULT = os.path.join(
    "/Users/chiku/Documents/side_projects/gre_with_ai/data/extracted/princeton",
    "image_classification_cache.json",
)


class _Cache:
    """Content-hash keyed JSON cache for classifier verdicts.

    Cache keys: ``sha256(image_bytes)``. Values: dict with at least
    ``bucket`` and ``source`` ("deterministic" | "sonnet" | "opus"
    | "jury").
    """

    def __init__(self, path: str = _CACHE_PATH_DEFAULT):
        self.path = path
        self._data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def get(self, image_bytes: bytes) -> Optional[Dict[str, Any]]:
        if image_bytes is None:
            return None
        h = hashlib.sha256(image_bytes).hexdigest()
        return self._data.get(h)

    def put(self, image_bytes: bytes, verdict: Dict[str, Any]):
        if image_bytes is None:
            return
        h = hashlib.sha256(image_bytes).hexdigest()
        self._data[h] = verdict
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)


_CLASSIFY_SYSTEM = """You are classifying images extracted from a GRE prep ebook.

Bucket each image into EXACTLY ONE of these labels:

  inline_math  — a small image showing math typeset that belongs INSIDE a
                  sentence: a fraction (e.g. 1/2), a radical (e.g. sqrt(2)),
                  an exponent or a single-symbol formula or a tiny operator
                  definition (e.g. "a $ b = a + b - 1"). These are NEVER
                  full diagrams or charts.

  answer_table — a single image showing the answer-choice GRID for a Text
                  Completion or Sentence Equivalence question: a small
                  table whose columns are "Blank (i)", "Blank (ii)", etc.
                  and whose rows are the candidate words. Always rectangular,
                  always tabular text, NEVER a chart with bars/axes.

  numeric_box  — an empty rectangular numeric-entry input glyph (a thin
                  hollow box). No text inside.

  bullet       — a radio button or checkbox glyph (filled or empty
                  circle) used to mark MCQ options. Tiny.

  diagram      — a geometry figure (triangle, circle, polygon, intersecting
                  lines), a function-graph plot, a number line, or any
                  visual stem the question refers to. NOT a chart.

  chart        — a Data Interpretation chart: bar / pie / line graph or a
                  multi-row data table that students must read values from
                  to answer follow-up questions.

Output ONLY a JSON object with this shape:

  {"bucket": "inline_math", "confidence": "high"|"medium"|"low", "reason": "<short>"}

No markdown fences. No commentary. JSON only."""


def _import_gateway():
    try:
        from services import _llm_gateway as gw
        return gw
    except Exception:
        pass
    candidates = [os.path.join(_MAIN_REPO, "services")]
    for base in candidates:
        if base not in sys.path:
            sys.path.append(base)
    from services import _llm_gateway as gw  # type: ignore
    return gw


def _media_type(filename: str) -> str:
    if not filename:
        return "image/png"
    f = filename.lower()
    if f.endswith(".gif"):
        return "image/gif"
    if f.endswith(".jpg") or f.endswith(".jpeg"):
        return "image/jpeg"
    if f.endswith(".png"):
        return "image/png"
    return "image/png"


def _classify_with_anthropic(image_bytes: bytes, filename: str,
                             context: Optional[str], model: str,
                             client) -> Dict[str, Any]:
    gw = _import_gateway()
    block = gw.FloodgateClient.encode_image_for_anthropic(
        image_bytes, media_type=_media_type(filename))
    prompt = (
        "Classify the image above. Filename: "
        + (filename or "<unknown>")
        + ".\nContext: " + (context or "<none>")
    )
    raw = client.call_anthropic(
        model=model,
        system=_CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": [
            block, {"type": "text", "text": prompt}]}],
        max_tokens=200,
    )
    return _parse_classify_json(raw)


def _parse_classify_json(raw_text: str) -> Dict[str, Any]:
    """Tolerant parser for ``{"bucket": "...", ...}`` blobs."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        lines = [ln for ln in text.split("\n")
                 if not ln.strip().startswith("```")]
        text = "\n".join(lines)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"bucket": BUCKET_UNKNOWN, "confidence": "low",
                "parse_error": "no_json", "raw": text[:200]}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return {"bucket": BUCKET_UNKNOWN, "confidence": "low",
                "parse_error": str(e), "raw": text[:200]}
    bucket = str(obj.get("bucket", "")).strip().lower()
    if bucket not in ALL_BUCKETS:
        return {"bucket": BUCKET_UNKNOWN, "confidence": "low",
                "parse_error": "unknown_bucket:" + bucket,
                "raw": text[:200]}
    return {
        "bucket": bucket,
        "confidence": str(obj.get("confidence", "medium")).lower(),
        "reason": obj.get("reason", "")[:200] if isinstance(obj.get("reason"), str) else "",
    }


def classify_image(
    *,
    filename: str,
    image_bytes: Optional[bytes] = None,
    width=None, height=None,
    html_class: Optional[str] = None,
    context: Optional[str] = None,
    cache: Optional[_Cache] = None,
    client=None,
    enable_vision: bool = True,
    enable_jury: bool = False,
) -> Dict[str, Any]:
    """Classify a single image into one of :data:`ALL_BUCKETS`.

    Returns a verdict dict::

        {
          "bucket": "<one of ALL_BUCKETS>",
          "source": "deterministic"|"sonnet"|"opus"|"jury",
          "confidence": "high"|"medium"|"low",
          "filename": "...",
          "reason": "...",       # only for vision verdicts
          "votes": {...},        # only for jury verdicts
        }
    """
    # 1. Deterministic.
    det = deterministic_classify(
        filename, width=width, height=height,
        html_class=html_class, context=context,
    )
    if det is not None:
        return {"bucket": det, "source": "deterministic",
                "confidence": "high", "filename": filename}

    # 2. Cached vision verdict (keyed on bytes).
    cached = cache.get(image_bytes) if cache and image_bytes else None
    if cached:
        cached.setdefault("filename", filename)
        return cached

    # 3. Vision via Sonnet.
    if not enable_vision:
        return {"bucket": BUCKET_UNKNOWN, "source": "deterministic",
                "confidence": "low", "filename": filename}
    if image_bytes is None:
        return {"bucket": BUCKET_UNKNOWN, "source": "no_bytes",
                "confidence": "low", "filename": filename}
    gw = _import_gateway()
    if client is None:
        client = gw.FloodgateClient()
    sonnet_verdict = _classify_with_anthropic(
        image_bytes, filename, context, gw.MODEL_SONNET, client)
    sonnet_bucket = sonnet_verdict.get("bucket")
    sonnet_conf = sonnet_verdict.get("confidence", "medium")
    verdict = {
        "bucket": sonnet_bucket,
        "source": "sonnet",
        "confidence": sonnet_conf,
        "filename": filename,
        "reason": sonnet_verdict.get("reason", ""),
    }

    # 4. Cross-validate borderline cases with Opus 4.7.
    if sonnet_conf in ("medium", "low") and sonnet_bucket != BUCKET_UNKNOWN:
        opus_verdict = _classify_with_anthropic(
            image_bytes, filename, context, gw.MODEL_OPUS, client)
        if opus_verdict.get("bucket") != sonnet_bucket:
            # 5. Disagreement → 3-model jury (optional, defaults to off).
            votes = {"sonnet": sonnet_bucket,
                     "opus": opus_verdict.get("bucket")}
            if enable_jury:
                try:
                    gemini_verdict = _classify_with_gemini(
                        image_bytes, filename, context, gw.MODEL_GEMINI_PRO,
                        client)
                    votes["gemini"] = gemini_verdict.get("bucket")
                except Exception:
                    votes["gemini"] = None
            # Majority wins; ties keep Opus (more conservative on small
            # samples).
            from collections import Counter
            tally = Counter(v for v in votes.values() if v in ALL_BUCKETS
                            and v != BUCKET_UNKNOWN)
            if tally:
                top, n = tally.most_common(1)[0]
                if list(tally.values()).count(n) == 1:
                    final_bucket = top
                else:
                    final_bucket = opus_verdict.get("bucket") or sonnet_bucket
            else:
                final_bucket = sonnet_bucket
            verdict = {
                "bucket": final_bucket,
                "source": "jury" if enable_jury else "opus",
                "confidence": "medium",
                "filename": filename,
                "reason": "models disagreed; voted",
                "votes": votes,
            }
        else:
            verdict["source"] = "opus_confirmed"
            verdict["confidence"] = "high"

    if cache is not None:
        cache.put(image_bytes, verdict)
    return verdict


def _classify_with_gemini(image_bytes, filename, context, model, client):
    gw = _import_gateway()
    part = gw.FloodgateClient.encode_image_for_gemini(
        image_bytes, mime_type=_media_type(filename))
    prompt = (
        "Classify the image above. Filename: "
        + (filename or "<unknown>")
        + ".\nContext: " + (context or "<none>")
    )
    raw = client.call_gemini(
        model=model,
        contents=[{"role": "user", "parts": [part, {"text": prompt}]}],
        system_instruction=_CLASSIFY_SYSTEM,
        max_output_tokens=200,
    )
    return _parse_classify_json(raw)


def get_cache(path: str = _CACHE_PATH_DEFAULT) -> _Cache:
    return _Cache(path)
