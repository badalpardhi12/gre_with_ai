"""Vision-driven re-rendering of inline-math GIFs and TC/SE answer-table GIFs.

After :mod:`services.image_classifier` assigns each image to a bucket,
this module:

  * For ``inline_math``: asks Sonnet 4.6 to transcribe the image as
    LaTeX (and a plain-text fallback), then substitutes the text back
    into the question wherever the image is referenced (stem or option).
    The image reference is dropped.

  * For ``answer_table``: asks Sonnet 4.6 to read the publisher's TC /
    SE answer-choice grid and return structured JSON with one entry per
    blank. The result is normalised into the runtime renderer's option
    schema (multi-blank labels ``blank1_A`` / ``blank1_B`` / ...; single-
    blank labels ``A`` / ``B`` / ...; SE = single column, 6 rows). A
    fallback HTML table is returned for the markdown sample.

A small subset (≥10% of inline_math, ≥20% of answer_table) is
cross-validated against Opus 4.7 to flag transcription disagreements.
The caller chooses whether to escalate disagreements to human review.

Caching is content-hash keyed via :class:`services.image_classifier._Cache`
plus a parallel ``vision_render_cache.json`` to avoid re-paying the LLM
for the same image bytes across runs.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

_MAIN_REPO = "/Users/chiku/Documents/side_projects/gre_with_ai"
if _MAIN_REPO not in sys.path:
    sys.path.append(_MAIN_REPO)


# ── Cache ─────────────────────────────────────────────────────────────

_RENDER_CACHE_PATH_DEFAULT = os.path.join(
    "/Users/chiku/Documents/side_projects/gre_with_ai/data/extracted/princeton",
    "vision_render_cache.json",
)


class _RenderCache:
    """Two-level cache keyed on (kind, sha256(bytes))."""

    def __init__(self, path: str = _RENDER_CACHE_PATH_DEFAULT):
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

    def _key(self, kind: str, image_bytes: bytes) -> str:
        return kind + ":" + hashlib.sha256(image_bytes).hexdigest()

    def get(self, kind: str, image_bytes: Optional[bytes]) -> Optional[Dict[str, Any]]:
        if image_bytes is None:
            return None
        return self._data.get(self._key(kind, image_bytes))

    def put(self, kind: str, image_bytes: bytes, value: Dict[str, Any]):
        if image_bytes is None:
            return
        self._data[self._key(kind, image_bytes)] = value
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)


def get_render_cache(path: str = _RENDER_CACHE_PATH_DEFAULT) -> _RenderCache:
    return _RenderCache(path)


def _import_gateway():
    try:
        from services import _llm_gateway as gw
        return gw
    except Exception:
        pass
    cands = [os.path.join(_MAIN_REPO, "services")]
    for base in cands:
        if base not in sys.path:
            sys.path.append(base)
    from services import _llm_gateway as gw  # type: ignore
    return gw


def _media_type(filename: str) -> str:
    f = (filename or "").lower()
    if f.endswith(".gif"):
        return "image/gif"
    if f.endswith(".jpg") or f.endswith(".jpeg"):
        return "image/jpeg"
    if f.endswith(".png"):
        return "image/png"
    return "image/png"


# ── Inline-math vision render ────────────────────────────────────────

_INLINE_SYSTEM = """You transcribe inline math glyphs from a GRE prep ebook.

The image shows a SHORT mathematical expression (a fraction, a radical,
an exponent, a small operator definition like "a $ b = a + b - 1", or
similar). It belongs INSIDE a sentence — not as a standalone diagram.

Return ONLY a JSON object:

  {"latex": "<LaTeX form>", "plain": "<readable ASCII form>"}

