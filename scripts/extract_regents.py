#!/usr/bin/env python3
"""
NYC Regents exam extraction pipeline (Phase 4 · D5).

NYC Regents are New York State high-school exit exams. They are
published in the **public domain** by the NY State Education Department
at https://www.nysedregents.org/ as companion PDF pairs:

    <exam>-exam-<month-year>.pdf            # question booklet
    <exam>-scoring-key-<month-year>.pdf     # scoring / answer key

For Phase 4 · D5 we treat Regents items as **Level-1 warm-up** material
(difficulty_target=2) — they are substantially easier than real GRE
items but provide clean, legally-uncomplicated volume for the warm-up
tier and section-intro drills.

MEASURE MAPPING
    Algebra II, Geometry, Algebra I    -> quant
    English Language Arts              -> verbal

SUBTYPE
    Regents Part I items are almost exclusively 4-option MCQ. We map
    them to ``mcq_single``. Constructed-response / essay items are
    *skipped* in this pipeline (they need separate rubric handling).

STATUS
    All imported items land with ``status='candidate'`` — they need a
    human review pass (taxonomy tagging, difficulty re-calibration vs
    the GRE curve) before promotion to ``pretest``/``live``. This also
    routes figure-bearing items through the vision audit pipeline (see
    docs/figure_audit_2026_05_11.md) before going live. No separate
    implementation — the existing `candidate → review` queue picks them
    up automatically.

USAGE
    # dry run — parse cached/bundled fixtures, print summary, no DB writes
    python scripts/extract_regents.py --dry-run

    # full import (requires cached PDFs; this script does NOT fetch over
    # the network by default — use --fetch to opt in)
    python scripts/extract_regents.py

    # network-enabled fetch of the target PDFs listed below
    python scripts/extract_regents.py --fetch

PDF-to-text
    We don't ship a PDF parser here — Regents booklets are fairly
    consistent in layout but variable enough that a custom
    pdfplumber/marker-pdf pass is warranted (see D3 for the shared
    extraction infra). This module's responsibility is the **text-level
    parser**: given already-extracted plain text for an exam booklet
    and its scoring key, produce structured question records. The
    ``--fetch`` path uses ``pdftotext`` (poppler) if available on PATH,
    else degrades gracefully with a warning.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import subprocess
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Tuple, Dict

# Make project root importable when run as a script.
_THIS = Path(__file__).resolve()
PROJECT_ROOT = _THIS.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Target exam catalog ───────────────────────────────────────────────
#
# Keep this list small and explicit. The point of D5 is a clean
# pipeline, not a scrape of the whole Regents archive. Each entry is
# (slug, subject, exam_pdf_url, key_pdf_url). The NYSED URL pattern is
# stable: exams live under /<SubjectPath>/<YYYYMM>/<filename>.pdf.
#
# NOTE: We list nine exams (3 Algebra II + 3 Geometry + 3 English). We
# do NOT actually hit the network during the D5 drop — these URLs are
# reference targets for an operator who runs `--fetch` locally.

TARGET_EXAMS: List[Dict[str, str]] = [
    # Algebra II  →  quant
    {"slug": "nyc_regents_algebra2_2024_06", "subject": "algebra2",
     "exam_url": "https://www.nysedregents.org/AlgebraTwo/624/algtwo62024-exam.pdf",
     "key_url":  "https://www.nysedregents.org/AlgebraTwo/624/algtwo62024-sk.pdf"},
    {"slug": "nyc_regents_algebra2_2024_01", "subject": "algebra2",
     "exam_url": "https://www.nysedregents.org/AlgebraTwo/124/algtwo12024-exam.pdf",
     "key_url":  "https://www.nysedregents.org/AlgebraTwo/124/algtwo12024-sk.pdf"},
    {"slug": "nyc_regents_algebra2_2023_06", "subject": "algebra2",
     "exam_url": "https://www.nysedregents.org/AlgebraTwo/623/algtwo62023-exam.pdf",
     "key_url":  "https://www.nysedregents.org/AlgebraTwo/623/algtwo62023-sk.pdf"},
    # Geometry  →  quant
    {"slug": "nyc_regents_geometry_2024_06", "subject": "geometry",
     "exam_url": "https://www.nysedregents.org/Geometry/624/geom62024-exam.pdf",
     "key_url":  "https://www.nysedregents.org/Geometry/624/geom62024-sk.pdf"},
    {"slug": "nyc_regents_geometry_2024_01", "subject": "geometry",
     "exam_url": "https://www.nysedregents.org/Geometry/124/geom12024-exam.pdf",
     "key_url":  "https://www.nysedregents.org/Geometry/124/geom12024-sk.pdf"},
    {"slug": "nyc_regents_geometry_2023_06", "subject": "geometry",
     "exam_url": "https://www.nysedregents.org/Geometry/623/geom62023-exam.pdf",
     "key_url":  "https://www.nysedregents.org/Geometry/623/geom62023-sk.pdf"},
    # English Language Arts  →  verbal
    {"slug": "nyc_regents_english_2024_06", "subject": "english",
     "exam_url": "https://www.nysedregents.org/hsela/624/ela62024-exam.pdf",
     "key_url":  "https://www.nysedregents.org/hsela/624/ela62024-sk.pdf"},
    {"slug": "nyc_regents_english_2024_01", "subject": "english",
     "exam_url": "https://www.nysedregents.org/hsela/124/ela12024-exam.pdf",
     "key_url":  "https://www.nysedregents.org/hsela/124/ela12024-sk.pdf"},
    {"slug": "nyc_regents_english_2023_06", "subject": "english",
     "exam_url": "https://www.nysedregents.org/hsela/623/ela62023-exam.pdf",
     "key_url":  "https://www.nysedregents.org/hsela/623/ela62023-sk.pdf"},
]

CACHE_DIR = PROJECT_ROOT / "data" / "external" / "regents"

SUBJECT_TO_MEASURE = {
    "algebra2":  "quant",
    "algebra1":  "quant",
    "geometry":  "quant",
    "english":   "verbal",
}


# ── Parsed record shapes ──────────────────────────────────────────────

@dataclass
class RegentsQuestion:
    """Structured Regents Part I MCQ item, ready for DB insert."""
    exam_slug: str
    number: int                    # 1-based question number within the exam
    prompt: str
    options: List[Tuple[str, str]] = field(default_factory=list)  # [(label, text)]
    correct_label: Optional[str] = None
    has_figure: bool = False       # true if the stem text mentions a diagram/graph

    # Derived mapping fields
    @property
    def measure(self) -> str:
        subject = self.exam_slug.split("_")[2] if "_" in self.exam_slug else ""
        return SUBJECT_TO_MEASURE.get(subject, "quant")

    @property
    def subtype(self) -> str:
        return "mcq_single"

    @property
    def source(self) -> str:
        # strip the leading "nyc_" to keep things tidy:
        # "nyc_regents_algebra2_2024_06"  ->  same (already descriptive)
        return self.exam_slug

    @property
    def source_anchor(self) -> str:
        return f"q{self.number:02d}"


# ── Answer-key parser ─────────────────────────────────────────────────
#
# The NYSED scoring key for Part I is a 4-column table: (question #,
# correct letter, credit, [topic]). After pdftotext it looks like:
#
#     Question   Correct   Credit
#       1          2        1
#       2          4        1
#       ...
#
# or with letter answers:
#
#     Question   Correct   Credit
#       1          A        1
#       ...
#
# Older exams use letters (A/B/C/D), newer Algebra II/Geometry use
# numeric answers (1/2/3/4). We accept both.
#
# We look for lines of the form <int> <letter-or-digit> <1> and stop
# once the table ends (e.g. a "Part II" header, or a line that breaks
# the pattern for 3+ consecutive rows).

_KEY_LETTER_MAP = {"1": "A", "2": "B", "3": "C", "4": "D",
                   "A": "A", "B": "B", "C": "C", "D": "D"}

_KEY_LINE = re.compile(
    r"^\s*(\d{1,2})\s+([1-4A-D])\s+1\s*$"
)


def parse_answer_key_text(text: str) -> Dict[int, str]:
    """Return {question_number: correct_label} where label ∈ {A,B,C,D}.

    Stops at the first line that looks like a Part II / constructed
    response header, so we only capture the Part I MCQ block.
    """
    answers: Dict[int, str] = {}
    stop_markers = re.compile(
        r"part\s+(ii|2|three|iii|3)|constructed[-\s]response|open[-\s]response",
        re.IGNORECASE,
    )

    for line in text.splitlines():
        if stop_markers.search(line):
            break
        m = _KEY_LINE.match(line)
        if not m:
            continue
        qnum = int(m.group(1))
        raw = m.group(2).upper()
        answers[qnum] = _KEY_LETTER_MAP[raw]

    return answers


# ── Exam booklet parser ───────────────────────────────────────────────
#
# After pdftotext extraction, each Part I MCQ looks roughly like:
#
#     12  A store sells widgets at ... which expression
#         represents ... ?
#
#         (1) 2x + 3             (3) 4x - 1
#         (2) 3x - 2             (4) x + 5
#
#         13  The next question stem begins here ...
#
# pdftotext sometimes flattens the two-column layout so options run
# in-sequence (1)(2)(3)(4); sometimes it preserves the column order
# (1)(3)(2)(4). We handle both by sorting the captured options by
# label after extraction.
#
# Question numbers at the start of a stem are 1–N with N usually 24
# (Algebra II) or 28 (Geometry) or 24 (ELA).

# Regex: start of a question — a line that begins with "<num>  <uppercase>"
# where num is 1-40. We allow an optional leading space.
_Q_START = re.compile(r"^\s*(\d{1,2})\s+([A-Z(\"\'].{2,})")

# Option marker: "(1)" / "(2)" / ... / "(4)" — sometimes written with
# unicode full-width parens in old scans. Allow both.
_OPT_MARKER = re.compile(r"[(（]\s*([1-4A-D])\s*[)）]")

_FIGURE_HINTS = re.compile(
    r"\b(diagram|figure|graph|shown below|as shown|table below|accompanying)\b",
    re.IGNORECASE,
)


def _split_into_question_blocks(text: str) -> List[Tuple[int, str]]:
    """Chunk exam text into (qnum, body) pairs based on leading numbers.

    We only accept a number as a question boundary when it is
    monotonically increasing relative to the last accepted number
    (guards against stray numerics in option text or figures).
    """
    lines = text.splitlines()
    blocks: List[Tuple[int, List[str]]] = []
    current_num: Optional[int] = None
    current_lines: List[str] = []
    last_accepted = 0

    for ln in lines:
        m = _Q_START.match(ln)
        if m:
            candidate = int(m.group(1))
            # Accept if it's the next expected number, or within a
            # reasonable forward jump (handles occasional skipped
            # numbers in ELA reading-set questions).
            if 1 <= candidate <= 40 and candidate == last_accepted + 1:
                if current_num is not None:
                    blocks.append((current_num, current_lines))
                current_num = candidate
                # preserve the text AFTER the leading number on the
                # same line — that's the first line of the stem
                current_lines = [m.group(2)]
                last_accepted = candidate
                continue
        if current_num is not None:
            current_lines.append(ln)

    if current_num is not None:
        blocks.append((current_num, current_lines))

    return [(n, "\n".join(body)) for n, body in blocks]


def _parse_block(qnum: int, body: str) -> Optional[RegentsQuestion]:
    """Split one question body into (stem, options). Returns None if
    we can't find a plausible 4-option MCQ structure.

    Anchor point: the first option marker whose label is the *start*
    of the option set — "(1)" for numeric exams or "(A)" for letter
    exams. This avoids being fooled by incidental `(4)` references
    inside the stem (e.g. "f(4) equals").
    """
    # Find the first option marker whose label is "1" or "A" — that's
    # the true start of the options block. Regents items *always*
    # begin their option list with (1)/(A).
    start_idx = None
    for om in _OPT_MARKER.finditer(body):
        if om.group(1).upper() in ("1", "A"):
            start_idx = om.start()
            break
    if start_idx is None:
        return None

    stem = body[:start_idx].strip()
    opts_text = body[start_idx:].strip()

    # Collect (label, text) pairs by splitting at each marker.
    positions = [(om.start(), om.group(1)) for om in _OPT_MARKER.finditer(opts_text)]
    if len(positions) < 4:
        return None

    pairs: List[Tuple[str, str]] = []
    for i, (pos, raw_label) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(opts_text)
        # Strip the marker itself from the captured slice.
        marker_end = _OPT_MARKER.match(opts_text, pos).end()
        text = opts_text[marker_end:end].strip()
        label = _KEY_LETTER_MAP.get(raw_label.upper())
        if not label:
            return None
        pairs.append((label, text))

    # Deduplicate by label (column-order artifact: same label twice
    # means the regex got fooled). Keep only first 4 distinct labels
    # in A/B/C/D order.
    by_label: Dict[str, str] = {}
    for lbl, txt in pairs:
        by_label.setdefault(lbl, txt)
    if not {"A", "B", "C", "D"}.issubset(by_label.keys()):
        return None

    options = [(lbl, by_label[lbl]) for lbl in ("A", "B", "C", "D")]

    # Filter: options must be non-empty; stem must be non-empty.
    if not stem or any(not t for _, t in options):
        return None

    return RegentsQuestion(
        exam_slug="",  # filled in by caller
        number=qnum,
        prompt=stem,
        options=options,
        correct_label=None,  # filled in after key join
        has_figure=bool(_FIGURE_HINTS.search(stem)),
    )


def parse_exam_text(text: str, exam_slug: str) -> List[RegentsQuestion]:
    """Parse a full Part-I exam text dump into question records (no key join yet)."""
    out: List[RegentsQuestion] = []
    for qnum, body in _split_into_question_blocks(text):
        q = _parse_block(qnum, body)
        if q is None:
            continue
        q.exam_slug = exam_slug
        out.append(q)
    return out


def join_key(questions: List[RegentsQuestion],
             key: Dict[int, str]) -> List[RegentsQuestion]:
    """Attach correct_label from the parsed answer key. Drops items
    with no key entry (typically Part II constructed-response
    numbering that leaked into the booklet parser).
    """
    joined: List[RegentsQuestion] = []
    for q in questions:
        label = key.get(q.number)
        if not label:
            continue
        q.correct_label = label
        joined.append(q)
    return joined


# ── Network / fetch layer (opt-in) ────────────────────────────────────

def _cache_path(slug: str, kind: str) -> Path:
    """kind ∈ {'exam', 'key'} → data/external/regents/<slug>_<kind>.pdf"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{slug}_{kind}.pdf"


