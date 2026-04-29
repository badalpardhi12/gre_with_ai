"""Princeton Review *1,014 GRE Practice Questions, 3rd Edition* (EPUB) extractor.

This is the deterministic-first ingest pipeline described in
``.claude/plans/princeton-extraction.md``. The EPUB is XHTML + GIF/JPG
assets, so OCR is not needed; question stems, options for MCQ/QC/RC,
passages, and answer-key labels are all in the markup. The only place a
vision model is needed is for TC/SE answer-choice tables that the
publisher rendered as a single GIF; that path is wired in Phase 2.

Stage layout (per the plan):

    A  enumerate_chapters / parse_drill_chapter / parse_answer_chapter
       / detect_passages / detect_figures   --- pure stdlib + bs4
    B  vision fill-ins                      --- Phase 2 (separate file)
    C  post-process: money cleanup, latex repair, numeric parse
    D  validation gates (12 of them)
    E  persistence to DB                    --- Phase 3 (not in this file)

Phase 0 entry point::

    venv/bin/python scripts/extract_princeton.py --dry-run --section tcd1

The dry run prints per-gate pass counts and a small sample of failures.
No DB writes happen.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup, NavigableString, Tag


# Paths & constants -------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EPUB_PATH = os.path.join(
    PROJECT_ROOT,
    "data",
    "ebooks",
    "Princeton Review - 1,014 GRE Practice Questions, 3rd Edition-Princeton Review (2012).epub",
)
EXTRACT_DIR = os.path.join(PROJECT_ROOT, "data", "extracted", "princeton")
DUMP_PATH = os.path.join(EXTRACT_DIR, "princeton_v2.json")
DEFECT_REPORT_PATH = os.path.join(EXTRACT_DIR, "defect_report.md")
SOURCE_TAG = "princeton_2012"

# Bullet GIFs that the EPUB uses to mark MCQ option rows. They are
# universally present and NEVER count as a "diagram" or "figure".
BULLET_IMAGE_FILES = {
    "Revi_9780307945396_epub_420_r1.jpg",  # filled circle (multi-select)
    "Revi_9780307945396_epub_421_r1.jpg",  # empty circle (radio)
}

# Numeric-entry boxes: the publisher renders the user input area as a
# small GIF (a thin rectangle with a caret). 351 is by far the most
# common (~25 occurrences); 112 is the stacked num/den fraction box (~3
# occurrences). Without these the extractor mis-types those questions as
# ``mcq_single`` with zero options — which was the user-reported "missing
# options" defect class.
NUMERIC_ENTRY_BOX_FILES = {
    "Revi_9780307945396_epub_419_r1.jpg",  # generic numeric entry box
    "Revi_9780307945396_epub_412_r1.jpg",  # fraction entry box (variant)
    "Revi_9780307945396_epub_351_r1.jpg",  # generic 78x26 numeric entry box
    "Revi_9780307945396_epub_112_r1.jpg",  # 108x60 stacked fraction entry
}

# QC option phrasing — when these four phrases are the option text, the
# subtype is QC regardless of how the parser initially classified it.
# The publisher sometimes renders the QC stem entirely as an image (so
# "Quantity A" never appears in the text) but the four boilerplate
# options are always plain text.
_QC_OPTION_PHRASES = (
    "quantity a is greater",
    "quantity b is greater",
    "the two quantities are equal",
    "the relationship cannot be determined",
)

QUANT_DRILL_SLUGS = {
    "pid", "phd", "qed", "fdpd", "npd", "rpd", "esd", "pad",
    "led", "lad", "gsfd", "prsd", "cgd", "trid", "cird", "cpd",
    "figd",
}
VERBAL_DRILL_SLUGS = {"tcd", "rcd", "sed"}

QUANT_ANSWER_SLUG = {
    "pid": "piAnE", "phd": "phAnE", "qed": "qeAnE", "fdpd": "fdpAnE",
    "npd": "npAnE", "rpd": "rpAnE", "esd": "esAnE", "pad": "paAnE",
    "led": "leAnE", "lad": "laAnE", "gsfd": "gsfAnE", "prsd": "prsAnE",
    "cgd": "cgAnE", "trid": "triAnE", "cird": "cirAnE", "cpd": "cpAnE",
    "figd": "figAnE",
}
VERBAL_ANSWER_SLUG = {
    "tcd": "tcAnE", "rcd": "rcAnE", "sed": "seAnE",
}


# Helpers -----------------------------------------------------------------

_QST_HREF_RE = re.compile(r"#QST(\d+)a?$")
_QST_ID_RE = re.compile(r"^QST(\d+)$")
_PASSAGE_MARKER_RE = re.compile(
    r"Questions?\s+(\d+)(?:\s*[\u2013\u2014\-]\s*(\d+))?\s+refer", re.I
)

# CSS classes that the publisher uses on a `<p>` to mark either a passage
# preamble ("Questions N-M refer to the following...") OR a stimulus block
# (chart caption, etc.) inside a DI/RC chapter. The extractor must (a) treat
# these as passage anchors and (b) treat the next QST as a new question — i.e.
# anything between such a marker and the next QST belongs to the SHARED
# stimulus, not to the previous question.
PASSAGE_MARKER_CLASSES = (
    "extract1",
    "extract1_pagebreak",
    "extract_pagebreak",
    "nonindent",            # cgd1: 4 markers
    "nonindent_pagebreak",  # cgd1: 21 markers — the big one we missed
)

# Inline GIFs whose filename encodes a small fraction (e.g.
# "Revi_..._frac4-7_r1.gif" → 4/7). Recognising these deterministically
# lets us replace the publisher's image-as-fraction with readable text
# without spending vision dollars.
_FRAC_GIF_RE = re.compile(
    r"Revi_\d+_epub_frac(-?\d+)-(-?\d+)_r1\.gif$",
    re.I,
)


def fraction_text_from_filename(filename):
    """Return ``"(num/den)"`` if *filename* encodes a fraction GIF, else None."""
    if not filename:
        return None
    m = _FRAC_GIF_RE.match(filename)
    if not m:
        return None
    return "(" + m.group(1) + "/" + m.group(2) + ")"


# Inline GIFs that are publisher artwork (e.g. operator-definition glyph,
# answer-choice glyph for an option that *is* a small expression). These
# need vision transcription. The filename pattern is
# "Revi_..._<digits>_r1.gif" with no "frac" prefix and not a bullet/box.
_INLINE_GIF_RE = re.compile(r"^Revi_\d+_epub_(\d+)_r1\.gif$", re.I)


def is_transcribable_inline_gif(filename):
    """Return True for non-fraction publisher GIFs that need vision."""
    if not filename:
        return False
    if filename in BULLET_IMAGE_FILES or filename in NUMERIC_ENTRY_BOX_FILES:
        return False
    if fraction_text_from_filename(filename):
        return False
    return bool(_INLINE_GIF_RE.match(filename))


def _whitespace(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def normalise_option_text(text):
    return re.sub(r"\s+", " ", (text or "")).lower().strip()


_MONEY_DD_RE = re.compile(r"\$\$(\d[\d,.\s{}\\]*)\$(?!\$)")


def clean_money_dollars(s):
    if not s:
        return s
    return _MONEY_DD_RE.sub(
        lambda m: "$" + m.group(1).replace("{,}", ",").replace("\\,", ","),
        s,
    )


_NUMERIC_SQRT_RE = re.compile(r"\\sqrt\{(\d+(?:\.\d+)?)\}")
_NUMERIC_FRAC_RE = re.compile(r"\\frac\{(-?\d+)\}\{(-?\d+)\}")


def parse_numeric_value(value):
    import math
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Princeton answer keys use unicode minus / en-dash / em-dash for
    # negative numbers ("−80/81", "–1"). Normalise to ASCII before any
    # numeric parse.
    s = (s.replace("\u2212", "-")    # mathematical minus
           .replace("\u2013", "-")    # en-dash
           .replace("\u2014", "-")    # em-dash
           .replace("\u2010", "-")    # hyphen
           .replace("\u2011", "-"))   # non-breaking hyphen
    s_clean = s.replace(",", "").replace("$", "").replace("\\$", "")
    try:
        return float(s_clean)
    except ValueError:
        pass
    if "/" in s_clean and "\\" not in s_clean:
        parts = s_clean.split("/")
        if len(parts) == 2:
            try:
                return float(parts[0]) / float(parts[1])
            except (ValueError, ZeroDivisionError):
                pass
    m = _NUMERIC_FRAC_RE.search(s)
    if m:
        try:
            return float(m.group(1)) / float(m.group(2))
        except (ValueError, ZeroDivisionError):
            pass
    m = _NUMERIC_SQRT_RE.search(s)
    if m:
        try:
            return math.sqrt(float(m.group(1)))
        except ValueError:
            pass
    m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\b", s_clean)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def latex_balance_check(text):
    """Return (passed, defect_tags) for math-delimiter balance + raw \\f / bare $N."""
    defects = []
    if text is None or text == "":
        return True, defects
    n_op = len(re.findall(r"\\\(", text))
    n_cp = len(re.findall(r"\\\)", text))
    n_ob = len(re.findall(r"\\\[", text))
    n_cb = len(re.findall(r"\\\]", text))
    if n_op != n_cp:
        defects.append("unmatched_paren")
    if n_ob != n_cb:
        defects.append("unmatched_brack")
    backslash_f = re.findall(r"\\f([a-zA-Z]*)", text)
    bad_f = [m for m in backslash_f
             if not (m.startswith("rac") or m.startswith("box")
                     or m.startswith("orall") or m.startswith("rown")
                     or m.startswith("lat"))]
    if bad_f:
        defects.append("raw_backslash_f")
    # ``bare_dollar`` is meant to catch LaTeX delimiters like ``$x$`` or
    # ``$\frac{1}{2}$`` that the renderer can't pair up — it must NOT
    # fire on real prose currency like ``$5.4 million``. Two signals:
    #   - the dollar must appear in a math-ish context (next char is a
    #     LaTeX command or unbalanced backslash), OR
    #   - there are two unbalanced ``$`` characters in the text.
    stripped = re.sub(r"\\\(.*?\\\)", "", text, flags=re.DOTALL)
    stripped = re.sub(r"\\\[.*?\\\]", "", stripped, flags=re.DOTALL)
    stripped = re.sub(r"\\\$", "", stripped)
    if re.search(r"\$\\(?:frac|sqrt|left|right|begin|end|sum|int|prod|"
                 r"alpha|beta|gamma|delta|theta|pi|sigma|mu|cdot|times)",
                 stripped):
        defects.append("bare_dollar")
    return (len(defects) == 0), defects


# Stage A: deterministic ingest -------------------------------------------


SECTION_SLUG_RE = re.compile(
    r"OEBPS/Revi_\d+_epub_c(\d+)_s(\d+)_([A-Za-z]+\d*)_r1\.htm$"
)


def _parse_chapter_path(path):
    m = SECTION_SLUG_RE.match(path)
    if not m:
        return None
    chapter, section, slug = m.group(1), m.group(2), m.group(3)
    base_slug = re.sub(r"\d+$", "", slug)
    drill_num = None
    if base_slug in QUANT_DRILL_SLUGS:
        m2 = re.match(r"^([a-z]+?)(\d+)$", slug)
        if m2:
            drill_num = int(m2.group(2))
        role = "drill"
        measure = "quant"
    elif base_slug in VERBAL_DRILL_SLUGS:
        m2 = re.match(r"^([a-z]+?)(\d+)$", slug)
        if m2:
            drill_num = int(m2.group(2))
        role = "drill"
        measure = "verbal"
    elif slug.endswith("AnE"):
        role = "answers"
        measure = "verbal" if slug in {"tcAnE", "rcAnE", "seAnE"} else "quant"
    else:
        role = "other"
        measure = "verbal" if base_slug in {"tc", "rc", "se"} else "quant"
    return {
        "path": path, "chapter": chapter, "section": section,
        "slug": slug, "base_slug": base_slug, "role": role,
        "measure": measure, "drill_num": drill_num,
    }


def enumerate_chapters(epub_path=EPUB_PATH):
    """Return ordered list of chapter specs from the EPUB."""
    chapters = []
    with zipfile.ZipFile(epub_path) as z:
        for name in sorted(z.namelist()):
            spec = _parse_chapter_path(name)
            if spec is not None:
                chapters.append(spec)
    return chapters


def _text_from(node):
    """Flatten *node* to plain text while preserving inline math hints.

    The publisher uses ``<sup>`` for exponents (often with ``class="frac"``
    even though it is *not* a fraction) and ``<sub>`` for subscripts. Both
    were getting silently dropped, producing readings like ``"a 2"`` for
    ``a^2``. We pre-walk the tree, replacing every ``<sup>``/``<sub>`` with
    a NavigableString that contains the LaTeX/ASCII rendering, then collapse
    the rest exactly as before. We also rewrite ``<img class="inline">``
    fraction GIFs (e.g. ``frac4-7_r1.gif``) to ``"(4/7)"`` text so options
    and stems don't render as ``[img:...]`` placeholders for fractions.

    Surviving non-fraction publisher GIFs (operator glyphs, equation
    images, stem-as-image questions like esd1 #5) are converted to
    ``[img:filename]`` placeholders so the downstream image pipeline
    (:func:`services.image_pipeline.resolve_question_images`) can render
    them via vision and substitute the text back in. Without this, a
    stem that is JUST an equation GIF would produce an empty prompt and
    fail the G10 length gate.
    """
    clone = BeautifulSoup(str(node), "html.parser")
    _rewrite_sup_sub_in_place(clone)
    _rewrite_fraction_gifs_in_place(clone)
    _rewrite_stem_imgs_to_placeholders_in_place(clone)
    parts = []
    for child in clone.descendants:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag) and child.name == "br":
            parts.append("\n")
    return _whitespace("".join(parts))


def _rewrite_stem_imgs_to_placeholders_in_place(soup):
    """Replace stem inline-math ``<img>`` tags with ``[img:filename]`` markers.

    Only inline-math GIFs (small publisher glyphs that the inline-math
    vision pass will transcribe into text) get a placeholder. Real
    diagram images (the larger ones that will be shown as figures) are
    left intact — they end up in ``figure_refs`` and the renderer shows
    the image separately, so a textual placeholder would be a duplicate.

    Bullet glyphs, numeric-entry boxes, and answer-table GIFs are
    surfaced through other channels and never become placeholders.
    """
    for img in list(soup.find_all("img")):
        fname = (img.get("src", "") or "").rsplit("/", 1)[-1]
        if not fname:
            img.decompose()
            continue
        if fname in BULLET_IMAGE_FILES:
            img.decompose()
            continue
        if fname in NUMERIC_ENTRY_BOX_FILES:
            img.decompose()
            continue
        if fraction_text_from_filename(fname):
            # Already replaced by _rewrite_fraction_gifs_in_place; if any
            # survived (shouldn't), drop them silently.
            img.decompose()
            continue
        if re.match(r"Revi_\d+_fi\d+_r1\.gif$", fname):
            img.decompose()
            continue
        # Only inline-sized publisher glyphs get a placeholder. Larger
        # images become figures and the renderer shows them visually.
        if not is_transcribable_inline_gif(fname):
            img.decompose()
            continue
        is_inline_marked = "inline" in (img.get("class") or [])
        try:
            h = int(img.get("height") or 0)
            w = int(img.get("width") or 0)
        except (TypeError, ValueError):
            h, w = 0, 0
        is_inline_sized = h <= 60 and w <= 80
        if not is_inline_marked and not is_inline_sized:
            # A "transcribable" GIF that's actually large — treat as a
            # diagram (figure_refs already has it). Drop the placeholder.
            img.decompose()
            continue
        img.replace_with(NavigableString("[img:" + fname + "]"))


def _rewrite_sup_sub_in_place(soup):
    """Replace every ``<sup>``/``<sub>`` with a NavigableString.

    ``<sup>2</sup>`` → ``"^{2}"`` and ``<sub>x</sub>`` → ``"_{x}"``. Works
    for nested cases (e.g. ``a<sup>b<sub>c</sub></sup>`` → ``a^{b_{c}}``)
    by repeatedly walking innermost tags until no ``<sup>``/``<sub>`` are
    left in the tree. Run on a clone of the original tree so we don't
    mutate the caller's BeautifulSoup.
    """
    while True:
        targets = []
        for tag in soup.find_all(["sup", "sub"]):
            # Only rewrite the innermost (leaf) sup/sub on this pass.
            if not tag.find(["sup", "sub"]):
                targets.append(tag)
        if not targets:
            return
        for tag in targets:
            inner_text = ""
            for child in tag.descendants:
                if isinstance(child, NavigableString):
                    inner_text += str(child)
            inner_text = _whitespace(inner_text)
            if not inner_text:
                tag.decompose()
                continue
            marker = "^" if tag.name == "sup" else "_"
            replacement = marker + "{" + inner_text + "}"
            tag.replace_with(NavigableString(replacement))


def _rewrite_fraction_gifs_in_place(soup):
    """Replace inline fraction GIFs with ``"(num/den)"`` text in place."""
    for img in list(soup.find_all("img")):
        fname = (img.get("src", "") or "").rsplit("/", 1)[-1]
        replacement = fraction_text_from_filename(fname)
        if replacement is not None:
            img.replace_with(NavigableString(replacement))


def _qst_id(tag):
    if not tag.has_attr("id"):
        return None
    m = _QST_ID_RE.match(tag["id"])
    if not m:
        return None
    return int(m.group(1))


def parse_answer_chapter(zf, htm_name):
    """Return {QST_id_int: correct_label_string} for one *AnE chapter."""
    soup = BeautifulSoup(zf.read(htm_name), "html.parser")
    out = {}
    for p in soup.select("p.hanging0"):
        a = p.find("a", class_="hlink")
        if not a:
            continue
        href = a.get("href", "")
        m = _QST_HREF_RE.search(href)
        if not m:
            continue
        qst_id = int(m.group(1))
        full_text = _whitespace(p.get_text(" ", strip=True))
        anchor_text = _whitespace(a.get_text(" ", strip=True))
        label = full_text
        if anchor_text and label.startswith(anchor_text):
            label = label[len(anchor_text):].strip()
        out[qst_id] = label
    return out


def _img_filename(img):
    src = img.get("src", "") or ""
    return src.rsplit("/", 1)[-1]


def _is_bullet_img(img):
    return _img_filename(img) in BULLET_IMAGE_FILES


def _is_inline_img(img):
    cls = img.get("class") or []
    return "inline" in cls


def _is_numeric_entry_img(img):
    return _img_filename(img) in NUMERIC_ENTRY_BOX_FILES


def _is_answer_table_img(img):
    """TC/SE GIF answer-choice table: filename like Revi_..._fi006_r1.gif."""
    fname = _img_filename(img)
    return bool(re.match(r"Revi_\d+_fi\d+_r1\.gif$", fname))


def _option_text_from_img_hang_p(p_tag):
    clone = BeautifulSoup(str(p_tag), "html.parser").p
    if clone is None:
        return ""
    _rewrite_sup_sub_in_place(clone)
    for img in clone.find_all("img"):
        src = _img_filename(img)
        if src in BULLET_IMAGE_FILES:
            img.decompose()
            continue
        # Fraction GIFs (e.g. frac4-7) → readable "(4/7)" text.
        frac = fraction_text_from_filename(src)
        if frac is not None:
            img.replace_with(NavigableString(frac))
            continue
        if "inline" in (img.get("class") or []):
            placeholder = "[img:" + src + "]"
            img.replace_with(NavigableString(placeholder))
        else:
            # Non-inline option-text GIFs — keep the placeholder so a
            # downstream vision pass (or human) can transcribe.
            placeholder = "[img:" + src + "]"
            img.replace_with(NavigableString(placeholder))
    return _whitespace(clone.get_text(" ", strip=True))


def detect_passages(soup):
    """Detect RC/DI passages — one entry per ``Questions N-M refer`` marker.

    The publisher uses several CSS classes on the marker paragraph
    depending on the chapter type (RC uses ``extract1`` /
    ``extract1_pagebreak``; DI uses ``nonindent`` / ``nonindent_pagebreak``
    / ``extract_pagebreak``). We scan **every** ``<p>`` whose class is in
    :data:`PASSAGE_MARKER_CLASSES`, so the same code path covers RC short
    passages, RC long passages, and DI chart clusters.

    Each entry contains the literal passage text (RC) or, for DI clusters,
    the descriptive caption + a list of figure references (chart filenames).
    """
    out = []
    seen_positions = set()
    counter = 0
    # Collect all candidate <p> markers in document order.
    candidates = []
    for p in soup.find_all("p"):
        cls = p.get("class") or []
        if not any(c in PASSAGE_MARKER_CLASSES for c in cls):
            continue
        text = _whitespace(p.get_text(" ", strip=True))
        m = _PASSAGE_MARKER_RE.match(text)
        if not m:
            continue
        # Avoid duplicate hits if a tag matches multiple times.
        pos = id(p)
        if pos in seen_positions:
            continue
        seen_positions.add(pos)
        candidates.append((p, m))

    for marker, m in candidates:
        q_start = int(m.group(1))
        q_end = int(m.group(2)) if m.group(2) else q_start
        passage_parts = []
        figure_refs = []
        # Walk forward across siblings AND inside `block_rc` containers,
        # stopping at either the next QST or the next passage marker.
        sib = marker.find_next_sibling()
        steps = 0
        while sib is not None and steps < 10:
            if isinstance(sib, Tag):
                cls = sib.get("class", []) or []
                # Stop at next passage marker.
                if (sib.name == "p"
                        and any(c in PASSAGE_MARKER_CLASSES for c in cls)
                        and _PASSAGE_MARKER_RE.match(
                            _whitespace(sib.get_text(" ", strip=True)))):
                    break
                # Stop at next QST anchor (means previous block belongs
                # to the cluster preamble, but the next question begins).
                if sib.name == "p" and _qst_id(sib) is not None:
                    break
                # Bare image paragraph (DI charts before the first QST).
                if sib.name == "p":
                    direct_imgs = [img for img in sib.find_all("img")]
                    for img in direct_imgs:
                        if _is_bullet_img(img) or _is_numeric_entry_img(img):
                            continue
                        if _is_inline_img(img):
                            continue
                        figure_refs.append({
                            "src": img.get("src", ""),
                            "filename": _img_filename(img),
                            "kind": "stimulus_chart",
                            "width": img.get("width"),
                            "height": img.get("height"),
                        })
                    ptext = _text_from(sib)
                    if ptext and "view a larger image" not in ptext.lower():
                        passage_parts.append(ptext)
                    sib = sib.find_next_sibling()
                    steps += 1
                    continue
                # RC: passage paragraphs live inside <div class="block_rc">.
                if "block_rc" in cls:
                    for p in sib.find_all("p"):
                        passage_parts.append(_text_from(p))
                    # Continue walking — there may be a follow-up block.
                    sib = sib.find_next_sibling()
                    steps += 1
                    continue
                # DI: chart and caption live inside <div class="block0">.
                if "block0" in cls:
                    for img in sib.find_all("img"):
                        if _is_bullet_img(img):
                            continue
                        if _is_numeric_entry_img(img):
                            continue
                        if _is_inline_img(img):
                            continue
                        figure_refs.append({
                            "src": img.get("src", ""),
                            "filename": _img_filename(img),
                            "kind": "stimulus_chart",
                            "width": img.get("width"),
                            "height": img.get("height"),
                        })
                    for p in sib.find_all("p"):
                        ptext = _text_from(p)
                        if ptext and "view a larger image" not in ptext.lower():
                            passage_parts.append(ptext)
                    sib = sib.find_next_sibling()
                    steps += 1
                    continue
            sib = sib.find_next_sibling()
            steps += 1
        passage_text = "\n\n".join([p for p in passage_parts if p])
        counter += 1
        out.append({
            "passage_index": counter,
            "q_start": q_start,
            "q_end": q_end,
            "passage_text": passage_text,
            "figure_refs": figure_refs,
            "anchor": "p" + str(counter) + "_q" + str(q_start) + "-" + str(q_end),
        })
    return out


def _passage_for_qnum(passages, q_num):
    for p in passages:
        if p["q_start"] <= q_num <= p["q_end"]:
            return p
    return None


def _detect_quant_subtype(block_html, has_options, has_numeric_box,
                          opt_uses_filled_bullet=False):
    soup = BeautifulSoup(block_html, "html.parser")
    for table in soup.find_all("table"):
        text = _whitespace(table.get_text(" ", strip=True))
        if "Quantity A" in text and "Quantity B" in text:
            return "qc"
    if has_numeric_box and not has_options:
        return "numeric_entry"
    # Multi-select MCQ: prompt cue OR option bullets are the filled-circle
    # image (420). Princeton uses several phrasing variants:
    #   - "indicate all such" / "indicate all of the"
    #   - "select all that apply"
    #   - "Which of the following ARE/SATISFY/HAVE..." (plural verb)
    #   - "consider each ... select all that apply" (rare in quant)
    body_text = _whitespace(soup.get_text(" ", strip=True)).lower()
    if "indicate all such" in body_text \
            or "indicate all of the" in body_text \
            or "select all that apply" in body_text \
            or opt_uses_filled_bullet:
        return "mcq_multi"
    # Plural-verb "Which of the following ARE/SATISFY/HAVE/COULD..." —
    # heuristic, refined later by post-processing if the answer key shows
    # only one correct letter. Allow up to ~5 intervening words to catch
    # phrasings like "Which of the following values of x satisfy".
    if re.search(r"which of the following(?:\s+\S+){0,5}\s+"
                 r"(are|satisfy|satisfies|have|could\s+be|could\s+equal|"
                 r"could\s+give|could\s+result|could\s+the|are\s+possible|"
                 r"are\s+valid|are\s+integers|are\s+even|are\s+odd|"
                 r"are\s+factors|are\s+multiples|are\s+values|"
                 r"are\s+equivalent|are\s+not|are\s+equal\s+to|"
                 r"are\s+greater\s+than|are\s+less\s+than|are\s+between|"
                 r"are\s+within)\b",
                 body_text):
        return "mcq_multi"
    # "For which of the [weeks/values/...]" — the user enumerates which
    # subset matches a condition. Princeton's cgd4 chart questions and
    # the phd2 "values of x is f(x) between 0 and 4" questions use this.
    if re.search(r"\bfor\s+which\s+of\s+the\s+\w+\b", body_text):
        return "mcq_multi"
    return "mcq_single"


def _detect_rc_subtype(prompt):
    """Decide rc_single / rc_multi / rc_select_passage from the stem text.

    - "select the sentence" / "select-in-passage" → rc_select_passage
    - "consider each ... select all that apply" → rc_multi
    - else → rc_single
    """
    plain = prompt.lower()
    if re.search(r"select\s+the\s+sentence", plain):
        return "rc_select_passage"
    if "select all that apply" in plain or re.search(
            r"consider\s+each\s+of\s+the\s+(following\s+)?(answer\s+)?choices",
            plain):
        return "rc_multi"
    return "rc_single"


def _qc_table_text(blk):
    table = blk.find("table")
    if not table:
        return None
    cells = table.find_all("td")
    if len(cells) < 4:
        return None
    quant_a = _text_from(cells[2])
    quant_b = _text_from(cells[3])
    return "Quantity A: " + quant_a + "\nQuantity B: " + quant_b


def parse_drill_chapter(zf, htm_name):
    """Parse a single drill chapter into a list of raw question dicts."""
    spec = _parse_chapter_path(htm_name)
    if spec is None or spec["role"] != "drill":
        return []
    measure = spec["measure"]
    base_slug = spec["base_slug"]
    drill_num = spec["drill_num"]

    soup = BeautifulSoup(zf.read(htm_name), "html.parser")

    # Detect passages for both RC (verbal) AND DI (cgd / figd) drills.
    passages = []
    if base_slug in ("rcd", "cgd", "figd"):
        passages = detect_passages(soup)

    question_markers = []
    for p in soup.find_all("p"):
        qid = _qst_id(p)
        if qid is None:
            continue
        question_markers.append(p)

    out = []
    for q_idx, marker in enumerate(question_markers, start=1):
        qst_id = _qst_id(marker)
        if qst_id is None:
            continue

        block_nodes = []
        opt_nodes = []
        sib = marker.find_next_sibling()
        while sib is not None:
            if isinstance(sib, Tag):
                if sib.name == "p" and _qst_id(sib) is not None:
                    break
                if sib.name in ("h2", "h3"):
                    break
                cls = sib.get("class", []) or []
                # Stop at any passage marker — content past the marker
                # belongs to the NEXT cluster's stimulus, not this question.
                if (sib.name == "p"
                        and any(c in PASSAGE_MARKER_CLASSES for c in cls)
                        and _PASSAGE_MARKER_RE.match(
                            _whitespace(sib.get_text(" ", strip=True)))):
                    break
                if "img_hang" in cls:
                    # The publisher uses two patterns:
                    #   <div class="img_hang"> wrapping <p class="img_hang"> rows
                    #   bare <p class="img_hang"> as direct sibling
                    # Flatten the div case so each option is its own node.
                    if sib.name == "div":
                        for p in sib.find_all("p", class_="img_hang"):
                            opt_nodes.append(p)
                    else:
                        opt_nodes.append(sib)
                else:
                    block_nodes.append(sib)
            sib = sib.find_next_sibling()

        if not opt_nodes:
            for blk in block_nodes:
                for p in blk.find_all("p", class_="img_hang"):
                    opt_nodes.append(p)

        prompt_parts = []
        figure_refs = []
        has_numeric_box = False
        has_qc_table = False
        has_answer_table_img = False
        answer_table_image = None

        qc_text = None
        for blk in block_nodes:
            text_for_table = _whitespace(blk.get_text(" ", strip=True))
            if ("Quantity A" in text_for_table
                    and "Quantity B" in text_for_table):
                qc_text = _qc_table_text(blk)
                has_qc_table = True

        for blk in block_nodes:
            for img in blk.find_all("img"):
                if _is_inline_img(img):
                    continue
                if _is_bullet_img(img):
                    continue
                if _is_numeric_entry_img(img):
                    has_numeric_box = True
                    continue
                if _is_answer_table_img(img):
                    has_answer_table_img = True
                    # Surface the answer-table GIF filename so a later
                    # vision pass (and the verifier) can grab it without
                    # re-walking the EPUB.
                    if answer_table_image is None:
                        answer_table_image = _img_filename(img)
                    continue
                # Recognise inline-fraction GIFs (replaced as text by
                # `_text_from` later) — never treat as a diagram.
                if fraction_text_from_filename(_img_filename(img)):
                    continue
                # Tiny non-fraction publisher glyphs (e.g. operator-def
                # GIFs). Their dimensions are like 24-40px tall × 30px
                # wide. They look like figures by URL but they are inline
                # math; ship them as ``inline_gif_targets`` for vision,
                # not as ``figure_refs`` (which the renderer treats as a
                # full-size diagram).
                if is_transcribable_inline_gif(_img_filename(img)):
                    try:
                        h = int(img.get("height") or 0)
                        w = int(img.get("width") or 0)
                    except (TypeError, ValueError):
                        h, w = 0, 0
                    if h <= 60 and w <= 80:
                        # Vision pass will handle this — skip the figure.
                        continue
                figure_refs.append({
                    "src": img.get("src", ""),
                    "filename": _img_filename(img),
                    "kind": "diagram",
                    "width": img.get("width"),
                    "height": img.get("height"),
                })

        for blk in block_nodes:
            clone = BeautifulSoup(str(blk), "html.parser")
            for p in clone.find_all("p", class_="img_hang"):
                p.decompose()
            # Drop QC tables here; we already captured Quantity A/B above
            # via :func:`_qc_table_text` and we don't want the same values
            # to appear twice (once flattened, once labeled).
            for tab in clone.find_all("table"):
                tab_text = _whitespace(tab.get_text(" ", strip=True))
                if "Quantity A" in tab_text and "Quantity B" in tab_text:
                    tab.decompose()
            text = _text_from(clone)
            if text:
                prompt_parts.append(text)

        prompt = "\n\n".join([p for p in prompt_parts if p]).strip()
        if qc_text and "Quantity A" in qc_text:
            if "Quantity A:" not in prompt or "Quantity B:" not in prompt:
                if prompt:
                    prompt = prompt + "\n\n" + qc_text
                else:
                    prompt = qc_text
        prompt = clean_money_dollars(prompt)

        options = []
        for i, opt_p in enumerate(opt_nodes):
            text = _option_text_from_img_hang_p(opt_p)
            label = chr(ord("A") + i)
            options.append({
                "label": label,
                "text": clean_money_dollars(text),
                "is_correct": False,
            })

        if base_slug == "rcd":
            subtype = _detect_rc_subtype(prompt)
        elif base_slug == "tcd":
            subtype = "tc"
        elif base_slug == "sed":
            subtype = "se"
        elif base_slug in QUANT_DRILL_SLUGS:
            block_html = "<root>" + "".join(str(b) for b in block_nodes) + "</root>"
            # Inspect option bullet color to disambiguate single vs multi.
            opt_uses_filled_bullet = False
            for opt_p in opt_nodes:
                for img in opt_p.find_all("img"):
                    if _img_filename(img) == "Revi_9780307945396_epub_420_r1.jpg":
                        opt_uses_filled_bullet = True
                        break
                if opt_uses_filled_bullet:
                    break
            subtype = _detect_quant_subtype(
                block_html,
                has_options=bool(options),
                has_numeric_box=has_numeric_box,
                opt_uses_filled_bullet=opt_uses_filled_bullet,
            )
        else:
            subtype = "unknown"

        needs_vision = (subtype in ("tc", "se")) or has_answer_table_img
        if subtype.startswith("rc_") and not options and has_answer_table_img:
            needs_vision = True

        # Track whether ANY transcribable inline GIF appears in the prompt
        # or options — if so, vision should fill in the missing math/text.
        # Inline-fraction GIFs are already converted to "(num/den)" text,
        # so they don't trigger this. Other inline GIFs (operator defs,
        # publisher artwork) do. We use a small-image heuristic so a
        # full-page chart never gets mislabelled as inline math.
        inline_gif_targets = []

        def _maybe_add_inline_target(img, context):
            fname = _img_filename(img)
            if not is_transcribable_inline_gif(fname):
                return
            try:
                h = int(img.get("height") or 0)
                w = int(img.get("width") or 0)
            except (TypeError, ValueError):
                h, w = 0, 0
            # Big images (>60×80) are charts, not inline math glyphs.
            is_inline_marked = "inline" in (img.get("class") or [])
            if not is_inline_marked and not (h <= 60 and w <= 80):
                return
            inline_gif_targets.append({
                "src": img.get("src", ""),
                "filename": fname,
                "context": context,
                "width": img.get("width"),
                "height": img.get("height"),
            })

        for blk in block_nodes:
            for img in blk.find_all("img"):
                _maybe_add_inline_target(img, "prompt")
        for opt_p in opt_nodes:
            for img in opt_p.find_all("img"):
                _maybe_add_inline_target(img, "option")
        if inline_gif_targets:
            needs_vision = True

        stimulus_text = ""
        stimulus_anchor = ""
        cluster_figure_refs = []
        # RC and DI clusters: link every sibling question to one shared
        # passage / chart so the renderer can show stimulus once + N
        # questions underneath.
        if (subtype.startswith("rc_") or base_slug in ("cgd", "figd")) \
                and passages:
            p = _passage_for_qnum(passages, q_idx)
            if p is not None:
                stimulus_text = p["passage_text"]
                stimulus_anchor = ("princeton_" + base_slug
                                   + str(drill_num) + "_" + p["anchor"])
                cluster_figure_refs = list(p.get("figure_refs", []))

        # Merge cluster figures into the per-question figure list so the
        # downstream renderer can simply iterate ``figure_refs``. Dedupe
        # by filename in case a chart was already attached to the question.
        if cluster_figure_refs:
            existing_fnames = {f.get("filename") for f in figure_refs}
            for f in cluster_figure_refs:
                if f.get("filename") not in existing_fnames:
                    figure_refs.append(f)

        out.append({
            "qst_id": qst_id, "drill_num": drill_num,
            "question_num": q_idx, "subtype": subtype, "measure": measure,
            "prompt": prompt, "options": options,
            "stimulus_text": stimulus_text, "stimulus_anchor": stimulus_anchor,
            "figure_refs": figure_refs, "needs_vision": needs_vision,
            "inline_gif_targets": inline_gif_targets,
            "answer_table_image": answer_table_image,
            "source_path": htm_name, "source_anchor": "QST" + str(qst_id),
            "base_slug": base_slug, "has_numeric_box": has_numeric_box,
            "has_qc_table": has_qc_table,
        })
    return out


def detect_figures(question):
    """Public wrapper for the per-question figure list."""
    return list(question.get("figure_refs") or [])


# Stage C: post-process (answer-key + numeric + cluster wiring) -----------


def attach_answer_keys(questions, answer_map):
    paired = 0
    for q in questions:
        label = answer_map.get(q["qst_id"])
        if label is not None:
            q["correct_label"] = label
            paired += 1
        else:
            q["correct_label"] = None
    return paired


def derive_correct_flags(question):
    label = (question.get("correct_label") or "").strip()
    options = question.get("options", [])
    if not label or not options:
        return options
    if re.match(r"^[A-Za-z](?:\s*,\s*[A-Za-z])*$", label):
        letters = {p.strip().upper() for p in label.split(",")}
        for o in options:
            o["is_correct"] = o["label"].upper() in letters
    return options


def numeric_answer_dict(question):
    label = (question.get("correct_label") or "").strip()
    if not label:
        return None
    if "/" in label and not re.search(r"[A-Za-z\\]", label):
        parts = [p.strip() for p in label.split("/")]
        if len(parts) == 2:
            try:
                return {
                    "numerator": int(parts[0]),
                    "denominator": int(parts[1]),
                    "mode": "fraction",
                }
            except ValueError:
                pass
    val = parse_numeric_value(label)
    if val is not None:
        return {"exact_value": val, "mode": "decimal"}
    return None


# Quant subtype recovery using the answer-key signal --------------------

_NUMERIC_LABEL_RE = re.compile(
    r"""^\s*
        (?:
          [-\u2010\u2011\u2012\u2013\u2014\u2212]?\d+(?:[,\d]*)?(?:\.\d+)?  # 60, 1,234, 3.14, −80
          (?:\s*/\s*[-\u2010\u2011\u2012\u2013\u2014\u2212]?\d+(?:\.\d+)?)?  # /5 (fraction)
          | [-\u2010\u2011\u2012\u2013\u2014\u2212]?\d+(?:\.\d+)?\s*%        # 12.5%
          | \\?\$\d+[\d,.\s]*                                                 # $1,234
        )
        (?:\s+or\s+\S+.*)?               # "1/2 or 0.5"
        \s*$
    """,
    re.VERBOSE,
)
_LETTER_LIST_RE = re.compile(r"^[A-G](?:\s*[,\s]\s*[A-G]){0,6}$")
_QC_LETTER_RE = re.compile(r"^[A-D]$")


def _looks_like_qc_options(options):
    """Return True if the option text matches the four QC boilerplate phrases."""
    if not options or len(options) != 4:
        return False
    seen = set()
    for o in options:
        t = (o.get("text") or "").lower()
        for phrase in _QC_OPTION_PHRASES:
            if phrase in t:
                seen.add(phrase)
                break
    return len(seen) >= 3  # 3-of-4 is enough (publisher sometimes paraphrases)


def reclassify_subtype_from_answer_key(question):
    """Use the answer-key text to recover the correct subtype.

    Why this exists: the parser can guess wrong about subtype when the
    publisher renders the stem entirely as an image (so the body text has
    no "Quantity A" / "indicate all" cue). The answer-key chapter is
    deterministic, so we can use its label format to back-fill::

      correct_label = "60", "1/5", "$1,234"  -> numeric_entry
      correct_label = "B, E"                 -> mcq_multi
      correct_label = "B" + 4 QC option phrases -> qc

    The function mutates the question in place and returns the new subtype.
    """
    label = (question.get("correct_label") or "").strip()
    sub = question.get("subtype", "")
    options = question.get("options") or []
    base_slug = question.get("base_slug") or ""

    # Verbal subtypes ride on chapter slug; only quant gets reclassified.
    if base_slug not in QUANT_DRILL_SLUGS:
        return sub

    # 1) Multi-letter answer key  -> mcq_multi
    if "," in label and _LETTER_LIST_RE.match(label.replace(" ", "")):
        if sub != "mcq_multi":
            question["subtype"] = "mcq_multi"
        # Re-derive correctness so each listed letter flips on
        derive_correct_flags(question)
        return question["subtype"]

    # 2) Numeric / fraction / dollar answer key  -> numeric_entry
    if label and _NUMERIC_LABEL_RE.match(label):
        # Belt-and-suspenders: only flip when the current options aren't
        # MCQ-shaped (5 well-formed lettered options). If we have 5 real
        # options, the numeric-looking key is just an option *value*
        # (e.g. mcq_single where the answer happens to be "60").
        if len(options) >= 4 and all(
                (o.get("text") or "").strip()
                and not (o.get("text") or "").strip().startswith("[img:")
                for o in options):
            return sub
        if sub != "numeric_entry":
            question["subtype"] = "numeric_entry"
            # Numeric entry has no MCQ options; clear any leftovers.
            question["options"] = []
            question["has_numeric_box"] = True
            # Replace stem-image figure_refs that were really the
            # numeric-entry box (351, 112) with nothing — they aren't
            # diagrams and shouldn't reach the renderer.
            kept = []
            for fr in question.get("figure_refs") or []:
                if fr.get("filename") in NUMERIC_ENTRY_BOX_FILES:
                    continue
                kept.append(fr)
            question["figure_refs"] = kept
        # Stash the parsed numeric form for the runtime grader. Do this
        # whether or not we changed subtype: a question that was already
        # typed as numeric_entry by the parser still needs its answer
        # parsed into a structured form.
        question["numeric_answer"] = numeric_answer_dict(question)
        return question["subtype"]

    # 3) QC option phrasing  -> qc (catches stems rendered as a stem-only
    #    image where "Quantity A" never appears in the text)
    if _looks_like_qc_options(options) and _QC_LETTER_RE.match(label or ""):
        if sub != "qc":
            question["subtype"] = "qc"
        return question["subtype"]

    # 4) Single-letter answer + non-5 option count + plural-verb stem
    #    -> mcq_multi where only one option happens to be correct.
    #    (Princeton's "For which of the following values of x is..."
    #    pattern with 4 or 6 options falls into this bucket.)
    if (sub == "mcq_single"
            and re.match(r"^[A-G]$", (label or "").strip())
            and len(options) in (3, 4, 6, 7, 8, 9, 10)):
        prompt_text = (question.get("prompt") or "").lower()
        if re.search(r"(for|of)\s+which\s+of\s+the\s+", prompt_text) \
                or re.search(r"which\s+of\s+the\s+(weeks|values|integers|"
                             r"following\s+(values|numbers|integers|"
                             r"factors|multiples|are))",
                             prompt_text):
            question["subtype"] = "mcq_multi"
            derive_correct_flags(question)
            return question["subtype"]

    return sub


# Stage D: validation gates -----------------------------------------------


SUBTYPE_OPTION_COUNTS = {
    "mcq_single": (5,),
    # Multi-select can be anywhere from 3 to 10 in Princeton's bank
    # (qst890: x^2+x-20=0 multi-select has 8 numeric options; cgd4
    # week-which has 4 options). The corpus max is 10 so we cap there.
    "mcq_multi": (3, 4, 5, 6, 7, 8, 9, 10),
    "qc": (4,),
    "tc": (5, 6, 9),  # 1-blank=5, 2-blank=6, 3-blank=9
    "se": (6,),
    "rc_single": (5,),
    "rc_multi": (3,),
    "rc_select_passage": (0,),  # answer is a sentence from the passage
    "data_interp": (5,),
    "numeric_entry": (0,),
}

GATE_NAMES = [
    "G1_option_count", "G2_distractor_unique", "G3_correct_count",
    "G4_answer_key_paired", "G5_latex_well_formed", "G6_rc_cluster_coherent",
    "G7_numeric_parseable", "G8_figure_attached_when_referenced",
    "G9_qc_quantity_labels", "G10_prompt_length_sane",
    "G11_single_correct_adversarial", "G12_dedup_against_existing",
    "G13_image_buckets_resolved",
]


def _g1_option_count(item):
    sub = item.get("subtype", "")
    n = len(item.get("options", []))
    expected = SUBTYPE_OPTION_COUNTS.get(sub)
    if expected is None:
        return False, "unknown_subtype"
    if sub in ("tc", "se") and n == 0 and item.get("needs_vision"):
        return False, sub + "_pending_vision"
    return (n in expected), sub + "=" + str(n)


def _g2_distractor_unique(item):
    opts = item.get("options", [])
    if not opts:
        return True, "no_options"
    norms = [normalise_option_text(o.get("text", "")) for o in opts]
    norms = [n for n in norms if n]
    if not norms:
        return True, "options_empty_text"
    return (len(set(norms)) == len(norms)), str(len(set(norms))) + "/" + str(len(norms))


def _g3_correct_count(item):
    sub = item.get("subtype", "")
    opts = item.get("options", [])
    n_correct = sum(1 for o in opts if o.get("is_correct"))
    if sub in ("mcq_single", "rc_single", "qc", "data_interp"):
        return (n_correct == 1), "correct=" + str(n_correct)
    if sub == "se":
        if not opts and item.get("needs_vision"):
            return False, "se_pending_vision"
        return (n_correct == 2), "correct=" + str(n_correct)
    if sub == "tc":
        if item.get("needs_vision"):
            return False, "tc_pending_vision"
        return (n_correct >= 1), "correct=" + str(n_correct)
    if sub == "rc_multi":
        return (1 <= n_correct <= 3), "correct=" + str(n_correct)
    if sub == "mcq_multi":
        return (1 <= n_correct), "correct=" + str(n_correct)
    if sub in ("numeric_entry", "rc_select_passage"):
        return True, "n/a"
    return False, "unhandled_subtype:" + sub


def _g4_answer_key_paired(item):
    label = (item.get("correct_label") or "").strip()
    if not label:
        return False, "no_label"
    sub = item.get("subtype", "")
    if sub == "numeric_entry":
        return True, "numeric"
    if sub == "rc_select_passage":
        # Princeton's answer key for "select-in-passage" stores the
        # answer as either (a) the literal sentence text or (b) a
        # single letter A-J that indexes into the passage sentences.
        # Both are valid — the runtime resolves the letter via passage
        # tokenisation. Accept either shape.
        if re.match(r"^[A-Ja-j]$", label):
            return True, "sentence_letter"
        return (len(label) >= 10), "sentence_len=" + str(len(label))
    if re.match(r"^[A-Za-z](?:\s*,\s*[A-Za-z])*$", label):
        opts = {o["label"].upper() for o in item.get("options", [])}
        for letter in [p.strip().upper() for p in label.split(",")]:
            if letter not in opts:
                if item.get("needs_vision"):
                    return False, "letter_pending_vision"
                return False, "letter_not_in_opts:" + letter
        return True, "letters_ok"
    # Word-list answer key (Princeton TC multi-blank). Pass if the image
    # pipeline has already flipped at least one option's ``is_correct``.
    if sub in ("tc", "se") and item.get("options"):
        n_correct = sum(1 for o in item["options"] if o.get("is_correct"))
        if n_correct >= 1:
            return True, "wordlist_matched=" + str(n_correct)
    return False, "wordlist_pending_vision"


def _g5_latex_well_formed(item):
    text = (item.get("prompt", "") or "") + "\n" + (item.get("explanation", "") or "")
    ok, defects = latex_balance_check(text)
    return ok, ",".join(defects) if defects else "ok"


def _g6_rc_cluster_coherent(item):
    sub = item.get("subtype", "")
    if not sub.startswith("rc_"):
        return True, "n/a"
    anchor = item.get("stimulus_anchor", "")
    if not anchor:
        return False, "missing_passage"
    return True, "ok"


def _g7_numeric_parseable(item):
    if item.get("subtype") != "numeric_entry":
        return True, "n/a"
    payload = numeric_answer_dict(item)
    if payload is None:
        return False, "unparseable"
    return True, payload.get("mode", "?")


def _g8_figure_attached_when_referenced(item):
    refs = item.get("figure_refs", [])
    if not refs:
        return True, "no_figure"
    if all(r.get("filename") for r in refs):
        return True, "refs=" + str(len(refs))
    return False, "missing_filename"


def _g9_qc_quantity_labels(item):
    if item.get("subtype") != "qc":
        return True, "n/a"
    prompt = item.get("prompt", "") or ""
    has_a = "Quantity A" in prompt
    has_b = "Quantity B" in prompt
    if has_a and has_b:
        return True, "a=True,b=True"
    # Stem-as-image QC: when the publisher renders the entire QC stem
    # (including the "Quantity A:" / "Quantity B:" labels) as a single
    # GIF (qst526, qst534), we get a stem image in figure_refs and the
    # four boilerplate options. That's a legitimate QC; the gate is
    # "labels exist somewhere", not "labels are in the prompt text".
    if not prompt and item.get("figure_refs"):
        return True, "stem_in_image"
    return False, "a=" + str(has_a) + ",b=" + str(has_b)


def _g10_prompt_length_sane(item):
    n = len(item.get("prompt", "") or "")
    # Some Princeton questions render the entire stem as a GIF (an
    # equation, a number-line, a coordinate-plane diagram). The image is
    # caught by ``figure_refs`` (kind=diagram) or ``inline_gif_targets``,
    # so a near-empty prompt is fine — the renderer shows the image.
    has_stem_image = bool(item.get("figure_refs")) or bool(
        item.get("inline_gif_targets"))
    # Pure-math questions rendered from inline-math GIFs come out as
    # short expressions like "sqrt(81 + 9) =" or "(x + 2)^{2} = 0". The
    # 30-char floor was tuned for prose word problems and unfairly fails
    # these. If the prompt looks like a math expression (contains an
    # equals sign or a recognisable math operator), accept length >= 4.
    if not has_stem_image and n > 0 and n < 30:
        if re.search(r"[=≠<>≤≥+\-/×÷^]|sqrt|frac|\*\*|\(\d", item.get("prompt", "")):
            if n >= 4:
                return True, "len=" + str(n) + "/math"
    floor = 0 if has_stem_image else 30
    return (floor <= n <= 4000), "len=" + str(n)


def _g11_single_correct_adversarial(item):
    sub = item.get("subtype", "")
    if sub not in ("mcq_single", "tc", "rc_single", "qc"):
        return True, "n/a"
    opts = item.get("options", [])
    n_correct = sum(1 for o in opts if o.get("is_correct"))
    if sub == "tc" and item.get("needs_vision"):
        return False, "tc_pending_vision"
    # Multi-blank TC has n_correct == #blanks (2 for 2-blank, 3 for
    # 3-blank). Detect by inspecting option labels (blank1_*, blank2_*).
    if sub == "tc":
        blanks = set()
        for o in opts:
            label = o.get("label", "")
            if "_" in label:
                blanks.add(label.split("_", 1)[0])
        if len(blanks) >= 2:
            return (n_correct == len(blanks)), \
                "correct=" + str(n_correct) + "/blanks=" + str(len(blanks))
    return (n_correct == 1), "correct=" + str(n_correct)


def _g12_dedup_against_existing(item, existing):
    """Phase 0 stub: prompt-prefix dedup against an in-memory dict.

    Stage E will replace ``existing`` with a live DB lookup restricted to
    ``source IN ('princeton_2012', 'manhattan_5lb_2018', 'imported')``.
    """
    prompt = (item.get("prompt") or "")[:120].lower().strip()
    if not prompt:
        return True, "empty"
    if prompt in existing:
        return False, "duplicate"
    return True, "unique"


def _g13_image_buckets_resolved(item):
    """G13: every image ref must have a known kind.

    After the image pipeline runs, ``figure_refs`` should hold only
    ``kind in {'diagram','chart'}`` entries; raw ``[img:...]``
    placeholders, ``inline_gif_targets``, and ``answer_table_image``
    must all have been rendered to text/HTML and dropped.

    For questions that were extracted *without* having had the pipeline
    run (deterministic-only test runs), we treat the gate as ``True`` if
    no images were attached at all and ``False`` if any image attachment
    is still in pre-pipeline shape.
    """
    # No image attachments at all → trivially ok.
    has_any = (
        bool(item.get("figure_refs"))
        or bool(item.get("inline_gif_targets"))
        or bool(item.get("answer_table_image"))
    )
    blob = (item.get("prompt") or "")
    for o in item.get("options") or []:
        blob += "\n" + (o.get("text") or "")
    if not has_any and "[img:" not in blob:
        return True, "no_images"

    # Inline-gif targets and answer-table image must be cleared by the
    # pipeline; if they are still present we cannot ship the question.
    if item.get("inline_gif_targets"):
        return False, "inline_gif_target_unrendered:" + str(
            len(item["inline_gif_targets"]))
    if item.get("answer_table_image"):
        return False, "answer_table_image_unrendered"
    if "[img:" in blob:
        return False, "raw_img_placeholder"

    # Surviving figure_refs need a known kind.
    for fr in item.get("figure_refs") or []:
        kind = fr.get("kind")
        if kind not in ("diagram", "chart"):
            return False, "figure_kind_missing:" + str(kind)
    return True, "ok"


def run_validation_gates(item, existing_prefixes=None):
    """Run all 12 gates on a single item; return verdict dict."""
    existing_prefixes = existing_prefixes or {}
    results = []
    results.append(("G1_option_count", _g1_option_count(item)))
    results.append(("G2_distractor_unique", _g2_distractor_unique(item)))
    results.append(("G3_correct_count", _g3_correct_count(item)))
    results.append(("G4_answer_key_paired", _g4_answer_key_paired(item)))
    results.append(("G5_latex_well_formed", _g5_latex_well_formed(item)))
    results.append(("G6_rc_cluster_coherent", _g6_rc_cluster_coherent(item)))
    results.append(("G7_numeric_parseable", _g7_numeric_parseable(item)))
    results.append(("G8_figure_attached_when_referenced",
                    _g8_figure_attached_when_referenced(item)))
    results.append(("G9_qc_quantity_labels", _g9_qc_quantity_labels(item)))
    results.append(("G10_prompt_length_sane", _g10_prompt_length_sane(item)))
    results.append(("G11_single_correct_adversarial",
                    _g11_single_correct_adversarial(item)))
    results.append(("G12_dedup_against_existing",
                    _g12_dedup_against_existing(item, existing_prefixes)))
    results.append(("G13_image_buckets_resolved",
                    _g13_image_buckets_resolved(item)))
    failed = [name for name, (ok, _) in results if not ok]
    details = {name: detail for name, (_, detail) in results}
    return {
        "passed": len(failed) == 0,
        "failed_gates": failed,
        "details": details,
        "n_total": len(results),
        "n_failed": len(failed),
    }


# Stage A entry points (orchestration) ------------------------------------


def extract_section(epub_path, section_slug):
    """Pull every drill chapter whose slug or base_slug matches `section_slug`."""
    chapters = enumerate_chapters(epub_path)
    drill_chapters = [
        c for c in chapters
        if c["role"] == "drill" and (
            c["slug"] == section_slug or c["base_slug"] == section_slug
        )
    ]
    if not drill_chapters:
        return [], {}
    base = drill_chapters[0]["base_slug"]
    ans_slug = QUANT_ANSWER_SLUG.get(base) or VERBAL_ANSWER_SLUG.get(base)
    if ans_slug is None:
        return [], {}
    ans_chapters = [c for c in chapters if c["slug"] == ans_slug]
    questions = []
    answer_map = {}
    with zipfile.ZipFile(epub_path) as z:
        for ch in drill_chapters:
            qs = parse_drill_chapter(z, ch["path"])
            questions.extend(qs)
        for ch in ans_chapters:
            answer_map.update(parse_answer_chapter(z, ch["path"]))
    attach_answer_keys(questions, answer_map)
    for q in questions:
        # First derive correctness with whatever the parser guessed for
        # subtype, then use the answer-key signal to recover the correct
        # subtype (mcq_multi / numeric_entry / qc) for items the publisher
        # rendered as stem-image-only. Re-derive correctness afterwards
        # so multi-letter answers actually flip the right options.
        derive_correct_flags(q)
        reclassify_subtype_from_answer_key(q)
        derive_correct_flags(q)
    return questions, answer_map


# CLI ---------------------------------------------------------------------


def _phase0_dry_run(section, epub_path, limit):
    print("== Phase 0 dry run: section=" + section + " ==")
    questions, answer_map = extract_section(epub_path, section)
    if limit:
        questions = questions[:limit]
    print("extracted " + str(len(questions)) + " question(s); answer_map has "
          + str(len(answer_map)) + " entries.")
    sub_counts = Counter(q["subtype"] for q in questions)
    drill_counts = Counter(q["drill_num"] for q in questions)
    print("subtype: " + str(dict(sub_counts)))
    print("per-drill counts: " + str(dict(drill_counts)))

    existing_prefixes = {}
    gate_pass = Counter()
    gate_fail = Counter()
    failures = []
    for q in questions:
        verdict = run_validation_gates(q, existing_prefixes)
        for name in GATE_NAMES:
            if name in verdict["failed_gates"]:
                gate_fail[name] += 1
            else:
                gate_pass[name] += 1
        if not verdict["passed"]:
            failures.append({
                "qst_id": q["qst_id"], "drill": q["drill_num"],
                "subtype": q["subtype"], "needs_vision": q.get("needs_vision"),
                "n_options": len(q.get("options", [])),
                "correct_label": q.get("correct_label"),
                "failed_gates": verdict["failed_gates"],
                "details": verdict["details"],
            })

    print()
    print("Per-gate pass / fail:")
    for name in GATE_NAMES:
        p = gate_pass[name]
        f = gate_fail[name]
        total = p + f
        rate = (str(int(p / total * 100)) + "%") if total else "n/a"
        print("  " + name.ljust(42) + "  pass " + str(p).rjust(4)
              + " / fail " + str(f).rjust(4) + "  (" + rate + ")")

    n_clean = sum(1 for q in questions
                  if not run_validation_gates(q, {})["failed_gates"])
    print()
    print("Clean (all gates pass): " + str(n_clean) + " / " + str(len(questions)))

    if failures:
        print()
        print("Sample failures (first 5):")
        for fl in failures[:5]:
            print("  qst" + str(fl["qst_id"]) + " drill" + str(fl["drill"])
                  + " sub=" + fl["subtype"]
                  + " needs_vision=" + str(fl["needs_vision"])
                  + " n_opt=" + str(fl["n_options"])
                  + " ans=" + repr(fl["correct_label"]))
            for g in fl["failed_gates"]:
                print("     - " + g + ": " + str(fl["details"].get(g, "?")))

    rc_qs = [q for q in questions if q["subtype"].startswith("rc_")]
    if rc_qs:
        clusters = defaultdict(list)
        for q in rc_qs:
            anchor = q.get("stimulus_anchor", "") or "<no_anchor>"
            clusters[anchor].append(q["qst_id"])
        sizes = Counter(len(v) for v in clusters.values())
        print()
        print("RC clusters: " + str(len(clusters)) + "; sizes: " + str(dict(sizes)))

    qst_ids = [q["qst_id"] for q in questions]
    if len(set(qst_ids)) != len(qst_ids):
        dup = [k for k, v in Counter(qst_ids).items() if v > 1]
        print()
        print("!!! QST id collisions: " + str(dup))

    if len(questions) == 0:
        return 1
    fail_rate = (len(questions) - n_clean) / len(questions)
    return 0 if fail_rate <= 0.30 else 1


def main():
    parser = argparse.ArgumentParser(description="Princeton extractor")
    parser.add_argument("--section", default="tcd1",
                        help="drill slug (e.g. tcd1, rcd, pid) for Phase 0")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap items after extraction (Phase 0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse + validate, no DB writes (Phase 0/1)")
    parser.add_argument("--import-db", action="store_true",
                        help="(Phase 3, not implemented) write to DB")
    parser.add_argument("--epub-path", default=EPUB_PATH)
    args = parser.parse_args()

    if args.import_db:
        print("--import-db is reserved for Phase 3; not implemented yet.")
        return 2

    return _phase0_dry_run(args.section, args.epub_path, args.limit)


if __name__ == "__main__":
    sys.exit(main() or 0)