Conventions:
  - fractions       \\frac{n}{d}     plain "(n/d)"
  - exponents       a^{b}            plain "a^b"
  - subscripts      a_{b}            plain "a_b"
  - radicals        \\sqrt{n}         plain "sqrt(n)"
  - radical w/ idx  \\sqrt[k]{n}      plain "k-root(n)"
  - operator def    a \\Box b = ...   plain "a [op] b = ..."
  - multiplication  use a single space, NOT \\cdot or *
  - keep variable names exactly as drawn (e.g. x, y, p, q)
  - do NOT add surrounding $ or \\(...\\) — the caller wraps as needed

If the image is anything OTHER than a small inline math expression
(e.g. a chart, a figure, an answer-choice table), return:

  {"latex": "", "plain": "", "error": "not_inline_math"}

No markdown fences. No commentary. JSON only."""


_OPUS_INLINE_SYSTEM = _INLINE_SYSTEM  # same prompt; second jury


def _latex_balanced(s: str) -> bool:
    if not s:
        return True
    if s.count("{") != s.count("}"):
        return False
    if s.count("\\(") != s.count("\\)"):
        return False
    if s.count("\\[") != s.count("\\]"):
        return False
    return True


def _strip_outer_math(s: str) -> str:
    """LLM occasionally wraps with $...$ or \\(...\\); strip a single layer."""
    s = (s or "").strip()
    if len(s) >= 2 and s.startswith("$") and s.endswith("$"):
        return s[1:-1].strip()
    if s.startswith("\\(") and s.endswith("\\)"):
        return s[2:-2].strip()
    if s.startswith("\\[") and s.endswith("\\]"):
        return s[2:-2].strip()
    return s


def _parse_render_json(raw_text: str) -> Dict[str, Any]:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = "\n".join(ln for ln in text.split("\n")
                         if not ln.strip().startswith("```"))
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"error": "no_json", "raw": text[:200]}
    blob = m.group(0)

    def _looks_corrupted(d):
        """Detect form-feed/backspace corruption from unescaped LaTeX
        backslashes. Real prompts never include literal control chars."""
        for v in d.values() if isinstance(d, dict) else ():
            if isinstance(v, str) and re.search(r"[\x00-\x08\x0b-\x1f]", v):
                return True
        return False

    try:
        out = json.loads(blob)
        if isinstance(out, dict) and _looks_corrupted(out):
            raise json.JSONDecodeError("control_chars", blob, 0)
        return out
    except json.JSONDecodeError:
        # The LLM frequently emits LaTeX with single backslashes (e.g.
        # ``"latex": "\frac{1}{2}"``) which strict JSON either rejects
        # outright or — worse — silently parses ``\f`` as form-feed,
        # ``\b`` as backspace, ``\t`` as tab. Two tolerant fallbacks:
        #   1. Escape any backslash that doesn't form one of the JSON
        #      escapes we actually want to preserve. We exclude ``\f``,
        #      ``\b``, ``\t`` from the allowlist because LaTeX uses
        #      ``\frac``, ``\beta``, ``\theta``, etc.
        #   2. If that still fails, scrape ``"key": "value"`` pairs.
        try:
            fixed = re.sub(r'\\(?!["\\/nru])', r'\\\\', blob)
            return json.loads(fixed)
        except json.JSONDecodeError as e:
            scrape = _scrape_json_pairs(blob)
            if scrape:
                return scrape
            return {"error": "json_parse:" + str(e), "raw": blob[:200]}


def _scrape_json_pairs(blob: str) -> Dict[str, Any]:
    """Last-resort regex scrape: look for ``"key": "value"`` pairs.

    Returns a dict of any string-typed keys we can recover. Skips
    arrays / nested objects (the inline_math + answer_table prompts only
    return a flat object so this is enough for that path; for the table
    schema we already get clean JSON in the common case)."""
    pairs = re.findall(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)"', blob)
    if not pairs:
        return {}
    out = {}
    for k, v in pairs:
        # Un-escape only the JSON-legal escapes; leave LaTeX backslashes alone.
        unescaped = (v.replace('\\"', '"').replace("\\\\", "\\")
                     .replace("\\n", "\n").replace("\\t", "\t"))
        out[k] = unescaped
    return out


def render_inline_math(
    image_bytes: bytes,
    filename: str,
    *,
    client=None,
    cache: Optional[_RenderCache] = None,
    cross_check_with_opus: bool = False,
) -> Dict[str, Any]:
    """Vision-render an inline-math image to LaTeX + plain text.

    Returns ``{latex, plain, source, agree?, opus?}`` or
    ``{error: ...}`` on failure.
    """
    if cache is not None:
        hit = cache.get("inline_math", image_bytes)
        if hit is not None:
            return hit

    gw = _import_gateway()
    if client is None:
        client = gw.FloodgateClient()

    block = gw.FloodgateClient.encode_image_for_anthropic(
        image_bytes, media_type=_media_type(filename))
    raw = client.call_anthropic(
        model=gw.MODEL_SONNET,
        system=_INLINE_SYSTEM,
        messages=[{"role": "user", "content": [
            block, {"type": "text", "text":
                    "Transcribe. Filename: " + (filename or "<unknown>")}]}],
        max_tokens=200,
    )
    parsed = _parse_render_json(raw)
    if "error" in parsed:
        out = {"error": parsed["error"], "filename": filename,
               "source": "sonnet"}
        if cache is not None:
            cache.put("inline_math", image_bytes, out)
        return out
    latex = _strip_outer_math(parsed.get("latex", ""))
    plain = (parsed.get("plain", "") or "").strip()
    if not _latex_balanced(latex):
        # Surface as parseable failure so the caller can route to human.
        out = {"error": "latex_unbalanced", "latex": latex, "plain": plain,
               "filename": filename, "source": "sonnet"}
        if cache is not None:
            cache.put("inline_math", image_bytes, out)
        return out

    out = {"latex": latex, "plain": plain, "filename": filename,
           "source": "sonnet"}

    if cross_check_with_opus:
        raw2 = client.call_anthropic(
            model=gw.MODEL_OPUS,
            system=_OPUS_INLINE_SYSTEM,
            messages=[{"role": "user", "content": [
                block, {"type": "text", "text":
                        "Transcribe. Filename: " + (filename or "<unknown>")}]}],
            max_tokens=200,
        )
        parsed2 = _parse_render_json(raw2)
        opus_latex = _strip_outer_math(parsed2.get("latex", "")) if "error" not in parsed2 else ""
        opus_plain = (parsed2.get("plain", "") or "").strip() if "error" not in parsed2 else ""
        agree = (latex == opus_latex) or (
            opus_latex and _normalise_math(latex) == _normalise_math(opus_latex))
        out["opus_latex"] = opus_latex
        out["opus_plain"] = opus_plain
        out["agree"] = bool(agree)

    if cache is not None:
        cache.put("inline_math", image_bytes, out)
    return out


def _normalise_math(s: str) -> str:
    """Loose match: collapse whitespace + brace style differences."""
    if not s:
        return ""
    s = s.strip().replace(" ", "")
    s = re.sub(r"\\,", "", s)
    return s


# ── Inline-math substitution into question text ──────────────────────


def substitute_inline_math(
    question: Dict[str, Any],
    *,
    rendered_by_filename: Dict[str, Dict[str, Any]],
    use_form: str = "plain",  # "plain" | "latex"
) -> Dict[str, Any]:
    """Replace ``[img:filename]`` placeholders in stem/options with the
    rendered text from :func:`render_inline_math`.

    Mutates the question dict in place and returns it. ``rendered_by_filename``
    maps the bare filename to a render dict (plain or latex form). Any
    ``inline_gif_targets`` entry that has a render is dropped from the
    list — the result is a textual question with no inline image refs.

    Use ``use_form="plain"`` for the runtime UI (matches Princeton's
    historical "(num/den)" convention) and ``use_form="latex"`` for
    samples / DB rows that are LaTeX-aware.
    """
    def _sub(text: str) -> str:
        if not text:
            return text
        def _replace(m):
            fname = m.group(1)
            rendered = rendered_by_filename.get(fname)
            if not rendered:
                return m.group(0)
            if "error" in rendered:
                return m.group(0)
            val = rendered.get(use_form) or rendered.get("plain") or ""
            return val
        return re.sub(r"\[img:([^\]]+)\]", _replace, text)

    if question.get("prompt"):
        question["prompt"] = _sub(question["prompt"])
    if question.get("stimulus_text"):
        question["stimulus_text"] = _sub(question["stimulus_text"])
    new_opts = []
    for o in question.get("options") or []:
        new_text = _sub(o.get("text", ""))
        new_o = dict(o)
        new_o["text"] = new_text
        new_opts.append(new_o)
    question["options"] = new_opts

    # Drop inline_gif_targets that we successfully rendered + drop tiny
    # figure_refs that classify as inline_math (e.g. file 351 — the
    # numeric-entry box used to leak in here).
    kept_targets = []
    for ig in question.get("inline_gif_targets") or []:
        fname = ig.get("filename")
        rendered = rendered_by_filename.get(fname)
        if rendered and "error" not in rendered:
            continue  # rendered into text — drop the image ref
        kept_targets.append(ig)
    question["inline_gif_targets"] = kept_targets
    return question


# ── Answer-table vision render ───────────────────────────────────────

_ANSWER_TABLE_SYSTEM_TC = """You transcribe a Text Completion / Sentence
Equivalence answer-choice table from a GRE prep ebook.