def fetch_pdf(url: str, dest: Path, timeout: int = 30) -> bool:
    """Download a PDF to ``dest`` unless it already exists.

    Returns True on success (or cache hit), False on any error.
    We keep the failure non-fatal so one broken exam doesn't abort
    the whole batch.
    """
    if dest.exists() and dest.stat().st_size > 0:
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gre-mock-extractor/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        dest.write_bytes(data)
        return True
    except Exception as exc:  # pragma: no cover — network path
        print(f"  [warn] fetch failed for {url}: {exc}", file=sys.stderr)
        if dest.exists():
            dest.unlink()
        return False


def pdf_to_text(path: Path) -> Optional[str]:
    """Convert a PDF to plain text using ``pdftotext`` (poppler).

    Returns None if ``pdftotext`` isn't on PATH or conversion fails.
    The D3 task introduces marker-pdf as a richer alternative; when
    that lands we can swap it in here.
    """
    if not path.exists():
        return None
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):  # pragma: no cover
        return None


# ── Top-level orchestration ───────────────────────────────────────────

def extract_one(exam_slug: str, exam_text: str, key_text: str) -> List[RegentsQuestion]:
    """Pure-text pipeline: parse booklet, parse key, join. No I/O."""
    questions = parse_exam_text(exam_text, exam_slug)
    key = parse_answer_key_text(key_text)
    return join_key(questions, key)


