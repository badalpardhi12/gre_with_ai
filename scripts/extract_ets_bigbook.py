#!/usr/bin/env python3
"""
ETS GRE Big Book extraction pipeline (Phase 4 · D2).

SOURCE
    "Practicing to Take the General Test, 10th Edition" (ETS, 1999) —
    often called the *GRE Big Book*. 27 retired paper-form GRE exams,
    ~2000-3000 usable items after discarding subtypes that were retired
    from the modern GRE (antonyms, analogies, and the entire analytical
    reasoning section).

    Copies of the PDF are publicly mirrored in several archive sites.
    This extractor takes a local path to the PDF -- no network.

    NOTE ON LEGALITY: The Big Book is out of print, the ETS catalog no
    longer sells it, and this is a research / personal-study project.
    We ingest the items as *candidates* (``status='candidate'``) — they
    are not redistributed, only used to populate a local SQLite DB for
    offline practice on the operator's own machine.

FORMAT MAPPING (legacy GRE → modern GRE)
    Verbal
      Reading Comprehension      → ``rc_single``      KEEP
      Sentence Completion        → ``text_completion``KEEP (best modern analogue)
      Antonyms                   → (dropped)          OBSOLETE
      Analogies                  → (dropped)          OBSOLETE
    Quantitative
      Problem Solving            → ``mcq_single``     KEEP
      Quantitative Comparison    → ``qc``             KEEP
      Data Interpretation        → ``data_interp``    KEEP (stimulus-linked)
    Analytical
      Logic games / analytical   → (dropped)          OBSOLETE (not on modern GRE)

DIFFICULTY PRIOR
    The Big Book does not label per-item difficulty. However it's well
    documented (in Kaplan/Manhattan prep guides and confirmed against
    ETS's own psychometric reports on the paper-based test) that within
    each section items are *roughly* arranged in increasing difficulty.
    We use position quartile as a weak prior:

        quartile 1   (first 25%)          → difficulty_target = 2
        quartile 2   (25-50%)             → difficulty_target = 3
        quartile 3   (50-75%)             → difficulty_target = 3
        quartile 4   (top 25%)            → difficulty_target = 4

    All items land as ``status='candidate'``; a human SME pass + the
    IRT recalibration flow (P4.E3) refines this later. The prior only
    controls the initial theta anchor for the rating engine, not the
    live adaptive routing.

USAGE
    # Dry-run against a small synthetic fixture (no DB writes)
    venv/bin/python scripts/extract_ets_bigbook.py \\
        --pdf tests/fixtures/fake_bigbook.pdf \\
        --dry-run

    # Full ingest, tests 1-10 only, keeping all subtypes
    venv/bin/python scripts/extract_ets_bigbook.py \\
        --pdf /path/to/gre_big_book.pdf \\
        --tests 1-10

    # Default behaviour drops antonyms/analogies/analytical
    venv/bin/python scripts/extract_ets_bigbook.py \\
        --pdf /path/to/gre_big_book.pdf

PIPELINE
    1. ``scripts.lib.marker_pipeline.extract_pdf_to_markdown`` converts
       the PDF to per-page markdown.
    2. This module splits the markdown into *tests* (``Test 1`` …
       ``Test 27`` headers) and, within each test, into *sections*
       (``Section 1`` … ``Section 7``).
    3. A lightweight per-subtype classifier looks at section headers
       and stem shape (e.g. a stem ending in ``_____`` → sentence
       completion; a two-column "Column A / Column B" prelude → QC).
    4. An answer-key block at the end of each test (the standard Big
       Book layout has one per test) is parsed into
       ``{question_number: letter}``.
    5. Idempotent upsert into Question + QuestionOption (+ Stimulus
       for RC passages and DI charts) keyed on
       ``(source='ets_big_book_tNN', source_anchor='sS_qNN')``.

    Each test is committed in its own ``db.atomic()`` block, so a
    mid-file crash leaves a clean partial state and a re-run picks up
    where we left off.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Make the project root importable when run as a script.
_THIS = Path(__file__).resolve()
PROJECT_ROOT = _THIS.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Subtype classification ────────────────────────────────────────────

# Kept subtypes (modern GRE analogues)
SUBTYPE_RC = "rc_single"
SUBTYPE_TC = "text_completion"          # sentence completion → TC
SUBTYPE_PS = "mcq_single"               # quant problem solving
SUBTYPE_QC = "qc"                       # quantitative comparison
SUBTYPE_DI = "data_interp"              # data interpretation

# Obsolete legacy subtypes — filtered out when --skip-obsolete is set.
SUBTYPE_ANTONYM = "legacy_antonym"
SUBTYPE_ANALOGY = "legacy_analogy"
SUBTYPE_ANALYTICAL = "legacy_analytical"

OBSOLETE_SUBTYPES = {
    SUBTYPE_ANTONYM,
    SUBTYPE_ANALOGY,
    SUBTYPE_ANALYTICAL,
}

# Measure mapping
SUBTYPE_MEASURE = {
    SUBTYPE_RC: "verbal",
    SUBTYPE_TC: "verbal",
    SUBTYPE_PS: "quant",
    SUBTYPE_QC: "quant",
    SUBTYPE_DI: "quant",
    SUBTYPE_ANTONYM: "verbal",
    SUBTYPE_ANALOGY: "verbal",
    SUBTYPE_ANALYTICAL: "verbal",  # nominal; obsolete
}


# ── Parsed record shapes ──────────────────────────────────────────────

@dataclass
class BigBookQuestion:
    """One item parsed out of a Big Book test section."""
    test_num: int                # 1..27
    section_num: int             # 1..7 within the test
    number: int                  # 1-based within its section
    section_size: int            # N items in the section (for quartile calc)
    subtype: str                 # see SUBTYPE_* constants
    prompt: str
    options: List[Tuple[str, str]] = field(default_factory=list)
    correct_label: Optional[str] = None
    stimulus_text: str = ""      # non-empty for RC passages and DI blurbs
    has_figure: bool = False

    @property
    def source(self) -> str:
        return f"ets_big_book_t{self.test_num:02d}"

    @property
    def source_anchor(self) -> str:
        return f"s{self.section_num}_q{self.number:02d}"

    @property
    def measure(self) -> str:
        return SUBTYPE_MEASURE.get(self.subtype, "verbal")

    @property
    def is_obsolete(self) -> bool:
        return self.subtype in OBSOLETE_SUBTYPES

    @property
    def difficulty_target(self) -> int:
        """Position-within-section quartile as a weak prior.

        Big Book items are arranged in increasing difficulty within
        each section; we map quartile → 1-5 band:
            Q1 → 2, Q2 → 3, Q3 → 3, Q4 → 4.
        The mid two quartiles collapse because the middle of a Big
        Book section is by convention the plateau.
        """
        if self.section_size <= 0:
            return 3
        # Position expressed as fraction ∈ (0, 1].
        frac = self.number / float(self.section_size)
        if frac <= 0.25:
            return 2
        if frac <= 0.75:
            return 3
        return 4


# ── Markdown → tests → sections → questions ───────────────────────────

# Test header: a line that says "Test 1", "TEST 1", "GRE Test 1", etc.
_TEST_HDR = re.compile(r"^\s*(?:GRE\s+)?TEST\s+(\d{1,2})\b", re.IGNORECASE)

# Section header: "Section 1", "SECTION 2 Verbal", etc.
_SECTION_HDR = re.compile(r"^\s*SECTION\s+(\d)\b(.*)$", re.IGNORECASE)

# Answer-key header. The Big Book prints one answer key per test
# toward the end; we accept several textual variants.
_KEY_HDR = re.compile(
    r"(answer\s+key|correct\s+answers|answers\s+to\s+test)",
    re.IGNORECASE,
)

# Subtype hints derived from section intros / titles.
_SECTION_SUBTYPE_HINTS = [
    (re.compile(r"analytical", re.IGNORECASE),     SUBTYPE_ANALYTICAL),
    (re.compile(r"quantitative", re.IGNORECASE),   None),  # resolved per-question
    (re.compile(r"verbal", re.IGNORECASE),         None),  # resolved per-question
]

# Per-question heuristics when the section header doesn't disambiguate.
_ANTONYM_MARKER = re.compile(r"most\s+nearly\s+opposite", re.IGNORECASE)
_ANALOGY_MARKER = re.compile(r"::|is\s+to\s+.*\s+as\s+", re.IGNORECASE)
_SENTCOMP_MARKER = re.compile(r"_{3,}")  # three+ underscores = blank
_QC_MARKER = re.compile(r"column\s+A\s*[:|]?\s*column\s+B", re.IGNORECASE)
_RC_MARKER = re.compile(r"passage|according to the passage|author", re.IGNORECASE)
_DI_MARKER = re.compile(r"according to the (chart|graph|table)|based on the (chart|graph|table)",
                        re.IGNORECASE)
_FIGURE_HINTS = re.compile(
    r"\b(figure|diagram|graph|chart|shown below|table below)\b",
    re.IGNORECASE,
)

# A question starts with a number like "1. " or "12. " at start of line.
_Q_START = re.compile(r"^\s*(\d{1,2})\.\s+(.*\S.*)$")
# Option marker: "(A) foo" or "A. foo" at start of line.
_OPT_MARKER = re.compile(r"^\s*[(\[]?([A-E])[)\].]\s+(.*\S.*)$")

# Answer-key row forms we accept:
#   "1. A"
#   "1  A"
#   "  1)  A"
# And lines with multiple "<n>. <letter>" pairs separated by
# whitespace (the Big Book packs 4-5 keys per printed line):
#   "1. A   2. B   3. C   4. D"
# Used with ``finditer`` so we can harvest all pairs on a line.
_KEY_ROW = re.compile(r"(\d{1,2})[\.\)\s]+([A-E])\b")


def _load_markdown(md_files: List[Path]) -> str:
    """Concatenate per-page markdown into one big stream with page markers."""
    chunks = []
    for f in md_files:
        chunks.append(f.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def _split_tests(markdown: str) -> Dict[int, str]:
    """Split the full book markdown into {test_num: test_body}.

    A test body runs from its header to the next test header (or EOF).
    Tests with numbers outside 1..27 are discarded as header false
    positives.
    """
    lines = markdown.splitlines()
    tests: Dict[int, List[str]] = {}
    current: Optional[int] = None
    for ln in lines:
        m = _TEST_HDR.match(ln)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 27:
                current = n
                tests.setdefault(current, [])
                continue
        if current is not None:
            tests[current].append(ln)
    return {k: "\n".join(v) for k, v in tests.items()}


def _split_sections(test_body: str) -> List[Tuple[int, str, str]]:
    """Split one test into [(section_num, section_header_tail, body), …]."""
    lines = test_body.splitlines()
    sections: List[Tuple[int, str, List[str]]] = []
    current_num: Optional[int] = None
    current_hdr_tail = ""
    current_body: List[str] = []

    for ln in lines:
        m = _SECTION_HDR.match(ln)
        if m:
            if current_num is not None:
                sections.append((current_num, current_hdr_tail, current_body))
            current_num = int(m.group(1))
            current_hdr_tail = (m.group(2) or "").strip()
            current_body = []
            continue
        if current_num is not None:
            current_body.append(ln)
    if current_num is not None:
        sections.append((current_num, current_hdr_tail, current_body))

    return [(n, hdr, "\n".join(b)) for n, hdr, b in sections]


def _split_key_from_body(section_body: str) -> Tuple[str, str]:
    """Return (question_block, key_block).

    If an answer-key header is found inside the section body, we split
    on it. The Big Book usually puts keys in an appendix *per test*
    rather than per section, so the key_block may come back empty here
    (we then pull it from the test-level key further down).
    """
    lines = section_body.splitlines()
    for i, ln in enumerate(lines):
        if _KEY_HDR.search(ln):
            return ("\n".join(lines[:i]), "\n".join(lines[i + 1:]))
    return (section_body, "")


def parse_answer_key_text(text: str) -> Dict[int, str]:
    """Return {question_number: letter}. Blank/unparseable lines are skipped.

    Accepts multiple ``<n>. <letter>`` pairs per line (Big Book keys
    typically pack 4-5 pairs per printed line).
    """
    out: Dict[int, str] = {}
    for ln in text.splitlines():
        for m in _KEY_ROW.finditer(ln):
            qnum = int(m.group(1))
            letter = m.group(2).upper()
            if 1 <= qnum <= 40:
                out.setdefault(qnum, letter)
    return out


def _extract_test_level_keys(test_body: str) -> Dict[int, Dict[int, str]]:
    """Return {section_num: {qnum: letter}} for the answer keys in a test.

    The Big Book key section often looks like::

        Answer Key for Test 3
        Section 1                    Section 2
        1. C   11. A                  1. E   11. D
        2. B   12. D                  2. A   12. B
        ...

    We accept two flavors:
      - Key header immediately followed by a per-section subheader that
        says "Section N" (we attribute rows to that section until the
        next "Section N" subheader).
      - A simple "Test N Answers" list with a ``(section, q)`` pair
        notation — we fall back to a single-section bucket if nothing
        labels the section explicitly.
    """
    m = _KEY_HDR.search(test_body)
    if not m:
        return {}
    key_text = test_body[m.end():]

    # Cut off at the next "Test N" header (in case multiple tests'
    # keys share an appendix) — _split_tests already bounded us to one
    # test, but belt-and-braces.
    end_m = _TEST_HDR.search(key_text)
    if end_m:
        key_text = key_text[:end_m.start()]

    buckets: Dict[int, Dict[int, str]] = {}
    current_section: Optional[int] = 1  # default if no subheader
    for ln in key_text.splitlines():
        # Is this line a pure "Section N" subheader (no key rows on it)?
        key_matches = list(_KEY_ROW.finditer(ln))
        sec_m = _SECTION_HDR.match(ln)
        if sec_m and not key_matches:
            current_section = int(sec_m.group(1))
            buckets.setdefault(current_section, {})
            continue
        sec2 = re.search(r"\bSection\s+(\d)\b", ln, re.IGNORECASE)
        if sec2 and not key_matches:
            current_section = int(sec2.group(1))
            buckets.setdefault(current_section, {})
            continue
        for m2 in key_matches:
            q = int(m2.group(1))
            letter = m2.group(2).upper()
            if current_section is None:
                current_section = 1
            buckets.setdefault(current_section, {})
            buckets[current_section].setdefault(q, letter)
    return buckets


def _classify_section(header_tail: str, body: str) -> str:
    """Best-effort subtype classification from section header + body.

    We start from the header ("Analytical Ability", "Verbal",
    "Quantitative") and then drill into the first question to
    disambiguate subtype. Returns one of SUBTYPE_*.
    """
    # Analytical Ability / Analytical Reasoning → drop
    if re.search(r"analytical", header_tail, re.IGNORECASE):
        return SUBTYPE_ANALYTICAL

    # Peek at the first 2000 chars of body to spot structural markers.
    peek = body[:2000]

    # Quantitative subtypes
    if re.search(r"quantitative|mathematic", header_tail, re.IGNORECASE):
        if _QC_MARKER.search(peek):
            return SUBTYPE_QC
        if _DI_MARKER.search(peek):
            return SUBTYPE_DI
        return SUBTYPE_PS  # default quant → problem solving

    # Verbal subtypes — a Verbal section can MIX subtypes (ETS packs
    # TC, RC, antonyms, analogies together in a single section). Emit
    # a "verbal_mixed" sentinel and resolve per-question downstream.
    if re.search(r"verbal", header_tail, re.IGNORECASE) or not header_tail:
        return "verbal_mixed"

    return SUBTYPE_PS


def _classify_question(section_subtype: str, block: str) -> str:
    """Per-item refinement for verbal blocks where section-level
    classification is ambiguous ("verbal_mixed"). Quant subtypes don't
    mix within a section, so they're passed through unchanged.

    Heuristic order (applied to the block text):
      1. Antonym marker ("most nearly opposite") → antonym
      2. Analogy marker ("::" or "is to X as") → analogy
      3. Blanks ("____") in the stem → text_completion
      4. References to "passage"/"line N"/"according to the" → RC
      5. Fallback → RC (safer than an obsolete bucket)
    """
    if section_subtype in {SUBTYPE_QC, SUBTYPE_DI, SUBTYPE_PS,
                           SUBTYPE_ANALYTICAL, SUBTYPE_RC,
                           SUBTYPE_TC, SUBTYPE_ANTONYM, SUBTYPE_ANALOGY}:
        return section_subtype

    # section_subtype == "verbal_mixed" — inspect the item itself.
    if _ANTONYM_MARKER.search(block):
        return SUBTYPE_ANTONYM
    if _ANALOGY_MARKER.search(block):
        return SUBTYPE_ANALOGY
    # A bare ALL-CAPS word followed by a colon and 4-5 options is the
    # classic Big Book antonym shape even without the "most nearly
    # opposite" instruction (that line appears once per section).
    first_line = block.splitlines()[0] if block.splitlines() else ""
    if re.match(r"^[A-Z]{3,}\s*[:.]\s*$", first_line.strip()):
        return SUBTYPE_ANTONYM
    # Analogy shape: "WORD :: WORD :" or "WORD : WORD ::"
    if re.search(r"[A-Z]{3,}\s*::?\s*[A-Z]{3,}", first_line):
        return SUBTYPE_ANALOGY
    if _SENTCOMP_MARKER.search(block):
        return SUBTYPE_TC
    if re.search(r"\bpassage\b|\bline\s+\d+\b|according to", block,
                 re.IGNORECASE):
        return SUBTYPE_RC
    return SUBTYPE_RC


def _parse_question_blocks(body: str) -> List[Tuple[int, str]]:
    """Split a section body into [(qnum, block_text), …].

    We accept a question boundary whenever a line starts with "<n>. "
    and ``n`` is the next monotonically-increasing integer (protects
    against stray numerics like "In 1985, ...").
    """
    lines = body.splitlines()
    blocks: List[Tuple[int, List[str]]] = []
    current_num: Optional[int] = None
    current_lines: List[str] = []
    last_accepted = 0

    for ln in lines:
        m = _Q_START.match(ln)
        if m:
            candidate = int(m.group(1))
            # Accept monotonic increases only. Allow +1 (the typical case).
            if 1 <= candidate <= 40 and candidate == last_accepted + 1:
                if current_num is not None:
                    blocks.append((current_num, current_lines))
                current_num = candidate
                current_lines = [m.group(2)]
                last_accepted = candidate
                continue
        if current_num is not None:
            current_lines.append(ln)

    if current_num is not None:
        blocks.append((current_num, current_lines))

    return [(n, "\n".join(b)) for n, b in blocks]


def _split_stem_and_options(block: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Pull option lines out of the block. Supports 4-option and
    5-option items. Returns (stem, [(label, text), …])."""
    lines = block.splitlines()
    stem_lines: List[str] = []
    options: List[Tuple[str, str]] = []
    seen_labels: set = set()

    for ln in lines:
        m = _OPT_MARKER.match(ln)
        if m:
            lbl = m.group(1).upper()
            txt = m.group(2).strip()
            # Allow A/B/C/D/E in increasing order. Skip duplicates.
            if lbl not in seen_labels:
                options.append((lbl, txt))
                seen_labels.add(lbl)
            continue
        if not options:
            stem_lines.append(ln)
        else:
            # Continuation of the last option's text.
            if options:
                last_lbl, last_txt = options[-1]
                options[-1] = (last_lbl, (last_txt + " " + ln.strip()).strip())

    stem = "\n".join(stem_lines).strip()
    return stem, options