The image shows a small table of candidate words (the "answers" the
test-taker chooses from). Tables look like:

  - **TC 1-blank**: a single column, 5 rows of candidate words.
  - **TC 2-blank**: 2 columns labelled "Blank (i)" and "Blank (ii)",
                    each column has 3 candidate words (6 cells total).
  - **TC 3-blank**: 3 columns labelled "Blank (i)", "Blank (ii)",
                    "Blank (iii)", each column has 3 candidate words.
  - **SE (sentence equivalence)**: a single column, 6 rows of words.

Your job: read the words exactly (preserve hyphens, accents, capitalisation)
and return them as JSON.

Output ONLY this JSON shape:

  {
    "kind": "tc_1blank" | "tc_2blank" | "tc_3blank" | "se",
    "blanks": [
      {"label": "Blank (i)",
       "choices": [{"letter": "A", "text": "..."},
                   {"letter": "B", "text": "..."},
                   {"letter": "C", "text": "..."}]},
      ...
    ]
  }

Rules:
  - SE tables: a single blank with 6 choices labelled A-F.
  - TC 1-blank: a single blank with 5 choices labelled A-E.
  - TC 2-blank: two blanks, each with 3 choices labelled A-C and D-F.
  - TC 3-blank: three blanks, each with 3 choices A-C, D-F, G-I.
  - The `letter` value MUST follow the convention above so the runtime
    renderer can map back to the publisher's answer key.
  - Do NOT include the column header row (e.g. "Blank (i)") as a choice.

