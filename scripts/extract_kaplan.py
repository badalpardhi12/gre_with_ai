"""
Kaplan GRE Prep Plus 2024 (EPUB) extractor.

Stage layout (per `.claude/plans/kaplan-extraction.md`):

    A  parse_chapter / split_into_blocks / parse_answer_key_ol /
       parse_explanations / detect_rc_groups
       --- pure stdlib + bs4
    B  vision repair (Stage B): Sonnet transcribes inline math glyphs
       (`<img class="inline">`) to LaTeX with prompt caching.
    C  post-process: money cleanup, latex repair, numeric parse
    D  validation gates (V1 - V14; see validators/kaplan.py)
    E  persistence to DB                    --- Phase 4 (not in this file)

Phase 0 entry point::

    venv/bin/python scripts/extract_kaplan.py --dry-run --chapter 5
    venv/bin/python scripts/extract_kaplan.py --dry-run --chapter 11
    venv/bin/python scripts/extract_kaplan.py --dry-run --chapters 5,11

Phase 0 prints per-gate pass/fail counts and writes a JSON dump of the
parsed items to `data/extracted/kaplan/phase0_<chapter>.json`. No DB
writes happen during dry-run.

The `--vision` flag enables the Sonnet glyph transcription pass
(Stage B). Without it, inline math glyphs are left in place as
`<img class="inline">` tags (the existing renderer can still display
them, but text search and screen readers won't see the math).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup, NavigableString, Tag


# ── Paths & constants ────────────────────────────────────────────────

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The EPUB lives in the main checkout's gitignored `data/ebooks/`. When
# this script runs from a worktree, walk up from PROJECT_ROOT to find it.
# On CI / fresh clones the EPUB isn't present at all; `_resolve_epub_path`
# returns None and any caller that actually needs the path (the CLI) must
# check before use. The module-level `EPUB_PATH = _resolve_epub_path()`
# assignment used to raise at import time, which broke pytest collection
# of tests/test_extract_kaplan.py in environments without the EPUB.
def _resolve_epub_path() -> Optional[str]:
    """Find the Kaplan EPUB by walking up from the worktree to the
    main checkout. Returns None if no candidate directory contains a
    Kaplan-prefixed .epub — callers that need the path must check and
    raise their own error with whatever context they have."""
    candidates: List[str] = []
    local = os.path.join(PROJECT_ROOT, "data", "ebooks")
    candidates.append(local)
    # If we're in a worktree, the main checkout is three levels up
    # (.claude/worktrees/<name>/...).
    parts = PROJECT_ROOT.split(os.sep)
    if ".claude" in parts:
        idx = parts.index(".claude")
        main_root = os.sep.join(parts[:idx])
        candidates.append(os.path.join(main_root, "data", "ebooks"))
    for d in candidates:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.startswith("(Kaplan") and f.endswith(".epub"):
                return os.path.join(d, f)
    return None


EPUB_PATH = _resolve_epub_path()
EXTRACT_DIR = os.path.join(PROJECT_ROOT, "data", "extracted", "kaplan")
DUMP_PATH = os.path.join(EXTRACT_DIR, "kaplan_2024.json")
PHASE0_DIR = EXTRACT_DIR  # phase 0 dumps land here as phase0_<ch>.json
GLYPH_CACHE_PATH = os.path.join(EXTRACT_DIR, "glyph_latex_cache.json")
SOURCE_TAG = "kaplan_2024"

# Practice-bearing chapters, per the plan's TOC analysis.
CHAPTER_NAMES = {
    "chapter04": "Verbal Foundations and Content Review",
    "chapter05": "Text Completion",
    "chapter06": "Sentence Equivalence",
    "chapter07": "Reading Comprehension",
    "chapter08": "Verbal Practice Sets",
    "chapter10": "Quant Foundations",
    "chapter11": "Arithmetic - Ratios and Math Formulas",
    "chapter12": "Algebra",
    "chapter13": "Word Problems",
    "chapter14": "Statistics",
    "chapter15": "Geometry",
    "chapter16": "Coordinate Geometry",
    "chapter17": "Probability and Combinatorics",
    "chapter18": "Counting and Sequences",
    "chapter19": "Quant Practice Sets",
    "chapter20": "Quantitative Comparison",
    "chapter21": "Problem Solving",
    "chapter22": "Numeric Entry",
    "chapter23": "Data Interpretation",
    "chapter24": "Mixed Practice",
    "chapter25": "AWA Issue",
    "chapter26": "AWA Argument",
}

# Verbal vs quant by chapter (used to pick subtype defaults when
# heading text is ambiguous).
VERBAL_CHAPTERS = {
    "chapter03", "chapter04", "chapter05", "chapter06", "chapter07",
    "chapter08",
}
QUANT_CHAPTERS = {
    "chapter09", "chapter10", "chapter11", "chapter12", "chapter13",
    "chapter14", "chapter15", "chapter16", "chapter17", "chapter18",
    "chapter19", "chapter20", "chapter21", "chapter22", "chapter23",
    "chapter24",
}

# Cap for explanation length to keep DB rows reasonable for the wxPython
# renderer (per plan section 6).
MAX_EXPLANATION_BYTES = 4096

# Max question-prompt length sanity bound (prevents pathological items
# that swallow whole chapters from going through).
MAX_PROMPT_BYTES = 8192


# ── Data classes ────────────────────────────────────────────────────

@dataclass
class RawOption:
    label: str            # "A", "B", "blank1_A", ...
    text: str             # plain or LaTeX-bearing text
    is_correct: bool = False


@dataclass
class RcGroup:
    q_start: int
    q_end: int
    passage_html: str     # HTML content of the passage block
    kind: str = "passage"  # passage | chart | graph | table | argument
    figure_images: List[str] = field(default_factory=list)  # chart/graph asset filenames


@dataclass
class RawItem:
    chapter_id: str
    section_title: str
    measure: str          # verbal | quant
    subtype: str          # mcq_single, qc, numeric_entry, tc, se, rc_*
    q_number: int         # absolute question number within the practice set
    prompt: str           # HTML (after Stage B substitutions when --vision)
    options: List[RawOption] = field(default_factory=list)
    explanation: str = ""
    correct_label: str = ""   # raw answer-key text e.g. "C, D"
    explanation_label: str = ""  # answer label parsed from explanation header
    difficulty_band: Optional[str] = None  # Basic | Intermediate | Advanced
    has_figure: bool = False
    figure_image: Optional[str] = None  # filename of the non-inline image
    figure_caption: str = ""
    rc_group_key: Optional[Tuple[int, int]] = None  # (q_start, q_end) cluster id
    numeric_value: Optional[str] = None      # printed numeric answer text
    inline_glyph_files: List[str] = field(default_factory=list)
    source_ref: str = ""     # e.g. "kaplan_2024:chapter11:set1:q7"


@dataclass
class PracticeBlock:
    chapter_id: str
    chapter_title: str
    section_title: str
    measure: str
    set_index: int                # 1, 2, ... within the chapter
    rc_groups: List[RcGroup] = field(default_factory=list)
    items: List[RawItem] = field(default_factory=list)


# ── Stage A — deterministic EPUB parse ──────────────────────────────

# Compiled patterns -------------------------------------------------

_PRACTICE_HEADING_RE = re.compile(r"\bPractice Set\b", re.I)
_PRACTICE_SET_BARE_RE = re.compile(
    r"^[A-Za-z][\w \-’',&/]*Practice Set(\s+\d+)?\s*$", re.I
)
_ANSWER_KEY_RE = re.compile(r"Practice Set\s*\d*\s*Answer Key\b", re.I)
_EXPLANATIONS_RE = re.compile(
    r"Practice Set\s*\d*\s*Answers and Explanations\b", re.I,
)

_RC_GROUP_RE = re.compile(
    r"Questions?\s+(\d+)\s*(?:[\u2013\u2014\-]|to|and|through|–|—)\s*"
    r"(\d+)?\s+"
    r"(?:(?:are|is)\s+based\s+on|refer\s+to|relate\s+to)"
    r"\s+the\s+(?:following\s+)?"
    r"(passage|chart|graph|table|figure|argument|stimulus|graphs?|charts?|tables?)",
    re.I,
)

_RC_SOLO_RE = re.compile(
    r"Question\s+(\d+)\s+"
    r"(?:is\s+based\s+on|refers?\s+to)"
    r"\s+the\s+(?:following\s+)?"
    r"(passage|chart|graph|table|figure|argument|stimulus)",
    re.I,
)

# Used to parse `<p class="tx1-1"><b>3. C, D</b></p>` headers.
_EXPL_HEADER_RE = re.compile(
    r"^\s*(\d+)\s*\.\s*(.+?)\s*$",
    re.S,
)

_BLANK_RE = re.compile(r"\(([ivx]+)\)\s*_+", re.I)

# Page-prefixed glyph filenames (e.g. "352b.jpg", "p65a.jpg") — anything
# that isn't a one-letter option-bullet glyph or a QC oval glyph.
_OPTION_LETTER_GLYPH_RE = re.compile(
    r"^("
    r"[a-f]\.jpg|"          # plain bullet glyphs a.jpg .. f.jpg
    r"s-[a-f]\.jpg|"        # ch19 style s-a.jpg .. s-f.jpg
    r"g[a-f]\.jpg|"         # mixed-set ga.jpg .. gd.jpg
    r"abcd\.jpg|"           # a single block of A/B/C/D ovals (ch16/19)
    r"37[a-f]\.jpg"         # QC option ovals 37a..37d
    r")$",
    re.I,
)


def _is_option_letter_glyph(src: str) -> bool:
    """Return True for the publisher's pre-rendered option-bullet glyphs
    (the small letters or QC ovals that just label a choice)."""
    if not src:
        return False
    return bool(_OPTION_LETTER_GLYPH_RE.match(src))


_DIAGRAM_REF_RE = re.compile(
    r"\b(in|from|on)\s+the\s+(diagram|figure|graph|chart|picture|image)"
    r"(\s+(above|below|shown))?",
    re.I,
)


def _li_text_lower(li: Tag) -> str:
    """Plain-text dump of an <li> for reference-detection. Skips nested
    option-row paragraphs (hang-1*) since options never reference
    figures."""
    parts: List[str] = []
    for child in li.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        cls = child.get("class") or []
        if any(c.startswith("hang-") for c in cls):
            continue
        parts.append(child.get_text(" "))
    return re.sub(r"\s+", " ", "".join(parts)).strip().lower()


def _reattach_trailing_figures(ol: Tag) -> None:
    """Move a trailing ``<p class="txc"><img/></p>`` figure from one
    ``<li>`` to the next when the next li's text references "the diagram
    above" / "the figure above" and the current li does not.

    This fixes a publisher-layout artifact in chapter15 where a print
    layout placed the figure for q4 inside q3's <li> tag (defect 'a').
    """
    lis = ol.find_all("li", recursive=False)
    for i, li in enumerate(lis[:-1]):
        # Find the LAST direct-child <p class="txc"> with a single image.
        last_txc: Optional[Tag] = None
        for child in reversed(list(li.children)):
            if isinstance(child, NavigableString):
                if str(child).strip():
                    break
                continue
            if not isinstance(child, Tag):
                continue
            cls = child.get("class") or []
            if child.name == "p" and "txc" in cls:
                imgs = child.find_all("img")
                if len(imgs) == 1:
                    last_txc = child
                break
            # Hit a non-figure paragraph → no trailing figure.
            break
        if last_txc is None:
            continue
        # Does the current li's narrative text already reference a figure?
        cur_text = _li_text_lower(li)
        if _DIAGRAM_REF_RE.search(cur_text):
            continue   # current li actually wants this figure
        # Does the NEXT li reference a figure but lack one of its own?
        nxt = lis[i + 1]
        nxt_text = _li_text_lower(nxt)
        if not _DIAGRAM_REF_RE.search(nxt_text):
            continue
        nxt_has_figure = False
        for c in nxt.children:
            if isinstance(c, Tag) and c.name == "p":
                cl = c.get("class") or []
                if "txc" in cl and c.find_all("img"):
                    nxt_has_figure = True
                    break
        if nxt_has_figure:
            continue
        # Move it.
        last_txc.extract()
        nxt.insert(0, last_txc)


def _is_paragraph_lone_image(p: Tag) -> bool:
    """True when a `<p>` contains exactly one image and no meaningful
    text beyond figure captions ("Note:" / "Figure not drawn to scale" /
    bare units like "degrees"). Used to distinguish a centered diagram
    from a math glyph that's embedded inline within a sentence.
    """
    if not isinstance(p, Tag):
        return False
    imgs = p.find_all("img")
    if len(imgs) != 1:
        return False
    text = _normalise_text(p).strip()
    # QC synthesised paragraphs ("Quantity A: <img>", "Quantity B: <img>")
    # are NEVER lone images — the image is the inline value of a labelled
    # quantity slot and must be rendered as inline math, not a figure.
    if re.match(r"^\s*Quantity\s+[AB]\s*:", text, re.I):
        return False
    # Strip captions and inline labels that often share the txc paragraph
    # with a figure.
    cleaned = re.sub(
        r"^(note\s*:.*?)?(figure not drawn to scale\.?)?\s*",
        "", text, flags=re.I,
    ).strip()
    # Lone units like "degrees" / "in" attached to a measurement glyph
    # don't disqualify a glyph; but a real diagram caption is usually 0
    # chars after stripping.
    return len(cleaned) <= 12


def _normalise_text(node) -> str:
    """Plain-text dump of a node, collapsing whitespace."""
    if node is None:
        return ""
    if isinstance(node, NavigableString):
        return str(node)
    return re.sub(r"\s+", " ", node.get_text(" ").strip())


def _normalise_text_with_supsub(node) -> str:
    """Like :func:`_normalise_text`, but rewrites ``<sup>`` / ``<sub>``
    children as ``^{…}`` / ``_{…}`` so exponent-bearing source text
    survives the pull from cell to plain prompt.

    Without this, ``x<sup>2</sup>`` in a QC quantity cell was rendered as
    ``x 2`` — collapsing the exponent into a separate token (defect 'b'
    on Q13). Also captures images inside the cell as ``[img:src.jpg]``
    placeholders so the QC synthesiser can hand them to Stage B vision.
    """
    if node is None:
        return ""
    if isinstance(node, NavigableString):
        return str(node)
    parts: List[str] = []

    def walk(n):
        if isinstance(n, NavigableString):
            parts.append(str(n))
            return
        if not isinstance(n, Tag):
            return
        if n.name == "sup":
            inner = "".join(_normalise_text_with_supsub(c) for c in n.children)
            inner = inner.strip()
            if inner:
                parts.append("^{" + inner + "}")
            return
        if n.name == "sub":
            inner = "".join(_normalise_text_with_supsub(c) for c in n.children)
            inner = inner.strip()
            if inner:
                parts.append("_{" + inner + "}")
            return
        if n.name == "img":
            src = (n.get("src") or "").rsplit("/", 1)[-1]
            if src and not _OPTION_LETTER_GLYPH_RE.match(src):
                parts.append(f"[img:{src}]")
            return
        if n.name == "br":
            parts.append(" ")
            return
        for c in n.children:
            walk(c)

    walk(node)
    text = "".join(parts)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _heading_text(tag: Tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ").strip())


def _is_practice_heading(text: str) -> bool:
    """Open a block on canonical 'Practice Set' (or 'Foo Practice Set N')
    headings, but NOT on 'Answer Key' / 'Answers and Explanations'."""
    if _ANSWER_KEY_RE.search(text):
        return False
    if _EXPLANATIONS_RE.search(text):
        return False
    if not _PRACTICE_HEADING_RE.search(text):
        return False
    # Reject narrative headings that happen to contain "Practice Set" in
    # an explanatory paragraph: only headings whose entire text fits the
    # pattern qualify.
    if _PRACTICE_SET_BARE_RE.match(text):
        return True
    lc = text.lower().rstrip()
    if lc.endswith("practice set"):
        return True
    # Allow trailing "practice set <digit>" (e.g. ch08 'Verbal Reasoning
    # Practice Set 1').
    if re.search(r"\bpractice set\s+\d+\s*$", lc):
        return True
    return False


def _is_answer_key_heading(text: str) -> bool:
    return bool(_ANSWER_KEY_RE.search(text))


def _is_explanation_heading(text: str) -> bool:
    return bool(_EXPLANATIONS_RE.search(text))


def _siblings_until(start: Tag, stop_predicate) -> List[Tag]:
    """Collect document-order following-siblings of `start`'s parent
    chain until a sibling matches `stop_predicate`. Returns a flat list
    of Tag nodes (NavigableStrings filtered out)."""
    out: List[Tag] = []
    cur = start.next_sibling
    while cur is not None:
        if isinstance(cur, Tag):
            if stop_predicate(cur):
                break
            out.append(cur)
        cur = cur.next_sibling
    return out


def split_into_blocks(soup: BeautifulSoup, chapter_id: str,
                      chapter_title: str) -> List[Tuple[Tag, Tag, Tag]]:
    """Find every (practice_h1, answer_key_h1, explanations_h1) triplet
    in the chapter, in document order. Returns triplets of Tag nodes.
    """
    triplets: List[Tuple[Tag, Tag, Tag]] = []
    h1s = soup.find_all("h1", class_="h1")

    i = 0
    while i < len(h1s):
        h = h1s[i]
        text = _heading_text(h)
        if _is_practice_heading(text):
            # Look ahead for the matching answer-key + explanations h1s.
            ak: Optional[Tag] = None
            expl: Optional[Tag] = None
            for j in range(i + 1, len(h1s)):
                t2 = _heading_text(h1s[j])
                if _is_practice_heading(t2):
                    # Ran into the next practice block before finding the
                    # answer key → malformed; skip this heading.
                    break
                if ak is None and _is_answer_key_heading(t2):
                    ak = h1s[j]
                elif ak is not None and _is_explanation_heading(t2):
                    expl = h1s[j]
                    break
            if ak is not None and expl is not None:
                triplets.append((h, ak, expl))
                i = h1s.index(expl) + 1
                continue
        i += 1
    return triplets


def _between(start: Tag, end: Tag) -> List[Tag]:
    """Return all Tag siblings between `start` (exclusive) and `end`
    (exclusive) using a document-order walk via `find_all_next`."""
    out: List[Tag] = []
    for node in start.find_all_next():
        if node is end:
            break
        out.append(node)
    return out


def detect_rc_groups(nodes_between: List[Tag]) -> List[RcGroup]:
    """Look for `<h3>Questions N–M are based on the passage…</h3>` (or
    `<p class="tx1-1">Questions N–M …</p>` — Kaplan's RC chapter places
    the cluster header in a paragraph, not a heading) plus any `<h3>`
    that says "passage" / "argument" / "chart" without the explicit
    Q-range (per Risk R3).
    """
    groups: List[RcGroup] = []
    for i, node in enumerate(nodes_between):
        if not isinstance(node, Tag):
            continue
        # Headings carry RC group markers in most chapters.
        is_marker_node = node.name in ("h3", "h2")
        # ch07 (and some Verbal Practice Sets) use `<p class="tx1-1">`
        # paragraphs that BEGIN with the literal "Questions N-M …" text.
        if not is_marker_node and node.name == "p":
            cls = node.get("class") or []
            if "tx1-1" in cls:
                # Be conservative: only match paragraphs that look like
                # cluster headers (start with "Question(s) ... based on").
                lead = _heading_text(node)[:120].lower()
                if re.match(r"^questions?\s+\d", lead):
                    is_marker_node = True
        if not is_marker_node:
            continue
        text = _heading_text(node)
        if not text:
            continue

        m = _RC_GROUP_RE.search(text)
        m_solo = _RC_SOLO_RE.search(text)

        kind: Optional[str] = None
        q_start: Optional[int] = None
        q_end: Optional[int] = None
        if m:
            q_start = int(m.group(1))
            q_end = int(m.group(2)) if m.group(2) else q_start
            kind = m.group(3).lower()
        elif m_solo:
            q_start = int(m_solo.group(1))
            q_end = q_start
            kind = m_solo.group(2).lower()
        else:
            # No q-range. Defer to the first <ol> we find: per Risk R3,
            # if the heading mentions passage/argument/chart and the next
            # ol has >= 2 items we treat them as one cluster.
            lc = text.lower()
            if not any(k in lc for k in ("passage", "argument", "chart",
                                         "graph", "table")):
                continue
            kind = next(k for k in ("passage", "argument", "chart",
                                    "graph", "table") if k in lc)

        # Collect the passage HTML (everything until the next <ol>).
        passage_parts: List[str] = []
        target_ol: Optional[Tag] = None
        for follow in nodes_between[i + 1:]:
            if not isinstance(follow, Tag):
                continue
            if follow.name == "ol":
                target_ol = follow
                break
            if follow.name in ("h1", "h2", "h3"):
                # Hit another heading before any ol; bail out.
                break
            if (follow.name == "p"
                    and "tx1-1" in (follow.get("class") or [])
                    and re.match(r"^questions?\s+\d",
                                 _heading_text(follow)[:120].lower())):
                # Next RC cluster marker — stop accumulating this
                # passage.
                break
            passage_parts.append(str(follow))
        passage_html = "\n".join(passage_parts).strip()

        if not passage_html:
            continue

        # If the heading didn't carry an explicit q-range, infer it from
        # the target ol's li count.
        if q_start is None and target_ol is not None:
            lis = target_ol.find_all("li", recursive=False)
            if len(lis) < 2:
                continue
            start_attr = target_ol.get("start")
            try:
                q_start = int(start_attr) if start_attr else 1
            except ValueError:
                q_start = 1
            q_end = q_start + len(lis) - 1

        if q_start is None:
            continue

        # Pull figure assets out of the passage_html. Charts/graphs/tables
        # always live in `<p class="txc">` paragraphs whose only meaningful
        # child is the asset image. We capture the filenames so the
        # persistence layer (and the markdown sampler) can re-attach them
        # as Stimulus rows of `stimulus_type='chart'`/`graph`.
        figure_images: List[str] = []
        if passage_html:
            psoup = BeautifulSoup(passage_html, "html.parser")
            for p in psoup.find_all("p", class_="txc"):
                if not _is_paragraph_lone_image(p):
                    continue
                img = p.find("img")
                src = (img.get("src") or "").rsplit("/", 1)[-1] if img else ""
                if src and not _is_option_letter_glyph(src):
                    if src not in figure_images:
                        figure_images.append(src)

        groups.append(RcGroup(
            q_start=q_start, q_end=q_end or q_start,
            passage_html=passage_html, kind=kind or "passage",
            figure_images=figure_images,
        ))
    return groups


def parse_answer_key_ol(answer_h1: Tag, stop_h1: Tag) -> Dict[int, str]:
    """The `<ol class="ol0 bold">` after the Answer Key heading carries
    one <li> per question; the li's text (or inline image filename) is
    the answer.
    """
    out: Dict[int, str] = {}
    # Find the first <ol> after answer_h1 but before stop_h1.
    ol: Optional[Tag] = None
    for node in answer_h1.find_all_next():
        if node is stop_h1:
            break
        if isinstance(node, Tag) and node.name == "ol":
            ol = node
            break
    if ol is None:
        return out
    start_attr = ol.get("start")
    try:
        cur = int(start_attr) if start_attr else 1
    except ValueError:
        cur = 1
    for li in ol.find_all("li", recursive=False):
        text = _normalise_text(li)
        if not text:
            # The whole answer is an image (e.g., a fraction). Capture
            # the image filename so Stage B can transcribe it.
            img = li.find("img")
            if img is not None:
                src = (img.get("src") or "").rsplit("/", 1)[-1]
                if src:
                    text = f"@@GLYPH:{src}@@"
        # Re-set cur for ol with a numeric li[value] (rare).
        val_attr = li.get("value")
        if val_attr:
            try:
                cur = int(val_attr)
            except ValueError:
                pass
        out[cur] = text
        cur += 1
    return out


def parse_explanations(expl_h1: Tag, stop_h1: Optional[Tag]) -> Dict[int, Dict[str, str]]:
    """Walk paragraphs after `Answers and Explanations`. Each
    `<p class="tx1-1"><b>N. label</b></p>` opens explanation N; following
    `<p class="tx1">...</p>` paragraphs (and inline glyph images) belong
    to the same N until the next opener.
    """
    out: Dict[int, Dict[str, str]] = {}
    cur_n: Optional[int] = None
    cur_label: str = ""
    cur_parts: List[str] = []

    def _flush():
        nonlocal cur_n, cur_label, cur_parts
        if cur_n is not None:
            out[cur_n] = {
                "label": cur_label,
                "html": "\n".join(cur_parts).strip(),
            }
        cur_n = None
        cur_label = ""
        cur_parts = []

    # Detector: a paragraph that opens explanation N if its text starts
    # with "N." (optionally followed by the answer label). Allow:
    #   <b>1. C, D</b>
    #   <b>3.</b><img class="inline" src="..."/>     (answer is a glyph)
    #   <b>2.</b> 60                                 (answer is plain text)
    _bare_number_re = re.compile(r"^\s*(\d+)\s*\.\s*$")

    nodes = expl_h1.find_all_next()
    for node in nodes:
        if stop_h1 is not None and node is stop_h1:
            break
        if not isinstance(node, Tag):
            continue
        # New chapter h1 — bail.
        if node.name == "h1":
            break

        cls = node.get("class") or []
        if node.name == "p" and "tx1-1" in cls:
            text = _normalise_text(node)
            # Detect a header opener: starts with "<digit>. <label>".
            # Inspect the first <b> child for the canonical "1. C, D" form.
            b = node.find("b")
            if b is not None:
                btxt = _normalise_text(b)
                m = _EXPL_HEADER_RE.match(btxt)
                if m:
                    _flush()
                    cur_n = int(m.group(1))
                    cur_label = m.group(2).strip()
                    cur_parts.append(str(node))
                    continue
                # Bare "N." opener (label sits in a sibling element such
                # as an inline glyph, a second <b>, or trailing text).
                bare_m = _bare_number_re.match(btxt)
                if bare_m:
                    _flush()
                    cur_n = int(bare_m.group(1))
                    # Try to capture the label from whatever follows the
                    # leading <b>: a sibling <b>, an <img class="inline">,
                    # or trailing plain text.
                    label_parts: List[str] = []
                    sib = b.next_sibling
                    while sib is not None:
                        if isinstance(sib, NavigableString):
                            s = str(sib).strip()
                            if s:
                                label_parts.append(s)
                        elif isinstance(sib, Tag):
                            if sib.name == "b":
                                label_parts.append(_normalise_text(sib).strip())
                            elif sib.name == "img":
                                src = (sib.get("src") or "").rsplit("/", 1)[-1]
                                if src:
                                    label_parts.append(f"@@GLYPH:{src}@@")
                            else:
                                label_parts.append(_normalise_text(sib).strip())
                        sib = sib.next_sibling
                    cur_label = " ".join(p for p in label_parts if p).strip()
                    cur_parts.append(str(node))
                    continue
            # Fallback: no bold but the paragraph starts with "N."
            m = _EXPL_HEADER_RE.match(text)
            if m and len(m.group(2)) <= 60:
                _flush()
                cur_n = int(m.group(1))
                cur_label = m.group(2).strip()
                cur_parts.append(str(node))
                continue
            # Otherwise it's a continuation paragraph.
            if cur_n is not None:
                cur_parts.append(str(node))
            continue

        # Generic continuation paragraph (tx1, tx2, txc, etc.).
        if node.name in ("p", "ul", "ol", "table", "div", "h3"):
            # Stop on a section h1 (handled above) or on a new chapter
            # heading; otherwise collect.
            if node.name == "h3":
                # h3 inside the explanation block — usually a label like
                # "Basic" / "Intermediate" / "Advanced" subdividers.
                # Don't flush; just append for context.
                if cur_n is not None:
                    cur_parts.append(str(node))
                continue
            if cur_n is not None:
                cur_parts.append(str(node))
            continue

    _flush()
    return out


# ── Item parsing within a single practice set ───────────────────────

def _li_to_options(li: Tag) -> List[RawOption]:
    """Extract option rows from an <li>'s sub-paragraphs.

    Kaplan renders option rows three ways:
      1. <p class="hang-1"><img class="inline" src="images/a.jpg"/> text</p>
         (single-blank TC, RC single, MCQ — letter glyph present).
      2. <p class="hang-2">text</p>
         (SE / some MCQ — no inline letter glyph; labels are inferred
         from position A, B, C, D, E, F).
      3. <p class="txc"><img alt="image" src="images/p65a.jpg"/></p>
         (multi-blank TC; the entire option table is one JPEG; we capture
         the filename so Stage B can transcribe it).
    """
    options: List[RawOption] = []
    auto_labels = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]
    # Kaplan uses three option-row paragraph classes: hang-1, hang-2, and
    # hang-1k (the latter for QC and DI, where the option text comes
    # FIRST and the letter glyph follows). We accept all three.
    OPTION_ROW_CLASSES = ("hang-1", "hang-2", "hang-1k")
    for p in li.find_all("p", recursive=False):
        cls = p.get("class") or []
        if not any(c in cls for c in OPTION_ROW_CLASSES):
            continue
        # Option row. The first <img class="inline"> in this paragraph
        # is the bullet letter glyph; the surrounding text is the option
        # content. The hang-1k layout puts the bullet AFTER the text.
        label = ""
        label_img = p.find("img", class_="inline")
        # Walk forward-siblings to capture an inline img that lives just
        # outside the <p> (Kaplan's hang-1k DI layout sometimes has the
        # text in the <p> and the option-letter glyph as the next sibling).
        if label_img is None:
            sib = p.next_sibling
            while sib is not None and isinstance(sib, NavigableString):
                sib = sib.next_sibling
            if (isinstance(sib, Tag) and sib.name == "img"
                    and "inline" in (sib.get("class") or [])):
                label_img = sib
        if label_img is not None:
            src = (label_img.get("src") or "").rsplit("/", 1)[-1]
            # Bullet glyph: "a.jpg" / "b.jpg" / "s-a.jpg" / "ga.jpg" /
            # "37a.jpg" / "abcd.jpg" — only the literal first letter
            # tells us the option label.
            m = re.match(r"(?:s-|g)?([a-f])\.jpg$", src, re.I)
            if m:
                label = m.group(1).upper()
            else:
                # 37a/37b/37c/37d glyphs are the QC oval bullets.
                m37 = re.match(r"37([a-d])\.jpg$", src, re.I)
                if m37:
                    label = m37.group(1).upper()
            # Strip the bullet img so it doesn't end up in option text.
            if label_img.parent is p:
                label_img.extract()
        text_parts: List[str] = []
        for child in p.children:
            if isinstance(child, NavigableString):
                text_parts.append(str(child))
            elif isinstance(child, Tag):
                text_parts.append(child.decode())
        text = re.sub(r"\s+", " ", "".join(text_parts)).strip()
        if not label and len(options) < len(auto_labels):
            # Infer label by position when the source omits the
            # letter glyph (common for `hang-2` SE option rows).
            label = auto_labels[len(options)]
        if text or label:
            options.append(RawOption(label=label or "", text=text))
    return options


def _detect_subtype_from_li(li: Tag, measure: str,
                            chapter_id: str,
                            options: List[RawOption],
                            prompt_html: str,
                            answer_key_text: Optional[str] = None) -> str:
    """Heuristic to pick a subtype for a single <li>.

    `answer_key_text` (when known) helps disambiguate quant items where
    the printed answer is short-answer / ratio / dollar amount rather
    than a single number — those route to ``mcq_short_answer`` so V8
    doesn't fail.
    """
    html = str(li).lower()
    plain = _normalise_text(li)

    # Pure-image options: TC 2/3-blank or SE table.
    txc_imgs = [p for p in li.find_all("p", class_="txc")
                if p.find("img")]
    if txc_imgs and not options:
        # Look at the prompt for blanks (i)/(ii)/(iii).
        blanks = set(_BLANK_RE.findall(plain))
        if measure == "verbal":
            if len(blanks) >= 2:
                return f"tc"  # TC multi-blank; option count fixup later
            if "_" * 3 in plain or "_________" in plain:
                return "tc"
            return "se"  # default for verbal pure-image option tables
        # Quant + a centered image with no extracted options often means
        # the option table is a single JPEG (Foundation chapters render
        # answer choices that way). BUT if the answer key is a single
        # number, the txc images are figures/inline math glyphs, not an
        # option-table. Defer to the numeric_entry / mcq_short_answer
        # branches below in that case.
        if measure == "quant":
            ak_strip = (answer_key_text or "").strip()
            ak_looks_numeric = bool(
                re.match(r"^[+-]?\d+(\.\d+)?$", ak_strip)
                or re.match(r"^[+-]?\d+/\d+$", ak_strip)
            )
            ak_looks_letter = bool(
                re.match(r"^[A-E](\s*,\s*[A-E])*$", ak_strip)
            )
            if ak_looks_letter:
                # The publisher prints letter(s) as the answer key →
                # this really is an MCQ; the txc images are the option
                # table. Stage B vision will OCR the table.
                return "mcq_single"
            if not ak_looks_numeric:
                # Free-text answer (algebraic, ratio, etc.) — let the
                # quant short-answer branch handle it.
                pass
            else:
                # Numeric answer with a centered figure → numeric_entry
                # with an attached diagram.
                return "numeric_entry"

    # RC select-the-sentence: long answer-key text that looks like a
    # passage sentence (>40 chars when truncated, ends with terminal
    # punctuation or a "..." ellipsis indicating publisher truncation).
    # MUST be checked before the numeric_entry fallback because such
    # items also have no extracted options. The stem text "Select the
    # sentence" is the strongest signal — fall back to chapter id when
    # the stem signal is missing.
    looks_like_select_sentence_stem = bool(re.search(
        r"\bselect\s+the\s+sentence\b", plain, re.I,
    ))
    ak_strip = (answer_key_text or "").strip()
    ak_looks_sentence = (
        len(ak_strip) > 40
        and " " in ak_strip
        and (
            re.search(r"[\w][.?!]\s*$", ak_strip)
            or re.search(r"(\.\s*){2,}\s*$", ak_strip)  # publisher's "..."
            or ak_strip.endswith("\u2026")             # unicode ellipsis
        )
    )
    if (not options and answer_key_text
            and (looks_like_select_sentence_stem
                 or (chapter_id in ("chapter07", "chapter08")
                     and ak_looks_sentence))):
        return "rc_select_passage"

    # MCQ-multi: stem says "Indicate all such…" / "select all that apply"
    # AND the answer key is a comma-separated list of letters AND there
    # are >=4 options. Catches quant 5-option mcq_multi items (e.g. ch17
    # q4 "more than two distinct prime factors. Indicate all such
    # numbers." — answer "B, E").
    if (options and answer_key_text and "," in answer_key_text
            and re.match(r"^[A-Za-z](\s*,\s*[A-Za-z])+\s*$",
                         answer_key_text.strip())
            and len(options) >= 4
            and re.search(r"\bindicate\b.*\ball\b|select\s+all|all\s+that",
                          plain, re.I)):
        return "mcq_multi"

    # Quant + numeric_entry: no options, prompt asks "what is", "how many",
    # "find the value", or ends with a "?".
    if measure == "quant" and not options:
        # Free-text quant answers — ratio, dollar amount, comparison —
        # belong to a short-answer subtype (V8 only validates numeric
        # parseability for `numeric_entry`, so routing here keeps the
        # validator from blocking on items where the publisher's printed
        # answer is something like "18:11" or "$50").
        if answer_key_text:
            ak = answer_key_text.strip()
            # Normalise the Unicode minus so the regexes can use ASCII -.
            ak_norm = ak.replace("\u2212", "-")
            # An answer that's a glyph image (JPEG) is almost always a
            # symbolic / LaTeX expression — route to short_answer so V8
            # doesn't fail on the placeholder text.
            if ak.startswith("@@GLYPH"):
                return "mcq_short_answer"
            looks_like_ratio = bool(re.match(r"^\d+\s*:\s*\d+$", ak_norm))
            looks_like_money = bool(re.match(r"^\$\d", ak_norm))
            looks_like_units = bool(re.search(
                r"\b(mpg|mph|kg|cm|km|miles?|inches?|feet|gallons?|ounces?|pounds?|liters?)\b",
                ak_norm, re.I,
            ))
            looks_like_comparison = bool(re.search(
                r"\bgreater\b|\bless\b|\bequal\b", ak_norm, re.I))
            looks_like_coordinate = bool(re.match(
                r"^\(\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*\)$", ak_norm,
            ))
            looks_like_algebraic = bool(re.search(
                r"[a-zA-Z]\s*[\^]|\\frac|\\sqrt|\(\s*[a-zA-Z]"
                r"|[a-zA-Z]\s*[+\-*/]\s*[a-zA-Z\d]|\\pi|\\overline"
                r"|\bin terms of\b",
                ak_norm, re.I,
            ))
            looks_like_pair = bool(re.match(
                r"^\(?-?\d+\s*,\s*-?\d+\)?$", ak_norm,
            ))
            if (looks_like_ratio or looks_like_money or looks_like_units
                    or looks_like_comparison or looks_like_coordinate
                    or looks_like_algebraic or looks_like_pair):
                return "mcq_short_answer"
            # Quant prompt explicitly asking which has greater value —
            # treat as a comparison item even if the answer key is brief.
            if re.search(
                r"which has the greater value|which is greater"
                r"|in terms of|factor",
                plain, re.I,
            ):
                return "mcq_short_answer"

        if any(k in plain.lower() for k in (
            "how many", "what is the value", "what percent",
            "how much", "find ", "value of", "evaluate",
        )) or plain.endswith("?"):
            # Could still be a QC if the prompt is split into Quantity
            # A/B lines.
            if "quantity a" in plain.lower() and "quantity b" in plain.lower():
                return "qc"
            return "numeric_entry"
        if "quantity a" in plain.lower() and "quantity b" in plain.lower():
            return "qc"
        # Default: numeric_entry for open-response quant items.
        return "numeric_entry"

    # Verbal MCQ-style: 5 options labelled A-E from a passage.
    if measure == "verbal" and options:
        if len(options) == 5:
            # If the chapter is RC, call it rc_single; otherwise tc 1-blank.
            if chapter_id in ("chapter07",):
                return "rc_single"
            if chapter_id in ("chapter05", "chapter06"):
                return "tc"
            return "rc_single"
        if len(options) == 6:
            return "se"
        if len(options) == 3:
            return "rc_multi"

    # Quant MCQ (rare in Kaplan practice sets — most are open response).
    if measure == "quant" and options:
        if len(options) == 4:
            # QC structural shape: 4 options.
            if "quantity a" in plain.lower() and "quantity b" in plain.lower():
                return "qc"
            return "mcq_single"
        if len(options) in (5, 4):
            return "mcq_single"

    # Default fallback.
    return "mcq_single" if options else "numeric_entry"


def _li_inner_html(li: Tag, drop_image_srcs: Optional[List[str]] = None) -> str:
    """Render an <li>'s inner HTML, dropping option rows (which are
    captured separately) and pure-image option tables.

    If `drop_image_srcs` is provided, every `<img>` whose filename is in
    the set is also removed (used to strip figures that have been
    promoted to `figure_image`, so the renderer doesn't show them twice).
    Even without that hint, lone-image txc paragraphs are stripped since
    they are always either a multi-blank option-table or a figure — both
    handled out-of-band by the caller.
    """
    drop_set = set(drop_image_srcs or [])
    clone = BeautifulSoup(str(li), "html.parser").li
    # Remove option-row paragraphs.
    for p in list(clone.find_all("p")):
        cls = p.get("class") or []
        if any(c in cls for c in ("hang-1", "hang-2", "hang-1k")):
            p.extract()
            continue
        # Lone-image txc paragraph → option table (TC/SE) OR figure
        # diagram. Either way, the caller handles it via Stage B vision
        # (option tables) or the `figure_image` field (diagrams). Strip
        # so the prompt HTML doesn't render it inline.
        if "txc" in cls and p.find("img"):
            if _is_paragraph_lone_image(p):
                p.extract()
                continue
    # Strip individual <img> tags whose src matches drop_image_srcs
    # (used to also remove a figure image that lives inside a non-txc
    # paragraph like `tx1-1`, e.g. ch15 q4's `<p class="tx1-1"><img>`
    # diagram).
    if drop_set:
        for img in list(clone.find_all("img")):
            src = (img.get("src") or "").rsplit("/", 1)[-1]
            if src in drop_set:
                parent = img.parent
                img.extract()
                if (parent is not None and parent.name == "p"
                        and not _normalise_text(parent).strip()
                        and not parent.find("img")):
                    parent.extract()
    parts = []
    for child in clone.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            parts.append(child.decode())
    html = "".join(parts).strip()
    return html


def _synthesise_qc_ol(nodes_between: List[Tag], soup: BeautifulSoup
                      ) -> Optional[Tag]:
    """Build a synthetic <ol> for ch16 (Quantitative Comparison).

    ch16 lays out QC items inside `<table class="table">` blocks instead
    of `<ol>`. Each QC item is a slice of cells beginning with `<N>.` in
    the leftmost cell of a row and continuing until the next numbered
    header. We extract the centered information line(s) and the two
    quantity expressions, then fabricate a <li> with two `hang-1k` option
    rows pointing at the canonical QC choices A-D so the downstream
    parser builds a `qc` item.

    Returns a synthetic <ol> Tag — appended to `nodes_between` by the
    caller — or None if no QC tables were found.
    """
    qc_tables = []
    for n in nodes_between:
        if isinstance(n, Tag) and n.name == "table":
            cls = n.get("class") or []
            if "table" in cls:
                qc_tables.append(n)
    if not qc_tables:
        return None

    ol = soup.new_tag("ol")
    ol["class"] = ["ol0"]

    # Walk all qc_tables in order, with a reference to the immediately
    # preceding <p class="tx1-1"> or <p class="txc"> as the "centered
    # info" line.
    qc_items: List[Tuple[int, str, str, str, str]] = []
    # (qnum, centered_info, quantity_a, quantity_b, prompt_extras)

    # Centered-info paragraphs are bound to the *next* numbered question,
    # not the previous one. They appear in two places: between tables as
    # `<p class="tx1-1">` and inside tables as a single colspan-spanning
    # row. We accumulate them in `info_carry` and flush into the next Q
    # the moment its numbered header row is encountered.
    info_carry: List[str] = []

    def _row_classifies(tr: Tag) -> str:
        """Categorise a row inside a QC table."""
        cells = tr.find_all(["td", "th"])
        if not cells:
            return "filler"
        first_text = _normalise_text_with_supsub(cells[0]).strip()
        if re.match(r"^\d+\s*\.?\s*$", first_text):
            return "numbered"
        non_empty_texts = [_normalise_text_with_supsub(c).strip() for c in cells]
        cnt_quantity = sum(1 for t in non_empty_texts if "Quantity" in t)
        if cnt_quantity >= 2:
            return "header"
        visible = [t for t in non_empty_texts if t]
        if not visible:
            return "filler"
        if len(visible) == 1:
            for c in cells:
                cs = c.get("colspan")
                if cs and str(cs).strip() not in ("1", ""):
                    return "info"
            if len(visible[0]) >= 4:
                return "info"
            return "filler"
        return "quantity"

    def _quantity_from_row(tr: Tag) -> Tuple[str, str]:
        """Read the Quantity A / Quantity B values from a quantity row.

        Uses sup/sub-preserving rendering and turns image cells into
        ``[img:src.jpg]`` placeholder tokens so Stage B vision can swap
        them in later. Drops the trailing answer-bullet glyph cells
        (37a-37e) that the publisher repeats on every row."""
        cells = tr.find_all(["td", "th"])
        if not cells:
            return ("", "")
        rendered = [_normalise_text_with_supsub(c) for c in cells]
        visible: List[str] = []
        for r in rendered:
            stripped = r.strip()
            if not stripped:
                continue
            if re.fullmatch(r"\[img:37[a-e]\.jpg\]", stripped, re.I):
                continue
            visible.append(stripped)
        if len(visible) >= 2:
            return (visible[0], visible[1])
        if len(visible) == 1:
            return (visible[0], "")
        return ("", "")

    for table_idx, table in enumerate(qc_tables):
        # Pull `<p class="tx1-1">` / `<p class="txc">` paragraphs sitting
        # just before this table — they're centered info for the FIRST
        # numbered Q inside the table.
        prev = table.previous_sibling
        outer_carry: List[str] = []
        while prev is not None:
            if isinstance(prev, Tag):
                if prev.name == "table":
                    cls = prev.get("class") or []
                    if "table" in cls:
                        break
                if prev.name in ("h1", "h2"):
                    break
                if prev.name == "p":
                    p_cls = prev.get("class") or []
                    if any(c in p_cls for c in ("tx1-1", "txc")):
                        text = _normalise_text_with_supsub(prev).strip()
                        if text:
                            outer_carry.insert(0, text)
            prev = prev.previous_sibling
        info_carry.extend(outer_carry)

        rows = table.find_all("tr")
        cur: Optional[Dict[str, Any]] = None
        for tr in rows:
            kind = _row_classifies(tr)
            if kind == "numbered":
                if cur is not None:
                    qc_items.append((cur["q"], cur["info"], cur["a"],
                                     cur["b"], cur["extras"]))
                first_text = _normalise_text_with_supsub(
                    tr.find_all(["td", "th"])[0]).strip()
                m = re.match(r"^(\d+)", first_text)
                qnum = int(m.group(1)) if m else 0
                cur = {"q": qnum,
                       "info": "\n".join(info_carry).strip(),
                       "a": "", "b": "", "extras": ""}
                info_carry = []
                continue
            if kind in ("header", "filler"):
                continue
            if kind == "info":
                cells = tr.find_all(["td", "th"])
                texts = [_normalise_text_with_supsub(c).strip() for c in cells]
                visible = [t for t in texts if t]
                if visible:
                    info_carry.append(" ".join(visible))
                continue
            if kind == "quantity":
                if cur is None:
                    continue
                a, b = _quantity_from_row(tr)
                if a and not cur["a"]:
                    cur["a"] = a
                if b and not cur["b"]:
                    cur["b"] = b
                continue

        if cur is not None:
            qc_items.append((cur["q"], cur["info"], cur["a"], cur["b"],
                             cur["extras"]))

    if not qc_items:
        return None

    # Build synthetic <li>s.
    qc_items.sort(key=lambda x: x[0])
    qc_items_unique: List[Tuple[int, str, str, str, str]] = []
    seen_qs: set = set()
    for tup in qc_items:
        if tup[0] in seen_qs:
            continue
        seen_qs.add(tup[0])
        qc_items_unique.append(tup)

    if qc_items_unique:
        first_q = qc_items_unique[0][0]
        ol["start"] = str(first_q)

    for q, info, a, b, extras in qc_items_unique:
        li = soup.new_tag("li")
        li["class"] = ["li-1"]
        prompt_lines: List[Tuple[str, str]] = []
        if info:
            prompt_lines.append(("info", info.strip()))
        if extras.strip():
            prompt_lines.append(("extras", extras.strip()))
        prompt_lines.append(("qa", f"Quantity A: {a or '(missing)'}"))
        prompt_lines.append(("qb", f"Quantity B: {b or '(missing)'}"))
        for kind, line in prompt_lines:
            p = soup.new_tag("p")
            p["class"] = ["tx1"]
            # Tokens like ``[img:337f.jpg]`` become real <img> children so
            # the downstream inline-glyph collector + Stage B vision pass
            # pick them up. Without this, QC quantity images would never
            # be transcribed (defect 'c'/'d': Q14 / Q15).
            for piece in re.split(r"(\[img:[^\]]+\])", line):
                if not piece:
                    continue
                m = re.match(r"\[img:([^\]]+)\]", piece)
                if m:
                    img = soup.new_tag("img")
                    img["class"] = ["inline"]
                    img["src"] = "images/" + m.group(1)
                    p.append(img)
                else:
                    p.append(NavigableString(piece))
            li.append(p)
        # Build the four canonical QC option rows so _li_to_options will
        # pick them up.
        qc_choices = [
            ("a", "Quantity A is greater."),
            ("b", "Quantity B is greater."),
            ("c", "The two quantities are equal."),
            ("d", "The relationship cannot be determined "
                  "from the information given."),
        ]
        for letter, text in qc_choices:
            opt_p = soup.new_tag("p")
            opt_p["class"] = ["hang-1k"]
            img = soup.new_tag("img")
            img["class"] = ["inline"]
            img["src"] = f"images/37{letter}.jpg"
            opt_p.append(img)
            opt_p.append(NavigableString(" " + text))
            li.append(opt_p)
        ol.append(li)
    return ol


def parse_practice_set(practice_h1: Tag, ak_h1: Tag, expl_h1: Tag,
                       chapter_id: str, chapter_title: str,
                       set_index: int) -> PracticeBlock:
    """Build a PracticeBlock from one (practice, answer_key, explanations)
    triplet."""
    section_title = _heading_text(practice_h1)
    measure = "verbal" if chapter_id in VERBAL_CHAPTERS else "quant"
    if chapter_id == "chapter08":
        # ch08 is mixed verbal practice sets.
        measure = "verbal"
    if chapter_id == "chapter19":
        measure = "quant"

    block = PracticeBlock(
        chapter_id=chapter_id,
        chapter_title=chapter_title,
        section_title=section_title,
        measure=measure,
        set_index=set_index,
    )

    # Collect the nodes between the practice h1 and the answer-key h1.
    nodes_between: List[Tag] = []
    for node in practice_h1.find_all_next():
        if node is ak_h1:
            break
        nodes_between.append(node)

    # RC groups, if any.
    block.rc_groups = detect_rc_groups(nodes_between)

    # Pre-parse the answer-key so subtype detection can disambiguate
    # short-answer / ratio / dollar quant items from numeric_entry.
    answer_key_pre = parse_answer_key_ol(ak_h1, expl_h1)

    # ch16 (Quantitative Comparison) renders QC items inside
    # `<table class="table">` blocks rather than `<ol>`. Synthesize a
    # virtual <ol> of <li>s so the rest of the parser can treat them
    # uniformly. Each QC item is a contiguous slice of table rows
    # starting at a "<N>." header cell and ending at the next header.
    if chapter_id == "chapter16":
        synthetic_ol = _synthesise_qc_ol(nodes_between, soup=BeautifulSoup(
            "<html><body></body></html>", "html.parser"))
        if synthetic_ol is not None:
            # Splice the synthetic <ol> into nodes_between so the
            # downstream walker picks it up.
            nodes_between.append(synthetic_ol)

    # Find every <ol class="ol0"> in the practice region. Multiple OLs
    # arise when a chapter sub-divides into Basic/Intermediate/Advanced.
    # Some chapters (notably ch06 SE) use ol10 / ol1 with the same
    # semantics; treat all non-bold practice ol classes uniformly.
    def _is_prompt_ol(n: Tag) -> bool:
        if not (isinstance(n, Tag) and n.name == "ol"):
            return False
        cls = n.get("class") or []
        if "bold" in cls:
            return False
        return any(c in cls for c in ("ol0", "ol1", "ol10")) or not cls

    ols = [n for n in nodes_between if _is_prompt_ol(n)]

    # Map each OL to its preceding difficulty band, if any.
    ol_band: Dict[int, Optional[str]] = {}
    for ol in ols:
        band: Optional[str] = None
        prev = ol.previous_sibling
        while prev is not None:
            if isinstance(prev, Tag):
                if prev.name == "h2":
                    band_text = _normalise_text(prev).strip()
                    if band_text in ("Basic", "Intermediate", "Advanced"):
                        band = band_text
                    break
                if prev.name in ("h1", "h3"):
                    break
            prev = prev.previous_sibling
        ol_band[id(ol)] = band

    # Map each OL to its preceding RC group (if the most recent
    # passage-style header sits between the previous OL and this one).
    rc_keys_for_ol: Dict[int, Optional[Tuple[int, int]]] = {}

    # Walk OLs in document order, assigning q numbers from `start` attr.
    for ol in ols:
        start_attr = ol.get("start")
        try:
            cur_q = int(start_attr) if start_attr else 1
        except ValueError:
            cur_q = 1

        # Pre-pass: fix the publisher's print-layout artefact where a
        # ``<p class="txc"><img/></p>`` figure sits as the LAST element
        # of one ``<li>`` but visually belongs to the NEXT ``<li>``.
        # Symptom: ch15 q3 ("area of a circle is 36") had ``325a.jpg``
        # attached, but ``325a.jpg`` is the diagram for q4 ("In the
        # diagram above, what is the value of a?"). When the next li's
        # text references the diagram and the current li doesn't, move
        # the trailing figure to the next li.
        _reattach_trailing_figures(ol)

        # Find the most recent rc group whose q_start matches or
        # encompasses cur_q.
        rc_match: Optional[RcGroup] = None
        for grp in block.rc_groups:
            if grp.q_start <= cur_q <= grp.q_end:
                rc_match = grp
                break
        rc_keys_for_ol[id(ol)] = (
            (rc_match.q_start, rc_match.q_end) if rc_match else None
        )

        for li in ol.find_all("li", recursive=False):
            options = _li_to_options(li)
            prompt_html = _li_inner_html(li)
            ak_for_q = answer_key_pre.get(cur_q)
            subtype = _detect_subtype_from_li(
                li, measure, chapter_id, options, prompt_html,
                answer_key_text=ak_for_q,
            )
            if measure == "quant" and chapter_id == "chapter20":
                # ch20 is the QC deep-dive: every item is QC.
                subtype = "qc"
            if measure == "quant" and chapter_id == "chapter16":
                # ch16 is the QC chapter (table-walker synthesises items).
                subtype = "qc"
            if measure == "quant" and chapter_id == "chapter22":
                subtype = "numeric_entry"
            if measure == "quant" and chapter_id == "chapter21":
                # Problem Solving = MCQ single (5 options) or numeric_entry.
                if not options:
                    subtype = "numeric_entry"
                else:
                    subtype = "mcq_single"

            # Classify every <img> in this <li> as one of:
            #   - option-letter glyph (skip; the parser already labelled
            #     options from these)
            #   - inline math glyph  (Stage B vision will transcribe to
            #     LaTeX; tracked in `inline_files`)
            #   - figure / diagram   (kept as the item's `figure_image`)
            #   - option-table image (TC/SE multi-blank; Stage B vision
            #     will OCR; tracked in `inline_files`)
            #
            # The structural signal: an <img> that is the *only* visible
            # content of its parent paragraph is a figure (or option-table
            # for verbal multi-blank); an <img> nestled inside a sentence
            # is a math glyph. Filename heuristics are now used only for
            # the option-letter bullet check, never to decide figure vs.
            # glyph.
            inline_files: List[str] = []
            figure_image: Optional[str] = None
            has_figure = False

            for img in li.find_all("img"):
                src = (img.get("src") or "").rsplit("/", 1)[-1]
                if not src:
                    continue
                if _is_option_letter_glyph(src):
                    continue
                # Walk up to the containing <p> (or div) to gauge whether
                # this image stands alone or is embedded in text.
                container = img.find_parent("p") or img.find_parent("div")
                lone = (container is not None
                        and _is_paragraph_lone_image(container))
                # Multi-blank TC / SE option tables: subtype already routed
                # to tc/se with no extracted options, and the image is
                # standing alone in a txc paragraph — vision OCRs it as the
                # option table. (We can't *know* the subtype yet here, but
                # the prompt-level signal — verbal chapter, blank markers,
                # no extracted options — is enough.)
                container_classes = (
                    container.get("class") if container is not None else []
                ) or []
                is_txc = "txc" in container_classes

                if lone and is_txc and (
                        subtype in ("tc", "se") and not options):
                    # Option-table image (verbal multi-blank).
                    if src not in inline_files:
                        inline_files.append(src)
                    continue

                if lone:
                    # Figure / diagram. Keep the FIRST one we see;
                    # subsequent images in the same item become inline
                    # glyphs unless they too are lone (rare).
                    if not has_figure:
                        figure_image = src
                        has_figure = True
                        continue
                    # Multiple lone images in one item → keep the first as
                    # the figure and demote the rest to inline glyphs (so
                    # the renderer still shows them).
                    if src not in inline_files:
                        inline_files.append(src)
                    continue

                # Embedded inline image → math glyph candidate.
                if src not in inline_files:
                    inline_files.append(src)

            # Re-compute the prompt HTML, this time stripping any image
            # src that's been promoted to figure_image (so the renderer
            # doesn't show the figure twice — once above the stem and
            # once inline). Inline math glyphs stay; Stage B will swap
            # them for LaTeX.
            drop_srcs: List[str] = []
            if figure_image:
                drop_srcs.append(figure_image)
            # Multi-blank TC option-table images: those were already
            # stripped from prompt_html by `_li_inner_html` (txc + img),
            # but only when subtype is verbal AND no extracted options.
            # Pass them via drop_srcs to be safe.
            for f in inline_files:
                # Only the option-table image lives in a lone txc; do not
                # strip ordinary inline math glyphs.
                pass
            if drop_srcs:
                prompt_html = _li_inner_html(li, drop_image_srcs=drop_srcs)

            item = RawItem(
                chapter_id=chapter_id,
                section_title=section_title,
                measure=measure,
                subtype=subtype,
                q_number=cur_q,
                prompt=prompt_html,
                options=options,
                difficulty_band=ol_band[id(ol)],
                has_figure=has_figure,
                figure_image=figure_image,
                rc_group_key=rc_keys_for_ol[id(ol)],
                inline_glyph_files=sorted(set(inline_files)),
                source_ref=(
                    f"{SOURCE_TAG}:{chapter_id}:set{set_index}:q{cur_q}"
                ),
            )
            block.items.append(item)
            cur_q += 1

    # Cross-reference answer key (already parsed once for subtype hints,
    # re-use it here).
    for it in block.items:
        if it.q_number in answer_key_pre:
            it.correct_label = answer_key_pre[it.q_number]

    # Cross-reference explanations.
    explanations = parse_explanations(expl_h1, stop_h1=None)
    for it in block.items:
        rec = explanations.get(it.q_number)
        if rec:
            it.explanation_label = rec.get("label", "")
            it.explanation = rec.get("html", "")

    # Default difficulty band for chapters that don't ship Basic/Intermediate/
    # Advanced subdividers (ch08 mixed sets, ch07 RC, etc.). Persistence
    # downstream interprets None as "missing"; we coerce to "medium" so the
    # adaptive engine has a stable signal.
    for it in block.items:
        if it.difficulty_band is None:
            it.difficulty_band = "medium"

    return block


def parse_chapter(epub_z: zipfile.ZipFile, chapter_id: str) -> List[PracticeBlock]:
    """Parse all practice blocks in a chapter file."""
    item_name = f"OEBPS/{chapter_id}.xhtml"
    raw = epub_z.read(item_name).decode("utf-8")
    soup = BeautifulSoup(raw, "html.parser")
    chapter_title_tag = soup.find("h1", class_="chapter-title")
    chapter_title = (
        _normalise_text(chapter_title_tag) if chapter_title_tag
        else CHAPTER_NAMES.get(chapter_id, chapter_id)
    )
    triplets = split_into_blocks(soup, chapter_id, chapter_title)
    blocks: List[PracticeBlock] = []
    for idx, (ph, ak, ex) in enumerate(triplets, start=1):
        b = parse_practice_set(ph, ak, ex, chapter_id, chapter_title, idx)
        blocks.append(b)
    return blocks


# ── Stage B — vision repair (selective) ─────────────────────────────

GLYPH_SYSTEM_PROMPT = (
    "You are a GRE math typesetter. You will receive a sequence of small "
    "JPEG images, each labelled with its filename (e.g. \"GLYPH p65c.jpg:\"). "
    "Each image contains either:\n"
    "  - A single inline math expression (a fraction, exponent, radical, "
    "    repeating-decimal overline, summation, etc.), OR\n"
    "  - A small option-choice table for a Text Completion or Sentence "
    "    Equivalence question (1-3 columns of word choices labelled A/B/C "
    "    or A/B/C plus D/E/F or further blank2/blank3 columns).\n\n"
    "For each image, output ONE record. Output a single JSON array (no "
    "markdown fences, no surrounding prose) with one element per labelled "
    "image, preserving the input order:\n\n"
    "  [{\"id\": \"p65c.jpg\", \"kind\": \"latex\", \"latex\": \"\\\\frac{1}{3}\"}, "
    "   {\"id\": \"p65a.jpg\", \"kind\": \"options\", "
    "    \"options\": [{\"label\": \"A\", \"text\": \"truculent\"}, "
    "                  {\"label\": \"B\", \"text\": \"parsimonious\"}, "
    "                  {\"label\": \"C\", \"text\": \"sojourners\"}, "
    "                  {\"label\": \"D\", \"text\": \"adversaries\"}, "
    "                  {\"label\": \"E\", \"text\": \"occupants\"}, "
    "                  {\"label\": \"F\", \"text\": \"sacrosanct\"}]}, "
    "   ... ]\n\n"
    "RULES\n"
    "- For a single math glyph, set kind=\"latex\" and emit a LaTeX string "
    "  suitable for inline math: \\frac{a}{b}, x^{12}, \\sqrt{3}, "
    "  0.\\overline{6}. Use single backslashes; never wrap money in "
    "  $ ... $; never use display-math \\[...\\].\n"
    "- For option tables, set kind=\"options\" and label rows A,B,C... "
    "  reading TOP TO BOTTOM within each column, columns LEFT TO RIGHT. "
    "  A 2-blank table has labels A,B,C in column 1 and D,E,F in column 2. "
    "  A 3-blank table has labels A,B,C / D,E,F / G,H,I.\n"
    "- If an image is unreadable or doesn't fit either kind, output "
    "  {\"id\": \"...\", \"kind\": \"unknown\", \"raw_text\": \"...\"}.\n"
    "- NEVER omit an input id; the array length must equal the input "
    "  image count.\n"
)


def _is_inline_glyph(epub_z: zipfile.ZipFile, src: str) -> bool:
    """Cheap check: inline glyphs are < 30 KB JPEGs in OEBPS/images/."""
    name = f"OEBPS/images/{src}"
    try:
        info = epub_z.getinfo(name)
    except KeyError:
        return False
    return info.file_size < 35_000


def load_glyph_cache() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(GLYPH_CACHE_PATH):
        return {}
    try:
        with open(GLYPH_CACHE_PATH) as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def save_glyph_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(GLYPH_CACHE_PATH), exist_ok=True)
    with open(GLYPH_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True, ensure_ascii=False)


def transcribe_glyphs(epub_z: zipfile.ZipFile, glyph_files: List[str],
                      cache: Dict[str, Dict[str, Any]],
                      batch_size: int = 30,
                      max_cost_usd: Optional[float] = None,
                      cost_tracker: Optional[Dict[str, float]] = None,
                      ) -> Dict[str, Dict[str, Any]]:
    """Transcribe a batch of glyph filenames to LaTeX/option records via
    Sonnet vision. Updates `cache` in-place; uses prompt caching on the
    system block.
    """
    # Local-only vision adapter (untracked; matches the `_*.py` gitignore
    # rule). The adapter must expose `VisionClient` and `MODEL_SONNET`.
    from services._vision_adapter import VisionClient, MODEL_SONNET

    if cost_tracker is None:
        cost_tracker = {"usd": 0.0}

    # Filter cache hits.
    todo = [g for g in glyph_files if g not in cache]
    if not todo:
        return {g: cache[g] for g in glyph_files}

    client = VisionClient()
    # The vision adapter does not currently expose a prompt-cache hook,
    # so we accept slightly higher cost for Phase 0; the system prompt is
    # short enough that even repeated full sends stay under the cost cap.

    for batch_start in range(0, len(todo), batch_size):
        if max_cost_usd is not None and cost_tracker["usd"] >= max_cost_usd:
            break
        batch = todo[batch_start: batch_start + batch_size]
        import time as _time
        _t0 = _time.time()
        print(f"  vision batch {batch_start // batch_size + 1}: "
              f"{len(batch)} glyph(s)... ", end="", flush=True)
        content: List[Dict[str, Any]] = []
        for g in batch:
            name = f"OEBPS/images/{g}"
            try:
                blob = epub_z.read(name)
            except KeyError:
                cache[g] = {"id": g, "kind": "missing"}
                continue
            content.append({"type": "text", "text": f"GLYPH {g}:"})
            content.append(VisionClient.encode_image_for_anthropic(
                blob, media_type="image/jpeg"
            ))
        content.append({
            "type": "text",
            "text": (
                "Transcribe each labelled glyph above per the system "
                "prompt. Output the JSON array now."
            ),
        })

        try:
            resp = client.call_anthropic(
                model=MODEL_SONNET,
                messages=[{"role": "user", "content": content}],
                system=GLYPH_SYSTEM_PROMPT,
                max_tokens=4000,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [glyph batch failed: {exc}]")
            for g in batch:
                cache.setdefault(g, {"id": g, "kind": "error", "error": str(exc)})
            continue

        # Rough cost estimate: vision-heavy Sonnet 4.6 calls run
        # ~$0.005-0.01 per image. We log a conservative $0.012/image.
        cost_tracker["usd"] += 0.012 * len(batch)
        _dt = _time.time() - _t0
        print(f"done in {_dt:.1f}s (cum cost ${cost_tracker['usd']:.2f})",
              flush=True)

        # Parse the JSON response.
        text = resp.strip()
        if text.startswith("```"):
            lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
            text = "\n".join(lines)
        try:
            arr = json.loads(text)
        except ValueError:
            print(f"  [glyph batch returned non-JSON: {text[:200]!r}]")
            for g in batch:
                cache.setdefault(g, {"id": g, "kind": "error", "error": "json"})
            continue

        for rec in arr:
            if not isinstance(rec, dict):
                continue
            gid = rec.get("id")
            if gid:
                cache[gid] = rec

    return {g: cache.get(g, {"id": g, "kind": "missing"}) for g in glyph_files}


def apply_glyph_substitutions(html: str, glyph_cache: Dict[str, Dict[str, Any]]) -> str:
    """Replace `<img class="inline" src="images/<file>">` with the
    cached LaTeX string when available."""
    soup = BeautifulSoup(html, "html.parser")
    for img in list(soup.find_all("img")):
        src = (img.get("src") or "").rsplit("/", 1)[-1]
        if not src:
            continue
        cls = img.get("class") or []
        if isinstance(cls, str):
            cls = [cls]
        if "inline" not in cls:
            continue
        # Skip option-letter bullet glyphs (a.jpg .. f.jpg).
        if re.match(r"^[a-f]\.jpg$", src, re.I):
            continue
        rec = glyph_cache.get(src)
        if not rec or rec.get("kind") != "latex":
            continue
        latex = rec.get("latex")
        if not latex:
            continue
        # Wrap inline math.
        repl = soup.new_string("")
        # Use a tiny placeholder span to inject LaTeX-bearing text.
        span = soup.new_tag("span")
        span["class"] = ["math-inline"]
        span.string = f"\\({latex}\\)"
        img.replace_with(span)
    return str(soup)


# ── Stage C — deterministic post-process ────────────────────────────

_MONEY_DD_RE = re.compile(r"\$\$(\d[\d,.\s{}\\]*)\$(?!\$)")


def clean_money_dollars(s: str) -> str:
    """Collapse `$$50$` artefacts (display-math money) to `$50`."""
    return _MONEY_DD_RE.sub(
        lambda m: "$" + m.group(1).replace("{,}", ",").replace("\\,", ","),
        s,
    )


def normalise_latex(s: str) -> str:
    """Strip leftover JSON-escape leftovers and harmonise display math.

    ``\\$`` is the canonical LaTeX escape for a literal dollar sign — when
    it lives **inside** a math context (``\\(...\\)``) it MUST be kept
    escaped, otherwise the unescaped ``$`` is parsed as a math-mode
    delimiter by KaTeX/MathJax and the whole expression gets corrupted
    (defect 'd': Q16's ``\\frac{$75}{$750}`` rendered with stray
    dollar tokens). We unescape ``\\$`` only OUTSIDE math contexts.
    """
    if not s:
        return s
    # Map ``\[…\]`` to ``\(…\)`` first so the math-context detector below
    # sees a uniform delimiter shape.
    s = s.replace("\\n", "\n")
    s = s.replace("\\[", "\\(").replace("\\]", "\\)")
    # Walk math vs non-math segments and only unescape ``\$`` outside of
    # ``\(...\)``.
    parts: List[str] = []
    cursor = 0
    for m in re.finditer(r"\\\((?:\\.|[^\\])*?\\\)", s, re.DOTALL):
        outside = s[cursor:m.start()]
        outside = outside.replace("\\$", "$")
        parts.append(outside)
        parts.append(m.group(0))
        cursor = m.end()
    parts.append(s[cursor:].replace("\\$", "$"))
    return "".join(parts)


_NUMERIC_SQRT_RE = re.compile(r"\\sqrt\{(\d+(?:\.\d+)?)\}")
_NUMERIC_FRAC_RE = re.compile(r"\\frac\{(-?\d+)\}\{(-?\d+)\}")


def parse_numeric_value(value: Optional[str]) -> Optional[float]:
    """Best-effort decimal-float parse of a printed numeric answer.
    Mirrors `_extract_manhattan.py::_parse_numeric_value`.
    """
    import math
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Normalise the Unicode minus sign that Kaplan uses in its EPUB
    # (U+2212) to the ASCII '-' that Python's float() parses.
    s_clean = (s.replace("\u2212", "-")
                .replace(",", "")
                .replace("$", "")
                .replace("\\$", ""))
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


def attach_qc_quantity_lines(prompt_html: str, raw_li_html: str) -> str:
    """Ensure literal `Quantity A:` / `Quantity B:` lines exist for QC
    items. If the source rendered them as a 2-cell table, re-flatten."""
    if "Quantity A" in prompt_html and "Quantity B" in prompt_html:
        return prompt_html
    # Look for a 2-cell table.
    soup = BeautifulSoup(raw_li_html, "html.parser")
    table = soup.find("table")
    if table is not None:
        cells = table.find_all("td")
        if len(cells) >= 2:
            a = _normalise_text(cells[0])
            b = _normalise_text(cells[1])
            return f"{prompt_html}\n<p>Quantity A: {a}</p>\n<p>Quantity B: {b}</p>"
    return prompt_html


def dedupe_options(options: List[RawOption]) -> List[RawOption]:
    """Drop exact-duplicate option text (case-insensitive, trimmed)."""
    seen: set = set()
    out: List[RawOption] = []
    for o in options:
        key = re.sub(r"\s+", " ", (o.text or "").lower()).strip()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out


def truncate_explanation(html: str) -> str:
    if not html:
        return html
    if len(html.encode("utf-8")) <= MAX_EXPLANATION_BYTES:
        return html
    # Truncate and append ellipsis indicator.
    return html.encode("utf-8")[:MAX_EXPLANATION_BYTES - 30].decode(
        "utf-8", errors="ignore"
    ) + "...\n<!-- truncated -->"


# Apply the substitutions Stage B produced and run the cleanups.

def post_process_block(block: PracticeBlock,
                       glyph_cache: Optional[Dict[str, Dict[str, Any]]] = None,
                       ) -> None:
    """Mutate the block in place: substitute glyph LaTeX, clean money,
    normalise LaTeX, dedupe options."""
    if glyph_cache is None:
        glyph_cache = {}
    for it in block.items:
        if glyph_cache:
            it.prompt = apply_glyph_substitutions(it.prompt, glyph_cache)
            for opt in it.options:
                opt.text = apply_glyph_substitutions(opt.text, glyph_cache)
            it.explanation = apply_glyph_substitutions(it.explanation, glyph_cache)
            # Glyph-cached option tables: when an item came in with no
            # extracted options but its inline_glyph_files include an
            # option-table image, hydrate from the cache.
            if not it.options and it.inline_glyph_files:
                for g in it.inline_glyph_files:
                    rec = glyph_cache.get(g)
                    if not rec or rec.get("kind") != "options":
                        continue
                    for o in rec.get("options", []):
                        it.options.append(RawOption(
                            label=o.get("label", ""),
                            text=o.get("text", ""),
                        ))
                    break
            # Numeric_entry / mcq_short_answer items whose printed
            # answer was a glyph image: hydrate from the cache.
            if (it.subtype in ("numeric_entry", "mcq_short_answer")
                    and it.correct_label.startswith("@@GLYPH:")):
                src = it.correct_label[len("@@GLYPH:"):-len("@@")]
                rec = glyph_cache.get(src)
                if rec and rec.get("kind") == "latex":
                    it.correct_label = rec.get("latex", "")

        # Clean text fields.
        it.prompt = clean_money_dollars(normalise_latex(it.prompt))
        it.explanation = clean_money_dollars(normalise_latex(it.explanation))
        for opt in it.options:
            opt.text = clean_money_dollars(normalise_latex(opt.text))

        # QC fixup.
        if it.subtype == "qc":
            it.prompt = attach_qc_quantity_lines(it.prompt, it.prompt)
            # Standard QC option set (4 fixed choices).
            if not it.options:
                it.options = [
                    RawOption("A", "Quantity A is greater."),
                    RawOption("B", "Quantity B is greater."),
                    RawOption("C", "The two quantities are equal."),
                    RawOption("D",
                              "The relationship cannot be determined "
                              "from the information given."),
                ]

        # Dedupe options.
        it.options = dedupe_options(it.options)

        # Set is_correct flags from the answer-key label.
        _mark_correct_options(it)

        # TC multi-blank: relabel options as blank1_A / blank2_D / etc.
        # so the wxPython renderer groups them by blank (defect 'f').
        _relabel_multiblank_tc_options(it)

        # Numeric answer for numeric_entry rows.
        if it.subtype == "numeric_entry":
            it.numeric_value = it.correct_label

        # Truncate over-long explanations.
        it.explanation = truncate_explanation(it.explanation)


def _mark_correct_options(it: RawItem) -> None:
    """Parse `it.correct_label` ("C, D" / "A" / "B, E") and flip the
    matching option's is_correct flag."""
    if not it.options:
        return
    raw = (it.correct_label or "").strip()
    if not raw or raw.startswith("@@GLYPH"):
        return
    labels = {p.strip().upper() for p in re.split(r"\s*,\s*", raw) if p.strip()}
    if not labels:
        return
    for opt in it.options:
        opt.is_correct = (opt.label or "").strip().upper() in labels


# ── Public driver ───────────────────────────────────────────────────


def _classify_block_images(epub_z: zipfile.ZipFile,
                           blocks: List[PracticeBlock],
                           glyph_cache: Dict[str, Dict[str, Any]],
                           cache=None,
                           cost_tracker: Optional[Dict[str, float]] = None,
                           max_cost_usd: Optional[float] = None) -> None:
    """Run the image-bucket classifier over every image reference in
    ``blocks`` and re-route based on the verdict.

    See :mod:`services.image_classifier` for the bucket vocabulary.
    """
    from services import image_classifier as ic
    if cache is None:
        cache = ic.get_cache()
    if cost_tracker is None:
        cost_tracker = {"usd": 0.0}

    def _read(src: str) -> Optional[bytes]:
        if not src:
            return None
        for cand in (f"OEBPS/images/{src}", f"OEBPS/{src}", src):
            try:
                return epub_z.read(cand)
            except KeyError:
                continue
        return None

    def _classify(src: str, context: str) -> Dict[str, Any]:
        if max_cost_usd is not None and cost_tracker["usd"] >= max_cost_usd:
            return {"bucket": ic.BUCKET_UNKNOWN, "source": "budget_exhausted",
                    "filename": src, "transcription": "", "options": []}
        blob = _read(src)
        # Detect whether this will be a cache hit BEFORE classify_image
        # mutates state, so we don't double-bill cached verdicts whose
        # `source="sonnet"` reflects a prior run.
        cache_hit = cache.get(blob) is not None
        verdict = ic.classify_image(filename=src, image_bytes=blob,
                                    context=context, cache=cache,
                                    enable_vision=True)
        if not cache_hit and verdict.get("source") == "sonnet":
            cost_tracker["usd"] += 0.005
        return verdict

    def _hydrate_glyph_cache_from_verdict(src: str,
                                          verdict: Dict[str, Any]) -> None:
        """Populate ``glyph_cache`` from a classifier verdict so the
        existing :func:`apply_glyph_substitutions` pass can swap the
        ``<img>`` tag for LaTeX or option text."""
        bucket = verdict.get("bucket")
        if bucket in (ic.BUCKET_INLINE_MATH, ic.BUCKET_QUANTITY_EXPR):
            latex = (verdict.get("transcription") or "").strip()
            if latex:
                glyph_cache[src] = {"id": src, "kind": "latex",
                                    "latex": latex}
        elif bucket == ic.BUCKET_ANSWER_TABLE:
            opts = verdict.get("options") or []
            if opts:
                norm = []
                for o in opts:
                    norm.append({"label": str(o.get("label", "")).strip().upper(),
                                 "text": str(o.get("text", "")).strip()})
                glyph_cache[src] = {"id": src, "kind": "options",
                                    "options": norm}

    for b in blocks:
        for it in b.items:
            ctx = f"chapter={it.chapter_id}, subtype={it.subtype}, " \
                  f"q={it.q_number}"

            # ─ figure_image gate ─
            if it.figure_image:
                v = _classify(it.figure_image, ctx)
                _hydrate_glyph_cache_from_verdict(it.figure_image, v)
                bucket = v.get("bucket")
                if bucket in ic.DROP_BUCKETS:
                    # numeric_box / bullet → silently strip
                    it.figure_image = None
                    it.has_figure = False
                elif bucket in (ic.BUCKET_INLINE_MATH, ic.BUCKET_QUANTITY_EXPR,
                                ic.BUCKET_ANSWER_TABLE):
                    # Demote to inline_glyph_files so post_process_block can
                    # swap it inline / hydrate options. Drop the figure ref
                    # so the renderer doesn't show the tiny image as a
                    # standalone figure.
                    src = it.figure_image
                    it.figure_image = None
                    it.has_figure = False
                    if src not in it.inline_glyph_files:
                        it.inline_glyph_files.append(src)
                # diagram / chart / unknown → keep as figure_image

            # ─ inline_glyph_files gate ─
            keep_glyphs: List[str] = []
            for g in it.inline_glyph_files:
                v = _classify(g, ctx)
                _hydrate_glyph_cache_from_verdict(g, v)
                bucket = v.get("bucket")
                if bucket in ic.DROP_BUCKETS:
                    continue   # drop bullet / numeric_box
                if bucket in ic.FIGURE_BUCKETS:
                    # An "inline glyph" that's actually a real diagram —
                    # promote to figure_image (only if we don't already
                    # have one).
                    if it.figure_image is None:
                        it.figure_image = g
                        it.has_figure = True
                        continue
                keep_glyphs.append(g)
            it.inline_glyph_files = sorted(set(keep_glyphs))

        # rc_group figures: drop bullets/numeric_box from cluster figures.
        for grp in b.rc_groups:
            keep = []
            for fig in grp.figure_images:
                v = _classify(fig, "RC/DI cluster figure")
                _hydrate_glyph_cache_from_verdict(fig, v)
                bucket = v.get("bucket")
                if bucket in ic.DROP_BUCKETS:
                    continue
                if bucket in (ic.BUCKET_INLINE_MATH, ic.BUCKET_QUANTITY_EXPR,
                              ic.BUCKET_ANSWER_TABLE):
                    # Cluster glyph ≠ cluster diagram; drop from cluster
                    # figures.
                    continue
                keep.append(fig)
            grp.figure_images = keep


def _relabel_multiblank_tc_options(it: RawItem) -> None:
    """For TC items whose options came from a multi-blank answer table,
    relabel A/B/C/D/E/F (or A-I) into ``blank1_A`` ... ``blank3_C`` so
    the markdown renderer + wxPython runtime group them by blank.

    Convention (matches the seed-data shape in
    :mod:`scripts.seed_data` and the runtime renderer in
    ``screens/question_screen.py``):
      2-blank: A,B,C → blank1_A,blank1_B,blank1_C;
               D,E,F → blank2_A,blank2_B,blank2_C
      3-blank: A,B,C → blank1_A,blank1_B,blank1_C;
               D,E,F → blank2_A,blank2_B,blank2_C;
               G,H,I → blank3_A,blank3_B,blank3_C
      1-blank or items with already-prefixed labels: untouched.

    The choice letter restarts at A inside each blank (Princeton's seed
    bank uses the same convention) so the wxPython runtime treats each
    blank as an independent radio group.
    """
    if it.subtype != "tc":
        return
    if not it.options:
        return
    # Already prefixed?
    if any("_" in (o.label or "") for o in it.options):
        return
    n = len(it.options)
    plain = re.sub(r"<[^>]+>", " ", it.prompt or "")
    blanks_in_prompt = set(_BLANK_RE.findall(plain))
    declared_blanks = 1
    if "iii" in blanks_in_prompt:
        declared_blanks = 3
    elif "ii" in blanks_in_prompt:
        declared_blanks = 2
    if n == 9:
        declared_blanks = 3
    elif n == 6:
        declared_blanks = max(declared_blanks, 2)
    elif n in (3, 4, 5):
        declared_blanks = 1
    if declared_blanks <= 1:
        return
    per_blank = n // declared_blanks
    if per_blank not in (3,):
        return
    new_correct: List[str] = []
    correct_set = {p.strip().upper() for p in
                   re.split(r"\s*,\s*", it.correct_label or "") if p.strip()}
    inner_letters = ["A", "B", "C", "D", "E", "F"]
    for i, o in enumerate(it.options):
        blank_idx = i // per_blank + 1
        choice_idx = i % per_blank
        new_label = f"blank{blank_idx}_{inner_letters[choice_idx]}"
        if (o.label or "").upper() in correct_set:
            new_correct.append(new_label)
        o.label = new_label
    if new_correct:
        it.correct_label = ", ".join(new_correct)


def extract_chapter(epub_z: zipfile.ZipFile, chapter_id: str,
                    use_vision: bool = False,
                    glyph_cache: Optional[Dict[str, Dict[str, Any]]] = None,
                    cost_tracker: Optional[Dict[str, float]] = None,
                    max_cost_usd: Optional[float] = None,
                    batch_size: int = 10,
                    use_image_classifier: bool = False,
                    bucket_cache=None,
                    ) -> List[PracticeBlock]:
    blocks = parse_chapter(epub_z, chapter_id)
    # Step A.5: image-bucket classification.
    # Mandate: only diagrams + DI charts may travel as figure_image; every
    # other image reference gets vision-rendered to text and the image
    # ref dropped. Sub-buckets:
    #   numeric_box / bullet → drop entirely (the runtime renders these)
    #   inline_math / quantity_expression → glyph_cache (kind=latex)
    #   answer_table → glyph_cache (kind=options)
    #   diagram / chart → keep as figure_image (or rc_group figure_images)
    if use_image_classifier:
        if glyph_cache is None:
            glyph_cache = load_glyph_cache()
        _classify_block_images(epub_z, blocks, glyph_cache,
                               cache=bucket_cache,
                               cost_tracker=cost_tracker,
                               max_cost_usd=max_cost_usd)
        save_glyph_cache(glyph_cache)
    if use_vision:
        # Collect every glyph + option-table image referenced by the
        # chapter and transcribe in batches. We scan three sources:
        #   1. inline_glyph_files (collected per item during Stage A)
        #   2. answer-key glyphs (numeric answers that are themselves
        #      JPEGs, e.g. fractions)
        #   3. EXPLANATION HTML — Stage A doesn't track explanation
        #      glyphs explicitly, so we walk every <img class="inline">
        #      in the explanation now and add it to the batch.
        all_glyphs: List[str] = []
        for b in blocks:
            for it in b.items:
                for g in it.inline_glyph_files:
                    if g and g not in all_glyphs:
                        all_glyphs.append(g)
                if it.correct_label.startswith("@@GLYPH:"):
                    src = it.correct_label[len("@@GLYPH:"):-len("@@")]
                    if src and src not in all_glyphs:
                        all_glyphs.append(src)
                # NEW: explanation-side glyphs. We use the same lone-image
                # vs. embedded-glyph heuristic so explanation diagrams get
                # routed to figure-style rendering, while inline math
                # glyphs go to LaTeX transcription.
                if it.explanation:
                    expl_soup = BeautifulSoup(it.explanation, "html.parser")
                    for img in expl_soup.find_all("img"):
                        src = (img.get("src") or "").rsplit("/", 1)[-1]
                        if not src or _is_option_letter_glyph(src):
                            continue
                        if src not in all_glyphs:
                            all_glyphs.append(src)
                        # Track on the item so the renderer can handle
                        # explanation glyph swaps.
                        if src not in it.inline_glyph_files:
                            it.inline_glyph_files.append(src)
        if glyph_cache is None:
            glyph_cache = load_glyph_cache()
        if all_glyphs:
            transcribe_glyphs(
                epub_z, all_glyphs, glyph_cache,
                batch_size=batch_size,
                cost_tracker=cost_tracker, max_cost_usd=max_cost_usd,
            )
            save_glyph_cache(glyph_cache)
    for b in blocks:
        post_process_block(b, glyph_cache=glyph_cache)
    return blocks


# ── CLI ─────────────────────────────────────────────────────────────

def _block_to_dict(b: PracticeBlock) -> Dict[str, Any]:
    out = asdict(b)
    return out


def _summarise_blocks(blocks: List[PracticeBlock]) -> Dict[str, Any]:
    from validators.kaplan import validate, summarise

    summary: Dict[str, Any] = {
        "blocks": len(blocks),
        "items": 0,
        "by_subtype": {},
        "by_band": {},
        "rc_groups": 0,
        "gates": {},
        "block_failures": 0,
        "warn_failures": 0,
        "samples": [],
    }
    items_for_validate: List[Dict[str, Any]] = []
    for b in blocks:
        summary["rc_groups"] += len(b.rc_groups)
        for it in b.items:
            summary["items"] += 1
            summary["by_subtype"][it.subtype] = summary["by_subtype"].get(
                it.subtype, 0) + 1
            band = it.difficulty_band or "(none)"
            summary["by_band"][band] = summary["by_band"].get(band, 0) + 1
            items_for_validate.append(asdict(it))

    issues_per_item, gate_counts, severity_counts = summarise(
        items_for_validate, validate
    )
    summary["gates"] = gate_counts
    summary["block_failures"] = severity_counts.get("block", 0)
    summary["warn_failures"] = severity_counts.get("warn", 0)
    summary["info_failures"] = severity_counts.get("info", 0)
    summary["items_with_block_issue"] = sum(
        1 for issues in issues_per_item
        if any(i["severity"] == "block" for i in issues)
    )
    summary["items_with_warn_issue"] = sum(
        1 for issues in issues_per_item
        if any(i["severity"] == "warn" for i in issues)
    )
    # Sample of failing items (up to 8).
    failing = [
        (idx, issues) for idx, issues in enumerate(issues_per_item)
        if any(i["severity"] == "block" for i in issues)
    ][:8]
    for idx, issues in failing:
        rec = items_for_validate[idx]
        summary["samples"].append({
            "chapter_id": rec.get("chapter_id"),
            "q_number": rec.get("q_number"),
            "subtype": rec.get("subtype"),
            "issues": [(i["severity"], i["kind"]) for i in issues
                       if i["severity"] == "block"],
            "prompt_excerpt": (rec.get("prompt") or "")[:160],
        })
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--epub", default=EPUB_PATH)
    ap.add_argument("--chapter", type=int, default=None,
                    help="single chapter index (e.g., 11)")
    ap.add_argument("--chapters", default=None,
                    help="comma-separated chapter indices, e.g. '5,11'")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse + validate but don't write to DB")
    ap.add_argument("--vision", action="store_true",
                    help="enable Stage B: Sonnet glyph transcription")
    ap.add_argument("--classify-images", action="store_true",
                    help="enable image-bucket classifier (drops "
                         "numeric_box / bullet glyphs from figure_image; "
                         "demotes inline_math / answer_table images out "
                         "of figure_image; populates glyph_cache)")
    ap.add_argument("--max-cost-usd", type=float, default=15.0,
                    help="hard cap on Stage B vision spend (default $15)")
    ap.add_argument("--batch-size", type=int, default=10,
                    help="vision call batch size (default 10)")
    ap.add_argument("--out-dir", default=PHASE0_DIR,
                    help="destination for phase0 dump JSON")
    args = ap.parse_args()

    chapter_ids: List[str] = []
    if args.chapter:
        chapter_ids = [f"chapter{args.chapter:02d}"]
    elif args.chapters:
        for tok in args.chapters.split(","):
            tok = tok.strip()
            if tok:
                chapter_ids.append(f"chapter{int(tok):02d}")
    else:
        ap.error("Provide --chapter or --chapters; full extraction is "
                 "intentionally not the Phase 0 default.")

    os.makedirs(args.out_dir, exist_ok=True)

    cost_tracker = {"usd": 0.0}
    glyph_cache = load_glyph_cache() if (args.vision or args.classify_images) else {}

    z = zipfile.ZipFile(args.epub)
    total_summary: Dict[str, Any] = {
        "chapters": {},
        "total_items": 0,
        "total_blocks": 0,
        "total_block_failures": 0,
        "total_warn_failures": 0,
        "vision_cost_usd": 0.0,
    }
    for ch in chapter_ids:
        print(f"\n=== {ch} ===")
        blocks = extract_chapter(
            z, ch, use_vision=args.vision,
            glyph_cache=glyph_cache,
            cost_tracker=cost_tracker,
            max_cost_usd=args.max_cost_usd,
            batch_size=args.batch_size,
            use_image_classifier=args.classify_images,
        )
        summary = _summarise_blocks(blocks)
        total_summary["chapters"][ch] = summary
        total_summary["total_items"] += summary["items"]
        total_summary["total_blocks"] += summary["blocks"]
        total_summary["total_block_failures"] += summary.get(
            "items_with_block_issue", 0)
        total_summary["total_warn_failures"] += summary.get(
            "items_with_warn_issue", 0)

        # Write per-chapter dump.
        dump_path = os.path.join(args.out_dir, f"phase0_{ch}.json")
        with open(dump_path, "w") as f:
            json.dump({
                "chapter_id": ch,
                "blocks": [_block_to_dict(b) for b in blocks],
                "summary": summary,
            }, f, indent=2, ensure_ascii=False)
        print(f"  -> wrote {dump_path}")
        print(f"  blocks={summary['blocks']} items={summary['items']} "
              f"block_fail={summary.get('items_with_block_issue', 0)} "
              f"warn_fail={summary.get('items_with_warn_issue', 0)}")
        for kind, cnt in sorted(summary["gates"].items(),
                                key=lambda kv: -kv[1])[:10]:
            print(f"    gate {kind}: {cnt}")

    total_summary["vision_cost_usd"] = cost_tracker["usd"]
    summary_path = os.path.join(args.out_dir, "phase0_summary.json")
    with open(summary_path, "w") as f:
        json.dump(total_summary, f, indent=2, ensure_ascii=False)
    print(f"\nSummary -> {summary_path}")
    print(f"Total: {total_summary['total_items']} items across "
          f"{total_summary['total_blocks']} blocks, "
          f"{total_summary['total_block_failures']} with block-severity "
          f"issues, {total_summary['total_warn_failures']} with warns. "
          f"Vision cost: ${total_summary['vision_cost_usd']:.2f}.")


if __name__ == "__main__":
    main()