def _parse_section_stimulus(body: str, subtype: str) -> Tuple[str, str]:
    """For RC and DI sections, the stimulus sits ABOVE the first
    question. Return (stimulus_text, remainder).

    We split at the first "<n>. " line. If no question line is found
    (weird section), returns ("", body) so the caller can skip.
    """
    if subtype not in {SUBTYPE_RC, SUBTYPE_DI}:
        return ("", body)
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if _Q_START.match(ln):
            return ("\n".join(lines[:i]).strip(), "\n".join(lines[i:]))
    return ("", body)


# ── Full test extraction ──────────────────────────────────────────────

def extract_test(test_num: int, test_body: str) -> List[BigBookQuestion]:
    """Parse a single numbered test into a list of question records."""
    items: List[BigBookQuestion] = []

    # Test-level answer key (applies across all sections in this test).
    test_keys = _extract_test_level_keys(test_body)

    # Strip the key region off the body before splitting into sections
    # (otherwise the key section becomes a pseudo-section).
    key_m = _KEY_HDR.search(test_body)
    trimmed_body = test_body[:key_m.start()] if key_m else test_body

    sections = _split_sections(trimmed_body)
    for section_num, header_tail, raw_body in sections:
        section_subtype = _classify_section(header_tail, raw_body)

        # Sections that front-load a stimulus (RC passage, DI chart
        # blurb) need the stimulus carved off the top.
        stimulus_text, body_for_qs = _parse_section_stimulus(
            raw_body, section_subtype
        )

        blocks = _parse_question_blocks(body_for_qs)
        section_size = len(blocks)
        if section_size == 0:
            continue

        # Resolve the per-section answer key.
        section_key = test_keys.get(section_num, {})

        for qnum, block in blocks:
            stem, options = _split_stem_and_options(block)
            if not stem:
                continue
            # Obsolete analytical items still parse but are tagged so
            # --skip-obsolete can drop them. Analytical rarely has
            # 4-option MCQs — be lenient.
            item_subtype = _classify_question(section_subtype, block)

            # Analytical items + antonyms need ≥2 options to be meaningful.
            if not options and item_subtype not in {SUBTYPE_ANALYTICAL}:
                continue

            item = BigBookQuestion(
                test_num=test_num,
                section_num=section_num,
                number=qnum,
                section_size=section_size,
                subtype=item_subtype,
                prompt=stem,
                options=options,
                correct_label=section_key.get(qnum),
                stimulus_text=stimulus_text,
                has_figure=bool(_FIGURE_HINTS.search(stem)
                                or _FIGURE_HINTS.search(stimulus_text)),
            )
            items.append(item)

    return items