No markdown fences. No commentary. JSON only."""


def render_answer_table(
    image_bytes: bytes,
    filename: str,
    *,
    client=None,
    cache: Optional[_RenderCache] = None,
    cross_check_with_opus: bool = False,
    expected_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Vision-read a TC / SE answer-choice GIF into structured JSON.

    Returns ``{kind, blanks, source, opus?, agree?}`` or ``{error: ...}``.

    ``expected_kind`` is an optional hint ("tc" | "se") so the renderer
    can sanity-check the model's structural guess.
    """
    if cache is not None:
        hit = cache.get("answer_table", image_bytes)
        if hit is not None:
            return hit

    gw = _import_gateway()
    if client is None:
        client = gw.FloodgateClient()

    block = gw.FloodgateClient.encode_image_for_anthropic(
        image_bytes, media_type=_media_type(filename))
    user_prompt = ("Transcribe the answer-choice table. Filename: "
                   + (filename or "<unknown>")
                   + (". Expected kind: " + expected_kind
                      if expected_kind else ""))
    raw = client.call_anthropic(
        model=gw.MODEL_SONNET,
        system=_ANSWER_TABLE_SYSTEM_TC,
        messages=[{"role": "user", "content": [
            block, {"type": "text", "text": user_prompt}]}],
        max_tokens=600,
    )
    parsed = _parse_render_json(raw)
    if "error" in parsed:
        out = {"error": parsed["error"], "raw": parsed.get("raw", ""),
               "filename": filename, "source": "sonnet"}
        if cache is not None:
            cache.put("answer_table", image_bytes, out)
        return out
    structural = _normalise_answer_table(parsed)
    if "error" in structural:
        out = {"error": structural["error"], "filename": filename,
               "source": "sonnet", "raw_json": parsed}
        if cache is not None:
            cache.put("answer_table", image_bytes, out)
        return out
    structural["filename"] = filename
    structural["source"] = "sonnet"

    if cross_check_with_opus:
        raw2 = client.call_anthropic(
            model=gw.MODEL_OPUS,
            system=_ANSWER_TABLE_SYSTEM_TC,
            messages=[{"role": "user", "content": [
                block, {"type": "text", "text": user_prompt}]}],
            max_tokens=600,
        )
        parsed2 = _parse_render_json(raw2)
        opus_norm = (_normalise_answer_table(parsed2) if "error" not in parsed2
                     else {"error": parsed2["error"]})
        agree = (
            "error" not in opus_norm
            and opus_norm.get("kind") == structural.get("kind")
            and _table_choices_agree(structural, opus_norm)
        )
        structural["opus"] = opus_norm
        structural["agree"] = bool(agree)

    if cache is not None:
        cache.put("answer_table", image_bytes, structural)
    return structural