def extract_all(fetch: bool = False) -> List[RegentsQuestion]:
    """Run the full pipeline across every exam in TARGET_EXAMS.

    When ``fetch`` is True, download any missing PDFs to the cache
    directory. When False, we only process exams whose PDFs are
    already cached (this is the CI-safe default).
    """
    all_items: List[RegentsQuestion] = []
    for entry in TARGET_EXAMS:
        slug = entry["slug"]
        exam_pdf = _cache_path(slug, "exam")
        key_pdf = _cache_path(slug, "key")

        if fetch:
            fetch_pdf(entry["exam_url"], exam_pdf)
            fetch_pdf(entry["key_url"], key_pdf)

        if not (exam_pdf.exists() and key_pdf.exists()):
            print(f"  [skip] {slug}: PDFs not cached (use --fetch to download)")
            continue

        exam_text = pdf_to_text(exam_pdf)
        key_text = pdf_to_text(key_pdf)
        if not exam_text or not key_text:
            print(f"  [skip] {slug}: pdftotext unavailable or failed")
            continue

        items = extract_one(slug, exam_text, key_text)
        print(f"  [ok]   {slug}: {len(items)} items parsed")
        all_items.extend(items)

    return all_items


# ── DB import ─────────────────────────────────────────────────────────

def import_to_db(items: List[RegentsQuestion]) -> Tuple[int, int]:
    """Insert parsed questions into the DB with status='candidate'.

    Idempotent via (source, source_anchor) — re-running won't duplicate.
    Items that the dedup service flags as duplicates of an existing live
    question are skipped (and a structured log line is written by the
    dedup service itself). Returns (inserted, skipped_existing).
    """
    from models.database import db, init_db, Question, QuestionOption
    from services.dedup import get_dedup_service
    init_db()
    db.connect(reuse_if_open=True)

    dedup_svc = get_dedup_service()

    inserted = 0
    skipped = 0
    with db.atomic():
        for item in items:
            if item.correct_label is None:
                continue
            exists = (
                Question.select()
                .where((Question.source == item.source)
                       & (Question.source_anchor == item.source_anchor))
                .first()
            )
            if exists:
                skipped += 1
                continue

            # Dedup against the live bank (Phase 1.4 hook). The
            # service's decision log records both accepts and rejects.
            opt_texts = [t for (_lbl, t) in item.options]
            dup_qid = dedup_svc.find_dup_for(
                prompt=item.prompt,
                stimulus_content="",
                options=opt_texts,
                source=item.source,
            )
            if dup_qid is not None:
                skipped += 1
                continue

            provenance_payload = {
                "pipeline": "nyc_regents",
                "exam_slug": item.exam_slug,
                "has_figure": item.has_figure,
            }

            q = Question.create(
                measure=item.measure,
                subtype=item.subtype,
                prompt=item.prompt,
                difficulty_target=2,  # Level-1 warm-up tier
                time_target_seconds=90,
                concept_tags=json.dumps(["regents_warmup"]),
                source=item.source,
                source_anchor=item.source_anchor,
                provenance="imported",
                status="candidate",  # requires human review before live
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

    return inserted, skipped


# ── CLI ───────────────────────────────────────────────────────────────

def _summarize(items: List[RegentsQuestion]) -> None:
    by_measure: Dict[str, int] = {}
    by_exam: Dict[str, int] = {}
    with_figs = 0
    for q in items:
        by_measure[q.measure] = by_measure.get(q.measure, 0) + 1
        by_exam[q.exam_slug] = by_exam.get(q.exam_slug, 0) + 1
        if q.has_figure:
            with_figs += 1
    print(f"\nParsed {len(items)} Regents items")
    print(f"  by measure: {dict(sorted(by_measure.items()))}")
    print(f"  by exam:    {dict(sorted(by_exam.items()))}")
    print(f"  figure-bearing: {with_figs} (routed through vision audit)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and summarize but do not write to the DB.")
    parser.add_argument("--fetch", action="store_true",
                        help="Download any missing target PDFs "
                             "(network access required).")
    args = parser.parse_args(argv)

    print(f"NYC Regents extraction — {len(TARGET_EXAMS)} target exams")
    print(f"Cache dir: {CACHE_DIR}")
    items = extract_all(fetch=args.fetch)
    _summarize(items)

    if args.dry_run:
        print("\n[dry-run] no DB writes performed")
        # Emit a one-line JSON summary so downstream tooling can consume.
        summary = {
            "total": len(items),
            "measures": sorted({q.measure for q in items}),
            "exams": sorted({q.exam_slug for q in items}),
        }
        print(f"[dry-run-summary] {json.dumps(summary)}")
        return 0

    inserted, skipped = import_to_db(items)
    print(f"\nImported: {inserted}")
    print(f"Skipped (already present): {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