def extract_book(
    md_files: List[Path],
    test_range: Tuple[int, int],
) -> List[BigBookQuestion]:
    """Run the pipeline across a range of tests. ``test_range`` is inclusive."""
    lo, hi = test_range
    markdown = _load_markdown(md_files)
    tests = _split_tests(markdown)
    items: List[BigBookQuestion] = []
    for t in sorted(tests):
        if not (lo <= t <= hi):
            continue
        items.extend(extract_test(t, tests[t]))
    return items


# ── DB import ─────────────────────────────────────────────────────────

def import_to_db(
    items: List[BigBookQuestion],
    *,
    skip_obsolete: bool = True,
) -> Tuple[int, int, int]:
    """Insert parsed questions. Returns (inserted, skipped_existing, dropped_obsolete).

    Commits atomically per-test so a crash leaves a clean partial state.
    Idempotent via ``(source, source_anchor)``.
    """
    from models.database import (
        db, init_db, Question, QuestionOption, Stimulus,
    )
    init_db()
    db.connect(reuse_if_open=True)

    # Group by test so each test commits atomically.
    by_test: Dict[int, List[BigBookQuestion]] = {}
    for it in items:
        by_test.setdefault(it.test_num, []).append(it)

    inserted = 0
    skipped_existing = 0
    dropped_obsolete = 0

    for test_num, test_items in sorted(by_test.items()):
        with db.atomic():
            # Cache per-test stimuli by (section_num, content) to avoid
            # duplicating the RC passage across its 4-7 child questions.
            stim_cache: Dict[Tuple[int, str], int] = {}

            for item in test_items:
                if skip_obsolete and item.is_obsolete:
                    dropped_obsolete += 1
                    continue
                if item.correct_label is None and not item.is_obsolete:
                    # No key match → can't grade. Skip rather than
                    # upsert a half-broken row.
                    continue

                exists = (
                    Question.select()
                    .where((Question.source == item.source)
                           & (Question.source_anchor == item.source_anchor))
                    .first()
                )
                if exists:
                    skipped_existing += 1
                    continue

                # Upsert stimulus for RC / DI sections.
                stim_id = None
                if item.stimulus_text:
                    cache_key = (item.section_num, item.stimulus_text[:120])
                    if cache_key in stim_cache:
                        stim_id = stim_cache[cache_key]
                    else:
                        stim_type = "passage" if item.subtype == SUBTYPE_RC else "graph"
                        stim_row = Stimulus.create(
                            stimulus_type=stim_type,
                            title=f"Big Book Test {item.test_num} Section {item.section_num}",
                            content=item.stimulus_text,
                        )
                        stim_id = stim_row.id
                        stim_cache[cache_key] = stim_id

                provenance_payload = {
                    "pipeline": "ets_big_book",
                    "test_num": item.test_num,
                    "section_num": item.section_num,
                    "position": item.number,
                    "section_size": item.section_size,
                    "has_figure": item.has_figure,
                    "difficulty_prior": "position_quartile",
                }

                q = Question.create(
                    measure=item.measure,
                    subtype=item.subtype,
                    stimulus=stim_id,
                    prompt=item.prompt,
                    difficulty_target=item.difficulty_target,
                    time_target_seconds=90,
                    concept_tags=json.dumps(["ets_big_book"]),
                    source=item.source,
                    source_anchor=item.source_anchor,
                    provenance="imported",
                    status="candidate",
                    provenance_json=json.dumps(provenance_payload),
                )
                for label, otext in item.options:
                    QuestionOption.create(
                        question=q,
                        option_label=label,
                        option_text=otext,
                        is_correct=(label == item.correct_label),
                    )
                inserted += 1

    return inserted, skipped_existing, dropped_obsolete


