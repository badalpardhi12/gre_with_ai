"""Image-bucket classifier for the Kaplan EPUB extractor.

Mandate (matching Princeton's parallel work): every image attached to a
parsed item must land in one of these buckets, and only ``diagram`` /
``chart`` images are kept as ``figure_image``. Everything else is either
dropped (``numeric_box`` / ``bullet``) or vision-rendered to text and the
image reference is dropped from the rendered output.

Buckets
-------

inline_math   small sentence-level math glyph (fraction, radical, exponent
               artwork). Vision pass produces LaTeX; the ``<img>`` is
               replaced with ``\\(...\\)`` text in the prompt/explanation.
answer_table  the publisher's image-of-an-answer-grid (TC/SE multi-blank
               or any short-answer choice list rendered as a JPEG).
               Vision pass produces an option list; the ``<img>`` is
               dropped from the prompt and the structured options are
               attached to the item.
numeric_box   the empty input glyph used for numeric-entry questions.
               Always dropped from the visual output (the wxPython runtime
               draws its own NumericEntry widget).
bullet        the A/B/C/D oval glyphs that mark MCQ option rows. Always
               stripped at parse time.
diagram       a geometry figure / number line / function plot — the
               actual visual stem. Kept as ``figure_image``.
chart         a Data Interpretation chart (bar / pie / line). Kept as
               ``figure_image`` (or as the cluster's ``figure_images`` for
               RC/DI groups).
quantity_expr a math expression standing as the entire content of a
               ``Quantity A`` / ``Quantity B`` cell in a QC question.
               Vision pass produces LaTeX; the cell is rewritten with
               ``\\(...\\)``.
unknown       deterministic + vision both failed; caller decides whether
               to keep the ``<img>`` reference or drop it.

The classifier first tries deterministic rules (filename / size). For
every miss, a Sonnet 4.6 vision call labels the bucket and returns a
LaTeX or option-table transcription where applicable. Verdicts are
content-hash cached at ``data/extracted/kaplan/image_bucket_cache.json``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, Optional


_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


BUCKET_INLINE_MATH = "inline_math"
BUCKET_ANSWER_TABLE = "answer_table"
BUCKET_NUMERIC_BOX = "numeric_box"
BUCKET_BULLET = "bullet"
BUCKET_DIAGRAM = "diagram"
BUCKET_CHART = "chart"
BUCKET_QUANTITY_EXPR = "quantity_expression"
BUCKET_UNKNOWN = "unknown"

ALL_BUCKETS = (
    BUCKET_INLINE_MATH, BUCKET_ANSWER_TABLE, BUCKET_NUMERIC_BOX,
    BUCKET_BULLET, BUCKET_DIAGRAM, BUCKET_CHART,
    BUCKET_QUANTITY_EXPR, BUCKET_UNKNOWN,
)

# Buckets we keep as visible figures.
FIGURE_BUCKETS = frozenset({BUCKET_DIAGRAM, BUCKET_CHART})

# Buckets that are silently dropped (the renderer/runtime handles them).
DROP_BUCKETS = frozenset({BUCKET_NUMERIC_BOX, BUCKET_BULLET})


# ── Deterministic rules ────────────────────────────────────────────────

# Kaplan's option-letter ovals: a.jpg .. f.jpg, plus the QC variants
# (37a.jpg .. 37e.jpg) and the SE/TC variants (s-a.jpg, ga.jpg, gb.jpg).
_OPTION_BULLET_RE = re.compile(
    r"^(?:s-|g)?[a-f]\.jpg$|^37[a-e]\.jpg$|^38[a-f]\.jpg$",
    re.I,
)
# Numeric-entry box glyph filename heuristics (Kaplan reuses a small set).
_NUMERIC_BOX_FILENAMES = {
    "370a.jpg",  # Hannah numeric entry box (defect (e))
}

# Inline-math size budget: anything ≤ 32 KB in OEBPS/images/ is structurally
# an inline glyph in the Kaplan EPUB (the publisher renders fractions /
# operators / quantity expressions as 5–25 KB JPEGs; real diagrams /
# charts start at 35 KB+).
_INLINE_MAX_BYTES = 32_000


def deterministic_classify(filename: str, size_bytes: Optional[int] = None,
                           context: Optional[str] = None) -> Optional[str]:
    """Pure filename + size classification.

    Returns one of :data:`ALL_BUCKETS` or ``None`` if no rule matched
    (caller should fall back to vision).
    """
    if not filename:
        return None
    bare = filename.rsplit("/", 1)[-1]
    if _OPTION_BULLET_RE.match(bare):
        return BUCKET_BULLET
    if bare in _NUMERIC_BOX_FILENAMES:
        return BUCKET_NUMERIC_BOX
    return None


# ── Cache ──────────────────────────────────────────────────────────────

_CACHE_PATH_DEFAULT = os.path.join(
    _PROJECT_ROOT, "data", "extracted", "kaplan",
    "image_bucket_cache.json",
)


class BucketCache:
    """Content-hash keyed JSON cache for classifier verdicts.

    Cache keys are ``sha256(bytes)``. Values are dicts with ``bucket``,
    ``source`` ("deterministic" | "sonnet" | "opus"), ``confidence``,
    optional ``transcription`` (LaTeX), and optional ``options`` (list of
    {label, text} dicts).
    """

    def __init__(self, path: str = _CACHE_PATH_DEFAULT):
        self.path = path
        self._data = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (ValueError, OSError):
                return {}
        return {}

    def get(self, image_bytes: Optional[bytes]) -> Optional[Dict[str, Any]]:
        if image_bytes is None:
            return None
        h = hashlib.sha256(image_bytes).hexdigest()
        return self._data.get(h)

    def put(self, image_bytes: Optional[bytes], verdict: Dict[str, Any]) -> None:
        if image_bytes is None:
            return
        h = hashlib.sha256(image_bytes).hexdigest()
        self._data[h] = verdict
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False, sort_keys=True)

    def all(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._data)


# ── Vision (Sonnet) ────────────────────────────────────────────────────

_CLASSIFY_SYSTEM = """You classify images extracted from a GRE prep ebook.

