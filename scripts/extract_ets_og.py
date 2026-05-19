#!/usr/bin/env python3
"""
ETS Official Guide to the GRE General Test (3rd ed.) extraction pipeline.
============================================================================

Phase 4 · D1.  See docs/implementation_plan_2026_05_12.md.

Overview
--------
The ETS Official Guide (3rd edition) ships ~300 real retired GRE items plus
four full-length practice tests.  Because the book is licensed content that
we cannot redistribute in git, this script is structured so **you bring your
own copy** (an EPUB bought from ETS/Kindle, or an exported PDF) and the
pipeline converts it into our Question/QuestionOption/NumericAnswer/Stimulus
schema with ``source='ets_og_3rd'`` and ``status='candidate'``.

How to use
----------
1. Purchase the ETS Official Guide to the GRE General Test (3rd edition)
   from ETS (https://www.ets.org/gre/test-takers/general-test/prepare/) or
   as an Amazon / Kindle e-book.  Both EPUB and PDF inputs work.
2. Smoke-test the pipeline against the bundled synthetic fixture first:

       venv/bin/python scripts/extract_ets_og.py --dry-run \\
           --ebook tests/fixtures/fake_ets_og.pdf

   This runs the entire path (PDF -> markdown -> parser -> summary) without
   touching the database so you can verify the plumbing works.
3. Real import (writes candidates to the DB):

       venv/bin/python scripts/extract_ets_og.py --ebook /path/to/ETS_OG_3e.epub

4. Review the candidates.  Every row lands with ``status='candidate'``,
   which routes it through the human-review queue before any student sees
   it.  Figure-bearing stems are additionally routed through the vision
   audit pipeline (see docs/figure_audit_2026_05_11.md).  After review,
   promote to ``pretest`` -> ``live`` via the normal content-ops flow.

Idempotency
-----------
``(source, source_anchor)`` is the unique key for upserts.  Re-running the
script on the same ebook is a no-op: existing candidates are reported as
"skipped (already present)" rather than duplicated.

Subtype classification
----------------------
The parser inspects each question block for the following signals, in
order (first match wins):

    "Quantity A" + "Quantity B"   -> qc
    "Select two ... from ..."     -> sentence_equiv       (6 choices)
    "Select all that apply"       -> mcq_multi
    numeric-entry box placeholder -> numeric_entry
    "following reading passage"
      / nearest preceding passage -> rc_single / rc_multi
    chart / graph / table figure  -> data_interp
    blank(s) in stem ("______")   -> text_completion
    default                       -> mcq_single

Difficulty mapping
------------------
ETS prints an explicit difficulty label next to each item; we normalise:

    Easy   -> difficulty_target = 2
    Medium -> difficulty_target = 3
    Hard   -> difficulty_target = 4

Items without an explicit label default to 3 (Medium).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Make project root importable when run as a script.
_THIS = Path(__file__).resolve()
PROJECT_ROOT = _THIS.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


SOURCE_TAG = "ets_og_3rd"

# ── Difficulty mapping ────────────────────────────────────────────────

DIFFICULTY_MAP = {
    "easy": 2,
    "medium": 3,
    "hard": 4,
}

# ── Parsed record shapes ──────────────────────────────────────────────


@dataclass
class ETSQuestion:
    """One parsed ETS OG item, ready for DB insert."""

    number: int                          # 1-based number within the ebook
    measure: str                         # verbal / quant / awa
    subtype: str                         # mcq_single / qc / sentence_equiv / ...
    prompt: str
    options: List[Tuple[str, str]] = field(default_factory=list)
    correct_labels: List[str] = field(default_factory=list)
    difficulty: Optional[str] = None     # raw "easy" / "medium" / "hard"
    explanation: str = ""
    numeric_value: Optional[float] = None
    has_figure: bool = False             # true if stem/block mentions a figure
    stimulus_text: str = ""               # populated for RC items
    stimulus_title: str = ""
    source_anchor: str = ""              # e.g. "q003"
    page_index: int = 0                  # 1-based page number for audit trail

    @property
    def difficulty_target(self) -> int:
        if self.difficulty and self.difficulty.lower() in DIFFICULTY_MAP:
            return DIFFICULTY_MAP[self.difficulty.lower()]
        return 3


# ── Regexes ───────────────────────────────────────────────────────────

# Question header. Accepts either "Question N." / "Question N [Easy]" /
# "N. [Hard]".  We anchor on the word "Question" to keep the parser
# robust against stray numerics in stems.
_Q_HEADER = re.compile(
    r"^\s*Question\s+(\d{1,3})[.\s]*(?:\[(Easy|Medium|Hard)\])?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Alternative header: bare "N. [Difficulty]" (matches ETS's inline
# numbering on some practice-test pages).  Used as a fallback when no
# "Question" keyword is present.
_Q_HEADER_ALT = re.compile(
    r"^\s*(\d{1,3})\.\s*\[(Easy|Medium|Hard)\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Answer line: "Answer: A"  |  "Answer: A, B"  |  "Answer: 42"
_ANSWER = re.compile(
    r"^\s*Answer\s*[:\-]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Option marker: "(A)" through "(F)".  ETS supports up to 6 choices for
# sentence-equivalence items, so F is the upper bound.
_OPT = re.compile(r"\(([A-F])\)\s*([^\n]*)")

# Passage / stimulus header.  Some ETS chapters use "Passage N" plus a
# title; practice tests use "Reading Passage".
_PASSAGE_HEADER = re.compile(
    r"^\s*(Passage(?:\s+\d+)?|Reading\s+Passage(?:\s+\d+)?)\b(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

# Figure hints.  Same vocabulary as Regents, plus ETS-specific markers.
_FIGURE_HINTS = re.compile(
    r"\b(figure|diagram|graph|chart|table|shown (?:above|below)|"
    r"the (?:following|accompanying)\s+(?:figure|graph|chart|table))\b",
    re.IGNORECASE,
)

# Section headers inside ETS ebooks — tell us which measure a block
# belongs to.  Most chapters are single-measure so a single hit at the
# top of a page is enough.
_MEASURE_HINTS = [
    (re.compile(r"\b(verbal\s+reasoning|reading\s+comprehension|"
                r"text\s+completion|sentence\s+equivalence)\b",
                re.IGNORECASE), "verbal"),
    (re.compile(r"\b(quantitative\s+reasoning|quantitative\s+comparison|"
                r"data\s+interpretation|numeric\s+entry)\b",
                re.IGNORECASE), "quant"),
    (re.compile(r"\b(analytical\s+writing|issue\s+task|argument\s+task)\b",
                re.IGNORECASE), "awa"),
]

# Subtype hint regexes.
_HINT_QC = re.compile(r"\bQuantity\s+A\b.*\bQuantity\s+B\b", re.IGNORECASE | re.DOTALL)
_HINT_SE = re.compile(
    r"select\s+the\s+two\s+answer\s+choices|"
    r"select\s+two\s+.+\s+that.*\bcomplete\b|"
    r"two\s+that.*similar\s+in\s+meaning",
    re.IGNORECASE,
)
_HINT_MCQ_MULTI = re.compile(
    r"select\s+(?:all|one\s+or\s+more)\s+(?:that\s+apply|answer\s+choices)",
    re.IGNORECASE,
)
_HINT_NUMERIC = re.compile(
    r"enter\s+your\s+answer|"
    r"enter\s+the\s+(?:answer|value|fraction)|"
    r"numeric\s+entry|"
    r"(?:^|\n)\s*(?:\[\s*\]|\bbox\b|☐)",
    re.IGNORECASE,
)
_HINT_TC_BLANKS = re.compile(r"_{3,}")
_HINT_DATA_INTERP = re.compile(
    r"\b(bar\s+chart|pie\s+chart|line\s+graph|histogram|scatter\s?plot|"
    r"data\s+interpretation|based\s+on\s+the\s+(?:graph|chart|table))\b",
    re.IGNORECASE,
)

# Figure-link marker used by pymupdf4llm:
# ![](images/xyz.png) or ![alt](images/...).
_FIGURE_LINK = re.compile(r"!\[[^\]]*\]\([^)]*\)")


# ── Markdown-level page concatenation ─────────────────────────────────

def _load_pages(md_dir: Path) -> List[Tuple[int, str]]:
    """Return [(page_index_1based, text), ...] sorted by page index.

    Accepts both the ``pdf_page_NNNN.md`` naming from marker_pipeline
    and the EPUB ``epub_page_NNNN.md`` variant.
    """
    pages: List[Tuple[int, str]] = []
    for p in sorted(md_dir.glob("*_page_*.md")):
        m = re.search(r"_page_(\d+)\.md$", p.name)
        if not m:
            continue
        idx = int(m.group(1))
        pages.append((idx, p.read_text(encoding="utf-8")))
    return pages


# ── Measure inference ─────────────────────────────────────────────────

def _infer_measure(block_text: str, fallback: str) -> str:
    """Pick the most likely measure for a question block.

    We check the block itself first, then fall back to the surrounding
    page-level measure (which the caller pre-computed).
    """
    # Block-local structural signals come first — Quantity A/B is
    # unambiguous even in a mis-labeled chapter.
    if _HINT_QC.search(block_text):
        return "quant"
    if _HINT_DATA_INTERP.search(block_text):
        return "quant"
    if _HINT_NUMERIC.search(block_text):
        return "quant"
    # Verbal-specific markers.
    if _HINT_SE.search(block_text):
        return "verbal"
    if _HINT_TC_BLANKS.search(block_text):
        return "verbal"
    # Fall back to page-level / chapter-level hint.
    return fallback or "verbal"


def _page_measure(page_text: str, prior: Optional[str]) -> Optional[str]:
    """Determine the measure of the current page.

    If the page explicitly names a measure heading, use that;
    otherwise inherit from the prior page.
    """
    for regex, measure in _MEASURE_HINTS:
        if regex.search(page_text):
            return measure
    return prior


# ── Subtype classification ────────────────────────────────────────────

def classify_subtype(block_text: str, measure: str,
                     options: List[Tuple[str, str]],
                     correct_labels: List[str],
                     has_figure: bool) -> str:
    """Classify a parsed question block into a subtype slug.

    Parameters
    ----------
    block_text:
        The raw text of the entire question (stem + options + answer).
    measure:
        Already-inferred measure ('verbal', 'quant', 'awa').
    options:
        Parsed (label, text) pairs. Length informs several heuristics
        (SE has 6, QC has 4, numeric has 0, etc.).
    correct_labels:
        Parsed answer letters. Length > 1 implies multi-select.
    has_figure:
        Whether the block references a figure / image.
    """
    if measure == "awa":
        # ETS AWA comes in two flavours; the book prints the task type
        # right in the prompt.
        if re.search(r"\bissue\s+task\b", block_text, re.IGNORECASE):
            return "awa_issue"
        return "awa_argument"

    # Quant-side structural subtypes take priority — they're
    # unambiguous when present.
    if _HINT_QC.search(block_text):
        return "qc"
    if _HINT_NUMERIC.search(block_text) and not options:
        return "numeric_entry"
    if has_figure and _HINT_DATA_INTERP.search(block_text):
        return "data_interp"

    # Verbal-side: SE and multi-select share the 6-option shape, but
    # SE has the distinctive "similar in meaning" prompt.
    if _HINT_SE.search(block_text) or (measure == "verbal" and len(options) == 6):
        return "sentence_equiv"
    if _HINT_MCQ_MULTI.search(block_text) or len(correct_labels) > 1:
        return "mcq_multi"

    # Text completion — 1 to 3 blanks denoted by underscores.
    if measure == "verbal" and _HINT_TC_BLANKS.search(block_text):
        return "text_completion"

    # Reading comprehension items are conventionally mcq_single unless
    # the block itself says otherwise (handled above for multi).  The
    # ``rc_single`` / ``rc_multi`` subtypes exist for items anchored
    # to a shared passage; the stimulus-linking pass (see
    # :func:`_link_passage_stimuli`) upgrades mcq_single -> rc_single
    # for those items.  Plain mcq_single is the safe default here.
    return "mcq_single"


# ── Question-block parser ─────────────────────────────────────────────

def _split_question_blocks(page_text: str) -> List[Tuple[int, Optional[str], str]]:
    """Chunk a page into (qnum, difficulty_label, body_text) tuples.

    Question boundaries are detected by :data:`_Q_HEADER` (preferred)
    or :data:`_Q_HEADER_ALT` (fallback). The body extends from the
    header line until the next header or end-of-page.
    """
    # Collect all header matches, keeping (start_offset, qnum, diff).
    matches: List[Tuple[int, int, int, Optional[str]]] = []
    for m in _Q_HEADER.finditer(page_text):
        matches.append((m.start(), m.end(), int(m.group(1)), m.group(2)))
    if not matches:
        for m in _Q_HEADER_ALT.finditer(page_text):
            matches.append((m.start(), m.end(), int(m.group(1)), m.group(2)))
    if not matches:
        return []

    matches.sort(key=lambda t: t[0])
    out: List[Tuple[int, Optional[str], str]] = []
    for i, (_, header_end, qnum, diff) in enumerate(matches):
        body_end = matches[i + 1][0] if i + 1 < len(matches) else len(page_text)
        body = page_text[header_end:body_end].strip()
        out.append((qnum, diff, body))
    return out


def _parse_options(body: str) -> Tuple[List[Tuple[str, str]], str]:
    """Extract option list + return (options, body_without_options).

    The body-without-options is useful for downstream heuristics that
    only want to reason about the stem.
    """
    # Find the first option marker; treat everything before as stem.
    first_match = None
    positions: List[Tuple[int, str]] = []
    for m in _OPT.finditer(body):
        if first_match is None:
            first_match = m
        positions.append((m.start(), m.group(1)))

    if not first_match or not positions:
        return [], body.strip()

    stem = body[: first_match.start()].strip()

    # Determine where options end — at an "Answer:" line or body end.
    ans_match = _ANSWER.search(body)
    opts_end = ans_match.start() if ans_match else len(body)

    pairs: List[Tuple[str, str]] = []
    for i, (pos, label) in enumerate(positions):
        # Skip option markers that fall after the answer line — they
        # belong to the explanation, not the option list.
        if pos >= opts_end:
            break
        match_obj = _OPT.match(body, pos)
        if not match_obj:
            continue
        text_start = match_obj.end()
        # Option text runs until the next option marker or opts_end.
        next_pos = positions[i + 1][0] if i + 1 < len(positions) else opts_end
        text = body[text_start:next_pos].strip()
        # Text may include trailing whitespace/newlines — collapse.
        text = re.sub(r"\s+", " ", text).strip()
        pairs.append((label, text))

    # Deduplicate by label in case a stem contains "(A)" as content.
    by_label: Dict[str, str] = {}
    for lbl, txt in pairs:
        by_label.setdefault(lbl, txt)
    ordered = sorted(by_label.items(), key=lambda kv: kv[0])

    return ordered, stem


def _parse_answer(body: str) -> Tuple[List[str], Optional[float]]:
    """Return (correct_labels, numeric_value).

    A plain numeric answer (e.g. "Answer: 42") populates numeric_value;
    letter answers (possibly comma-separated) populate correct_labels.
    """
    m = _ANSWER.search(body)
    if not m:
        return [], None
    raw = m.group(1).strip()
    # Numeric answer?  Accept ints, decimals, and simple fractions.
    num_m = re.match(r"^-?\d+(?:\.\d+)?$", raw)
    if num_m:
        try:
            return [], float(raw)
        except ValueError:  # pragma: no cover
            return [], None
    # Letter answer(s): split on comma / "and" / whitespace.
    tokens = re.split(r"[,\s]+(?:and\s+)?|\s+and\s+", raw)
    labels = [t.strip().upper() for t in tokens if t.strip()]
    labels = [lbl for lbl in labels if re.fullmatch(r"[A-F]", lbl)]
    return labels, None


def _parse_passages(page_text: str) -> List[Tuple[str, str, int, int]]:
    """Return [(title, body_text, start_offset, end_offset), ...].

    A passage extends from its header to the next question header
    (``Question N``) or another passage header.  The returned offsets
    are into ``page_text`` and are used by the stimulus-linker to pair
    each passage with the immediately-following question(s).
    """
    headers = list(_PASSAGE_HEADER.finditer(page_text))
    if not headers:
        return []

    # End-of-passage candidates: next passage header, or next question
    # header — whichever comes first.
    q_header_starts = [m.start() for m in _Q_HEADER.finditer(page_text)]
    q_header_starts += [m.start() for m in _Q_HEADER_ALT.finditer(page_text)]
    q_header_starts.sort()

    passages: List[Tuple[str, str, int, int]] = []
    for i, h in enumerate(headers):
        title = h.group(2).strip() or h.group(1).strip()
        body_start = h.end()
        # End at the next passage, or the first question header
        # that appears after the passage header.
        next_passage = headers[i + 1].start() if i + 1 < len(headers) else len(page_text)
        next_q = next((s for s in q_header_starts if s > h.start()), len(page_text))
        body_end = min(next_passage, next_q)
        body = page_text[body_start:body_end].strip()
        if body:
            passages.append((title, body, h.start(), body_end))
    return passages


# ── Top-level parser ──────────────────────────────────────────────────

def parse_markdown_pages(pages: List[Tuple[int, str]]) -> List[ETSQuestion]:
    """Convert per-page markdown into a list of ETSQuestion records.

    The parser maintains a rolling "current measure" and the most
    recent passage (if any) so that RC items land with their stimulus
    attached.  It is deliberately tolerant: malformed blocks are
    skipped, not raised, so one bad page can't abort a full-book run.
    """
    items: List[ETSQuestion] = []
    current_measure: Optional[str] = None
    # Track the most recent passage so RC items within ~1 page of
    # their passage can link back to it.
    last_passage: Optional[Tuple[str, str, int]] = None  # (title, body, page_idx)

    for page_idx, page_text in pages:
        current_measure = _page_measure(page_text, current_measure)

        # Refresh passage state — always track the *last* passage seen
        # on or before this page.
        page_passages = _parse_passages(page_text)
        if page_passages:
            title, body, _, _ = page_passages[-1]
            last_passage = (title, body, page_idx)

        for qnum, diff, body in _split_question_blocks(page_text):
            options, stem = _parse_options(body)
            correct_labels, numeric_value = _parse_answer(body)

            # Figure-bearing signal: any of
            #   (a) a markdown image link in the block
            #   (b) a figure/diagram/graph mention in the stem
            #   (c) same mention in the attached passage (the passage
            #       will be linked below if this turns out to be RC).
            stim_for_figure = ""
            if last_passage and page_idx - last_passage[2] <= 1:
                stim_for_figure = last_passage[1]
            has_figure = (
                bool(_FIGURE_LINK.search(body))
                or bool(_FIGURE_HINTS.search(stem))
                or bool(_FIGURE_HINTS.search(stim_for_figure))
            )

            measure = _infer_measure(body, current_measure or "verbal")
            subtype = classify_subtype(
                body, measure, options, correct_labels, has_figure
            )

            # Attach the nearest preceding passage to RC-looking items.
            stimulus_text = ""
            stimulus_title = ""
            if last_passage and measure == "verbal" and subtype in (
                "mcq_single",
                "mcq_multi",
            ):
                # Only attach if the passage is on the same page or the
                # immediately-preceding one — anything farther is stale.
                if page_idx - last_passage[2] <= 1:
                    stimulus_title = last_passage[0]
                    stimulus_text = last_passage[1]
                    # Promote to rc_single / rc_multi.
                    subtype = "rc_multi" if subtype == "mcq_multi" else "rc_single"

            items.append(
                ETSQuestion(
                    number=qnum,
                    measure=measure,
                    subtype=subtype,
                    prompt=stem,
                    options=options,
                    correct_labels=correct_labels,
                    difficulty=diff,
                    numeric_value=numeric_value,
                    has_figure=has_figure,
                    stimulus_text=stimulus_text,
                    stimulus_title=stimulus_title,
                    source_anchor=f"q{qnum:03d}",
                    page_index=page_idx,
                )
            )
    return items


# ── Ebook -> markdown -> parse ────────────────────────────────────────

def extract_from_ebook(ebook_path: Path,
                       workdir: Optional[Path] = None
                       ) -> List[ETSQuestion]:
    """End-to-end: ebook file -> markdown -> parsed ETSQuestion records.

    Chooses the PDF or EPUB codepath based on the file extension.
    A scratch directory is created for the intermediate markdown and
    cleaned up on success; pass ``workdir`` to keep the artefacts
    (useful for debugging).
    """
    from scripts.lib.marker_pipeline import (
        extract_pdf_to_markdown,
        extract_epub_to_markdown,
        extractor_available,
    )

    if not extractor_available():
        raise RuntimeError(
            "pymupdf4llm / pymupdf not installed. "
            "Run: venv/bin/pip install pymupdf4llm"
        )

    cleanup = False
    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix="ets_og_extract_"))
        cleanup = True
    try:
        ext = ebook_path.suffix.lower()
        if ext == ".pdf":
            extract_pdf_to_markdown(ebook_path, workdir)
        elif ext == ".epub":
            extract_epub_to_markdown(ebook_path, workdir)
        else:
            raise ValueError(
                f"Unsupported ebook format: {ext} "
                "(expected .pdf or .epub)"
            )
        pages = _load_pages(workdir)
        return parse_markdown_pages(pages)
    finally:
        if cleanup:
            shutil.rmtree(workdir, ignore_errors=True)


# ── DB import ─────────────────────────────────────────────────────────

def import_to_db(items: List[ETSQuestion]) -> Tuple[int, int]:
    """Insert parsed questions into the DB with status='candidate'.

    Idempotent via ``(source, source_anchor)``.  Re-running with the
    same items skips existing rows.  Returns ``(inserted, skipped)``.

    Figure-bearing items are tagged via ``figure_refs`` and the
    provenance blob so that the existing vision-audit pipeline picks
    them up on its next pass (see docs/figure_audit_2026_05_11.md).

    Phase 1.4 hook: every candidate is run through the two-stage dedup
    service before insert. Items the service flags as duplicates of an
    existing live question are dropped (with a structured-log entry).
    """
    from models.database import (
        db, init_db, Question, QuestionOption, NumericAnswer, Stimulus,
    )
    from services.dedup import get_dedup_service
    init_db()
    db.connect(reuse_if_open=True)

    dedup_svc = get_dedup_service()

    inserted = 0
    skipped = 0
    with db.atomic():
        for item in items:
            exists = (
                Question.select()
                .where((Question.source == SOURCE_TAG)
                       & (Question.source_anchor == item.source_anchor))
                .first()
            )
            if exists:
                skipped += 1
                continue

            # Dedup against the live bank (Phase 1.4). For RC items the
            # stimulus text is fed in so the embedding stage gets the
            # discriminating prompt + a passage head.
            opt_texts = [t for (_lbl, t) in item.options]
            dup_qid = dedup_svc.find_dup_for(
                prompt=item.prompt,
                stimulus_content=item.stimulus_text or "",
                options=opt_texts,
                source=SOURCE_TAG,
            )
            if dup_qid is not None:
                skipped += 1
                continue

            # Optionally materialise a Stimulus row for RC items.
            stimulus_row = None
            if item.stimulus_text:
                stimulus_row = Stimulus.create(
                    stimulus_type="passage",
                    title=item.stimulus_title or "",
                    content=item.stimulus_text,
                )

            provenance_payload = {
                "pipeline": "ets_og_3rd",
                "page_index": item.page_index,
                "official_difficulty": item.difficulty,
                "has_figure": item.has_figure,
                # Downstream vision-audit hook.  The actual audit is
                # run post-ingest by services/figure_audit/*.
                "figure_audit_pending": bool(item.has_figure),
            }

            q = Question.create(
                measure=item.measure,
                subtype=item.subtype,
                stimulus=stimulus_row,
                prompt=item.prompt,
                difficulty_target=item.difficulty_target,
                time_target_seconds=_default_time(item.subtype),
                concept_tags=json.dumps(["ets_og_3rd"]),
                source=SOURCE_TAG,
                source_anchor=item.source_anchor,
                provenance="imported",
                status="candidate",  # human review required before live
                provenance_json=json.dumps(provenance_payload),
                explanation=item.explanation or "",
                # Signal the vision audit via figure_refs (empty list if none)
                figure_refs=json.dumps(
                    ["pending-audit"] if item.has_figure else []
                ),
            )

            for label, otext in item.options:
                QuestionOption.create(
                    question=q,
                    option_label=label,
                    option_text=otext,
                    is_correct=(label in item.correct_labels),
                )

            if item.numeric_value is not None:
                NumericAnswer.create(
                    question=q,
                    exact_value=item.numeric_value,
                    mode="decimal",
                )

            inserted += 1

    return inserted, skipped


def _default_time(subtype: str) -> int:
    """Per-subtype time target (seconds).  Matches the Phase 2 engine."""
    return {
        "qc": 90,
        "sentence_equiv": 75,
        "text_completion": 90,
        "mcq_single": 90,
        "mcq_multi": 105,
        "rc_single": 120,
        "rc_multi": 150,
        "numeric_entry": 120,
        "data_interp": 150,
        "awa_issue": 1800,
        "awa_argument": 1800,
    }.get(subtype, 90)


# ── Summarizer ────────────────────────────────────────────────────────

def summarize(items: List[ETSQuestion]) -> Dict[str, object]:
    """Return a structured summary + print a human-readable report."""
    by_measure: Dict[str, int] = {}
    by_subtype: Dict[str, int] = {}
    by_difficulty: Dict[str, int] = {}
    with_figures = 0
    for q in items:
        by_measure[q.measure] = by_measure.get(q.measure, 0) + 1
        by_subtype[q.subtype] = by_subtype.get(q.subtype, 0) + 1
        key = (q.difficulty or "unlabeled").lower()
        by_difficulty[key] = by_difficulty.get(key, 0) + 1
        if q.has_figure:
            with_figures += 1

    print(f"\nParsed {len(items)} ETS OG 3rd ed. items")
    print(f"  by measure:    {dict(sorted(by_measure.items()))}")
    print(f"  by subtype:    {dict(sorted(by_subtype.items()))}")
    print(f"  by difficulty: {dict(sorted(by_difficulty.items()))}")
    print(f"  figure-bearing: {with_figures} (routed through vision audit)")

    return {
        "total": len(items),
        "by_measure": by_measure,
        "by_subtype": by_subtype,
        "by_difficulty": by_difficulty,
        "with_figures": with_figures,
    }


# ── CLI ───────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract ETS Official Guide to the GRE General Test (3rd ed.) "
                    "into the Question / QuestionOption / NumericAnswer / Stimulus "
                    "schema with status='candidate'.",
    )
    parser.add_argument(
        "--ebook", type=Path, required=True,
        help="Path to the ETS OG 3rd ed. ebook (.epub or .pdf). "
             "For a plumbing smoke-test, point this at "
             "tests/fixtures/fake_ets_og.pdf.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and summarize but do not write to the DB.",
    )
    parser.add_argument(
        "--tag", default=f"source={SOURCE_TAG}",
        help=f"Source tag (default: source={SOURCE_TAG}). "
             "Override only if you're testing a new edition.",
    )
    args = parser.parse_args(argv)

    if not args.ebook.exists():
        print(f"ERROR: ebook not found: {args.ebook}", file=sys.stderr)
        return 2

    print(f"ETS OG 3rd ed. extraction — ebook: {args.ebook}")
    try:
        items = extract_from_ebook(args.ebook)
    except Exception as exc:
        print(f"ERROR: extraction failed: {exc}", file=sys.stderr)
        return 3

    summary = summarize(items)

    if args.dry_run:
        print("\n[dry-run] no DB writes performed")
        print(f"[dry-run-summary] {json.dumps(summary, sort_keys=True)}")
        return 0

    inserted, skipped = import_to_db(items)
    print(f"\nImported: {inserted}")
    print(f"Skipped (already present): {skipped}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