# ── CLI ───────────────────────────────────────────────────────────────

def _parse_range(spec: str) -> Tuple[int, int]:
    """Parse "1-27" / "3-3" / "5" → (lo, hi)."""
    spec = spec.strip()
    if "-" in spec:
        a, b = spec.split("-", 1)
        return (int(a), int(b))
    n = int(spec)
    return (n, n)


def _summarize(items: List[BigBookQuestion], skip_obsolete: bool) -> Dict:
    by_test: Dict[int, int] = {}
    by_subtype: Dict[str, int] = {}
    obsolete_count = 0
    kept_count = 0
    for q in items:
        by_test[q.test_num] = by_test.get(q.test_num, 0) + 1
        by_subtype[q.subtype] = by_subtype.get(q.subtype, 0) + 1
        if q.is_obsolete:
            obsolete_count += 1
        else:
            kept_count += 1
    print(f"\nParsed {len(items)} items across {len(by_test)} test(s)")
    print(f"  by test:     {dict(sorted(by_test.items()))}")
    print(f"  by subtype:  {dict(sorted(by_subtype.items()))}")
    if skip_obsolete:
        print(f"  will drop {obsolete_count} obsolete items; keep {kept_count}")
    return {
        "total": len(items),
        "kept": kept_count,
        "dropped_obsolete": obsolete_count,
        "by_subtype": by_subtype,
        "tests": sorted(by_test.keys()),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract questions from the ETS GRE Big Book (10th ed., 1999)."
    )
    parser.add_argument("--pdf", required=True,
                        help="Path to the Big Book PDF.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and summarize but do not write to the DB.")
    parser.add_argument("--tests", default="1-27",
                        help="Inclusive test range to process, e.g. '1-10'. Default: 1-27.")
    parser.add_argument("--skip-obsolete", dest="skip_obsolete",
                        action="store_true", default=True,
                        help="Drop antonym/analogy/analytical items (default).")
    parser.add_argument("--keep-obsolete", dest="skip_obsolete",
                        action="store_false",
                        help="Keep antonym/analogy/analytical items in the import.")
    parser.add_argument("--workdir", default=None,
                        help="Reuse an existing markdown workdir instead of re-extracting.")
    args = parser.parse_args(argv)

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        print(f"ERROR: PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    test_range = _parse_range(args.tests)

    from scripts.lib.marker_pipeline import (
        extract_pdf_to_markdown, extractor_available,
    )
    if not extractor_available():
        print("ERROR: pymupdf4llm/pymupdf not installed.", file=sys.stderr)
        return 1

    # Extract to markdown (either a fresh temp dir, or the user-supplied one).
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        md_files = sorted(workdir.glob("pdf_page_*.md"))
        if not md_files:
            md_files = extract_pdf_to_markdown(pdf_path, workdir)
        cleanup = False
    else:
        workdir = Path(tempfile.mkdtemp(prefix="ets_bigbook_"))
        md_files = extract_pdf_to_markdown(pdf_path, workdir)
        cleanup = True

    try:
        print(f"ETS Big Book extraction — tests {test_range[0]}..{test_range[1]}")
        print(f"  PDF:     {pdf_path}")
        print(f"  workdir: {workdir}  ({len(md_files)} page file(s))")

        items = extract_book(md_files, test_range)
        summary = _summarize(items, args.skip_obsolete)

        if args.dry_run:
            print("\n[dry-run] no DB writes performed")
            print(f"[dry-run-summary] {json.dumps(summary)}")
            return 0

        inserted, skipped, dropped = import_to_db(
            items, skip_obsolete=args.skip_obsolete,
        )
        print(f"\nImported: {inserted}")
        print(f"Skipped (already present): {skipped}")
        print(f"Dropped (obsolete subtypes): {dropped}")
        return 0
    finally:
        if cleanup:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