Bucket each image into EXACTLY ONE of these labels:

  inline_math         a small sentence-level math glyph (fraction, radical,
                       exponent artwork). NEVER a full diagram / chart.

  quantity_expression a small math expression standing as the entire content
                       of a "Quantity A" or "Quantity B" cell in a
                       Quantitative Comparison question (algebraic / numeric
                       expression that fills a table cell). Distinct from
                       inline_math because it stands alone as a cell value.

  answer_table        an image of an answer-choice GRID for a Text Completion
                       or Sentence Equivalence question (1-3 columns of word
                       choices labelled A/B/C, A/B/C plus D/E/F, etc.).
                       OR an image showing the multiple-choice answer list
                       (A through E) for a math short-answer question
                       rendered as a single JPEG. Always tabular text,
                       NEVER a chart with bars/axes.

  numeric_box         an empty rectangular numeric-entry input glyph (a
                       hollow box, optionally followed by a unit label like
                       "weeks"). Used as the "□" placeholder. Drop entirely.

  bullet              an A/B/C/D oval bubble used to mark MCQ option rows.
                       Tiny.

  diagram             a geometry figure (triangle, circle, polygon, intersecting
                       lines), function plot, number line, or any visual stem.

  chart               a Data Interpretation bar / pie / line chart or a
                       multi-row data table that students must read values
                       from to answer follow-up questions.

  unknown             cannot tell.

Output ONLY a JSON object with this shape:

  {
    "bucket": "<one of the buckets above>",
    "confidence": "high" | "medium" | "low",
    "reason": "<short explanation>",
    "transcription": "<LaTeX-style transcription if math/quantity, else empty>",
    "options": [{"label": "A", "text": "..."}, ...]   // only for answer_table
  }