def _table_choices_agree(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Loose comparison: same set of choice letters with same lower-cased text."""
    def _flat(t):
        out = []
        for blk in t.get("blanks") or []:
            for c in blk.get("choices") or []:
                out.append((c.get("letter", ""),
                            (c.get("text") or "").strip().lower()))
        return sorted(out)
    return _flat(a) == _flat(b)


def _normalise_answer_table(d: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce the LLM's blob into the canonical schema."""
    if not isinstance(d, dict):
        return {"error": "table_not_dict"}
    kind = str(d.get("kind", "")).strip().lower()
    blanks = d.get("blanks") or []
    if kind not in ("tc_1blank", "tc_2blank", "tc_3blank", "se"):
        # Try to infer.
        if len(blanks) == 1 and len(blanks[0].get("choices") or []) == 6:
            kind = "se"
        elif len(blanks) == 1 and len(blanks[0].get("choices") or []) == 5:
            kind = "tc_1blank"
        elif len(blanks) == 2:
            kind = "tc_2blank"
        elif len(blanks) == 3:
            kind = "tc_3blank"
        else:
            return {"error": "unknown_table_kind", "kind": kind,
                    "n_blanks": len(blanks)}
    expected_choices = {
        "tc_1blank": [5],
        "tc_2blank": [3, 3],
        "tc_3blank": [3, 3, 3],
        "se": [6],
    }[kind]
    if len(blanks) != len(expected_choices):
        return {"error": "wrong_blank_count",
                "expected": len(expected_choices),
                "got": len(blanks)}
    norm_blanks = []
    for i, blk in enumerate(blanks):
        choices = blk.get("choices") or []
        if len(choices) != expected_choices[i]:
            return {"error": "wrong_choice_count",
                    "blank_index": i,
                    "expected": expected_choices[i],
                    "got": len(choices)}
        canon = []
        for j, c in enumerate(choices):
            text = str(c.get("text", "") or "").strip()
            if not text:
                return {"error": "empty_choice_text",
                        "blank_index": i, "choice_index": j}
            canon.append({"letter": _expected_letter(kind, i, j),
                          "text": text})
        norm_blanks.append({"label": _expected_blank_label(kind, i),
                            "choices": canon})
    return {"kind": kind, "blanks": norm_blanks}


def _expected_letter(kind: str, blank_idx: int, choice_idx: int) -> str:
    """Canonical letter for the (blank, choice) pair following Princeton's
    publisher convention (TC 2-blank: A-C/D-F; TC 3-blank: A-C/D-F/G-I).
    SE: A-F. TC 1-blank: A-E."""
    if kind == "se":
        return chr(ord("A") + choice_idx)
    if kind == "tc_1blank":
        return chr(ord("A") + choice_idx)
    # tc_2blank / tc_3blank: 3 letters per blank, marching A-C, D-F, G-I.
    return chr(ord("A") + (3 * blank_idx + choice_idx))


def _expected_blank_label(kind: str, blank_idx: int) -> str:
    if kind == "se":
        return "Choices"
    if kind == "tc_1blank":
        return "Blank (i)"
    return ["Blank (i)", "Blank (ii)", "Blank (iii)"][blank_idx]


# ── Apply rendered answer table to a question dict ───────────────────


def apply_answer_table(question: Dict[str, Any],
                       table: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate ``question`` so its options match the rendered answer table.

    Sets:
      - ``options``: list of ``{label, text, is_correct}`` using the runtime
        renderer's label convention. For TC multi-blank the labels are
        ``blank1_A``, ``blank1_B``, ``blank2_A``, ... For TC 1-blank and SE
        they are ``A``, ``B``, ...
      - ``answer_table_html``: a minimal HTML ``<table>`` for the markdown
        sample (one row per choice; columns = blanks).
      - ``answer_table_rendered``: the raw structured JSON.
    Drops ``answer_table_image`` so downstream gates don't re-flag it.

    The publisher's correct-answer label (e.g. "B, F" for SE; "A, E" for
    a 2-blank TC) is then re-applied to flip ``is_correct`` flags so the
    runtime scorer keeps working.
    """
    if "error" in table:
        return question
    kind = table.get("kind")
    blanks = table.get("blanks") or []

    # Build options list with the runtime label convention.
    options: List[Dict[str, Any]] = []
    for i, blk in enumerate(blanks):
        for c in blk.get("choices") or []:
            letter = c.get("letter")
            text = c.get("text", "")
            if kind in ("tc_2blank", "tc_3blank"):
                label = "blank{}_{}".format(i + 1, letter)
            else:
                label = letter
            options.append({"label": label, "text": text,
                            "is_correct": False,
                            # Track which blank a TC option belongs to so
                            # the word-list matcher below can pin a word
                            # to the correct column.
                            "_blank_index": i if kind in ("tc_2blank", "tc_3blank") else 0})

    # Princeton's TC/SE answer keys come in two shapes:
    #   1. Letter-list  ("E"  or  "B, F"  or  "B, D, F") for SE / 1-blank TC.
    #   2. Word-list    ("initiative, strive"  or  "anomalies, daunting,
    #      authoritative") for multi-blank TC — the publisher prints the
    #      actual chosen word per blank, in column order.
    # We sniff the shape and route accordingly.
    raw_label = (question.get("correct_label") or "").strip()
    parts = [p.strip() for p in re.split(r"[,;]+", raw_label) if p.strip()]
    is_letter_list = bool(parts) and all(_is_letter_token(p) for p in parts)

    if is_letter_list:
        if kind in ("tc_2blank", "tc_3blank"):
            for letter in parts:
                offset = ord(letter.upper()) - ord("A")
                blank_idx = offset // 3
                within = offset % 3
                target_letter = chr(ord("A") + within + 3 * blank_idx)
                label = "blank{}_{}".format(blank_idx + 1, target_letter)
                for o in options:
                    if o["label"] == label:
                        o["is_correct"] = True
                        break
        else:
            for letter in parts:
                for o in options:
                    if o["label"].upper() == letter.upper():
                        o["is_correct"] = True
                        break
    elif parts:
        # Word-list. Match each word against the option text, column by
        # column where possible.
        if kind in ("tc_2blank", "tc_3blank") and len(parts) == len(blanks):
            for blank_idx, word in enumerate(parts):
                for o in options:
                    if (o["_blank_index"] == blank_idx
                            and _word_matches(o["text"], word)):
                        o["is_correct"] = True
                        break
        else:
            # SE / 1-blank — text match anywhere.
            for word in parts:
                for o in options:
                    if _word_matches(o["text"], word):
                        o["is_correct"] = True
                        break

    # Strip the helper field before persisting.
    for o in options:
        o.pop("_blank_index", None)

    question["options"] = options
    question["answer_table_html"] = _to_html_table(table)
    question["answer_table_rendered"] = table
    question["answer_table_image"] = None
    return question


def _is_letter_token(s: str) -> bool:
    """Return True if *s* is a single A-Z letter (the publisher's letter-list
    answer-key form)."""
    return len(s) == 1 and s.isalpha()


def _norm_word(s: str) -> str:
    """Lower-case + strip + collapse whitespace + drop trailing punctuation."""
    if not s:
        return ""
    out = re.sub(r"\s+", " ", s.strip().lower())
    return out.strip(".,;:")


def _norm_phrase_for_match(s: str) -> str:
    """Aggressive normalisation for TC multi-blank word-list matching.

    The publisher's printed answer key occasionally drops the leading
    article ("cultural" vs option text "a cultural") or singularises a
    plural ("mitigate the effect" vs option "mitigate the effects").
    Strips:
      - leading articles  (a/an/the)
      - trailing "s" / "es" on the last token
      - all surrounding/internal punctuation except letters and spaces
    """
    base = _norm_word(s)
    if not base:
        return ""
    base = re.sub(r"[^a-z0-9 ]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    parts = base.split(" ")
    # Drop leading article.
    if parts and parts[0] in ("a", "an", "the"):
        parts = parts[1:]
    # Singularise trailing "s" / "es" on the LAST token only.
    if parts:
        last = parts[-1]
        if len(last) > 3 and last.endswith("es"):
            parts[-1] = last[:-2]
        elif len(last) > 2 and last.endswith("s"):
            parts[-1] = last[:-1]
    return " ".join(parts)


def _word_matches(option_text: str, key_word: str) -> bool:
    """Match *key_word* (from the answer key) against an option's text.

    Tries exact, then aggressive (article-stripping + plural-tolerant)
    normalisation in both directions.
    """
    if _norm_word(option_text) == _norm_word(key_word):
        return True
    return _norm_phrase_for_match(option_text) == _norm_phrase_for_match(key_word)


def _split_correct_label(label):
    if not label:
        return []
    return [p.strip().upper() for p in re.split(r"[\s,]+", str(label)) if p.strip()]


def _to_html_table(table: Dict[str, Any]) -> str:
    blanks = table.get("blanks") or []
    if not blanks:
        return ""
    header = "<tr>" + "".join(
        "<th>" + _html.escape(b.get("label", "")) + "</th>" for b in blanks
    ) + "</tr>"
    n_rows = max(len(b.get("choices") or []) for b in blanks)
    rows = []
    for r in range(n_rows):
        cells = []
        for b in blanks:
            choices = b.get("choices") or []
            if r < len(choices):
                cell = (choices[r].get("letter", "") + ". "
                        + choices[r].get("text", ""))
            else:
                cell = ""
            cells.append("<td>" + _html.escape(cell) + "</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return ("<table border='1' cellpadding='4' cellspacing='0'>"
            + header + "".join(rows) + "</table>")