No markdown fences. No commentary. JSON only."""


def _import_gateway():
    try:
        from services import _llm_gateway as gw
        return gw
    except Exception:
        candidates = [
            os.path.join(
                "/Users/chiku/Documents/side_projects/gre_with_ai", "services"
            ),
        ]
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


def _parse_classify_json(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = [ln for ln in text.split("\n")
                 if not ln.strip().startswith("```")]
        text = "\n".join(lines)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"bucket": BUCKET_UNKNOWN, "confidence": "low",
                "reason": "no_json", "raw": text[:200]}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return {"bucket": BUCKET_UNKNOWN, "confidence": "low",
                "reason": "parse_error:" + str(e), "raw": text[:200]}
    bucket = str(obj.get("bucket", "")).strip().lower()
    if bucket not in ALL_BUCKETS:
        return {"bucket": BUCKET_UNKNOWN, "confidence": "low",
                "reason": "unknown_bucket:" + bucket, "raw": text[:200]}
    return {
        "bucket": bucket,
        "confidence": str(obj.get("confidence", "medium")).lower(),
        "reason": (obj.get("reason") or "")[:300],
        "transcription": (obj.get("transcription") or "").strip(),
        "options": obj.get("options") or [],
    }


def _classify_with_anthropic(image_bytes: bytes, filename: str,
                             context: Optional[str], model: str,
                             client) -> Dict[str, Any]:
    gw = _import_gateway()
    block = gw.FloodgateClient.encode_image_for_anthropic(
        image_bytes, media_type=_media_type(filename))
    prompt = (
        "Classify the image above. Filename: " + (filename or "<unknown>")
        + ".\nContext: " + (context or "<none>")
    )
    raw = client.call_anthropic(
        model=model,
        system=_CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": [
            block, {"type": "text", "text": prompt}]}],
        max_tokens=400,
    )
    return _parse_classify_json(raw)


def classify_image(
    *,
    filename: str,
    image_bytes: Optional[bytes] = None,
    size_bytes: Optional[int] = None,
    context: Optional[str] = None,
    cache: Optional[BucketCache] = None,
    client=None,
    enable_vision: bool = True,
) -> Dict[str, Any]:
    """Return a bucket verdict for a single image.

    Verdict shape::
        {"bucket": "<one of ALL_BUCKETS>",
         "source": "deterministic" | "sonnet" | "opus_confirmed" | "no_bytes",
         "confidence": "high" | "medium" | "low",
         "filename": "...",
         "reason": "<why>",
         "transcription": "<LaTeX or empty>",
         "options": [{label, text}, ...]    # only for answer_table
        }
    """
    bare = (filename or "").rsplit("/", 1)[-1]
    det = deterministic_classify(bare, size_bytes=size_bytes, context=context)
    if det is not None:
        return {"bucket": det, "source": "deterministic",
                "confidence": "high", "filename": bare,
                "transcription": "", "options": [], "reason": "filename_rule"}

    cached = cache.get(image_bytes) if cache and image_bytes else None
    if cached:
        cached.setdefault("filename", bare)
        return cached

    if not enable_vision:
        return {"bucket": BUCKET_UNKNOWN, "source": "deterministic",
                "confidence": "low", "filename": bare,
                "transcription": "", "options": [], "reason": "vision_disabled"}
    if image_bytes is None:
        return {"bucket": BUCKET_UNKNOWN, "source": "no_bytes",
                "confidence": "low", "filename": bare,
                "transcription": "", "options": [], "reason": "no_bytes"}

    gw = _import_gateway()
    if client is None:
        client = gw.FloodgateClient()
    sonnet_verdict = _classify_with_anthropic(
        image_bytes, bare, context, gw.MODEL_SONNET, client)
    verdict = {
        "bucket": sonnet_verdict["bucket"],
        "source": "sonnet",
        "confidence": sonnet_verdict.get("confidence", "medium"),
        "filename": bare,
        "reason": sonnet_verdict.get("reason", ""),
        "transcription": sonnet_verdict.get("transcription", ""),
        "options": sonnet_verdict.get("options", []),
    }
    if cache is not None:
        cache.put(image_bytes, verdict)
    return verdict


def get_cache(path: str = _CACHE_PATH_DEFAULT) -> BucketCache:
    return BucketCache(path)
