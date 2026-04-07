#!/usr/bin/env python3
"""
Import GRE verbal critical reasoning questions from external datasets.

Sources:
  - data/external/gre_questions.csv  (plain CSV, 132 questions)
  - data/external/gre_questions1.csv (xlsx, 100 questions)
  - data/external/gre_questions2.csv (xlsx, 100 questions)

Each question is a passage + question stem + 5 options (A-E) with a correct answer.
These are imported as verbal/rc_single questions with a Stimulus passage.

Usage:
    python scripts/import_cr_questions.py
"""

import re
import sys
import os
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import db, init_db, Stimulus, Question, QuestionOption


# ── Parsing helpers ────────────────────────────────────────────────


def parse_csv_file(path):
    """Parse gre_questions.csv: each question is "...text...",ANSWER_LETTER."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    raw_questions = re.findall(r'"(.+?)",([A-E])', content, re.DOTALL)
    return [(text.strip(), answer.strip()) for text, answer in raw_questions]


def parse_xlsx_file(path):
    """Parse xlsx files (questions1/questions2), return list of (text, answer)."""
    with zipfile.ZipFile(path, "r") as zf:
        # Shared strings
        shared = []
        with zf.open("xl/sharedStrings.xml") as f:
            tree = ET.parse(f)
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for si in tree.findall(".//s:si", ns):
                texts = si.findall(".//s:t", ns)
                shared.append("".join(t.text or "" for t in texts))

        # Sheet data
        with zf.open("xl/worksheets/sheet1.xml") as f:
            tree = ET.parse(f)
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            results = []
            for row in tree.findall(".//s:row", ns):
                cells = []
                for cell in row.findall("s:c", ns):
                    t = cell.get("t", "")
                    val_el = cell.find("s:v", ns)
                    if val_el is not None and val_el.text:
                        if t == "s":
                            cells.append(shared[int(val_el.text)])
                        else:
                            cells.append(val_el.text)
                    else:
                        cells.append("")
                if len(cells) >= 2 and cells[1].strip() in "ABCDE":
                    results.append((cells[0].strip(), cells[1].strip()))
            return results


def split_question_text(full_text):
    """Split combined text into passage, question stem, and options.

    Returns (passage, stem, options_list) where options_list is
    [(label, text), ...].  Returns None if parsing fails.
    """
    # Normalize whitespace runs but preserve structure
    text = re.sub(r"[ \t]+", " ", full_text)

    # Try multiple option formats:
    # Format 1: (A) ... (B) ... (C) ... (D) ... (E) ...
    # Format 2: A. ... B. ... C. ... D. ... E. ... (with or without space after dot)
    # Format 3: full-width unicode parentheses

    option_match = None
    opt_format = None

    # Try "(A)" format first
    m = re.search(r"[\s\n]\(A\)\s*", text)
    if m:
        option_match = m
        opt_format = "paren"
    else:
        # Try unicode parentheses
        m = re.search(r"[\s\n]\uff08A\uff09\s*", text)
        if m:
            option_match = m
            opt_format = "paren_unicode"
        else:
            # Try "A." format — match A. followed by text, with B. C. D. E. later
            m = re.search(r"[\s\n]A\.", text)
            if m:
                # Verify B. C. D. also exist after this point
                after = text[m.start():]
                if re.search(r"B\.", after) and re.search(r"C\.", after):
                    option_match = m
                    opt_format = "dot"

    if not option_match:
        return None

    pre_options = text[: option_match.start()].strip()
    options_text = text[option_match.start() :].strip()

    # Parse individual options based on format
    if opt_format == "paren":
        opt_pattern = r"[\(\uff08]([A-E])[\)\uff09]\s*(.+?)(?=[\s\n]*[\(\uff08][A-E][\)\uff09]|$)"
    elif opt_format == "paren_unicode":
        opt_pattern = r"\uff08([A-E])\uff09\s*(.+?)(?=[\s\n]*\uff08[A-E]\uff09|$)"
    else:  # dot format: A.text B.text (may or may not have spaces)
        opt_pattern = r"(?:^|\n)\s*([A-E])\.[\s]*(.+?)(?=\n\s*[A-E]\.|\Z)"

    opts = re.findall(opt_pattern, options_text, re.DOTALL)
    if len(opts) < 4:
        return None

    # Deduplicate by label, keeping first occurrence
    seen_labels = set()
    options = []
    for label, otext in opts:
        if label not in seen_labels:
            seen_labels.add(label)
            options.append((label, otext.strip()))
    
    if len(options) < 4:
        return None

    # Split pre_options into passage and question stem
    # Common question stems start with "Which of the following", "The argument", etc.
    stem_patterns = [
        r"Which of the following",
        r"The argument (?:above |given )?(?:assumes|depends|is most|most)",
        r"The answer to which",
        r"Each of the following",
        r"If the statements? above",
        r"The statements? above",
        r"In responding to",
        r"The reasoning",
        r"Knowledge of (?:each|which)",
        r"The considerations above",
        r"If the (?:statements|information)",
        r"The (?:author's|scientist's|historian's|economist's|archaeologists'|counselor's|argument)",
    ]
    combined_pattern = "|".join(f"({p})" for p in stem_patterns)
    stem_match = re.search(combined_pattern, pre_options, re.IGNORECASE)

    if stem_match:
        passage = pre_options[: stem_match.start()].strip()
        stem = pre_options[stem_match.start() :].strip()
    else:
        # Fallback: try splitting on last sentence boundary before options
        # Look for the last question mark or common question-introducing phrase
        qmark = pre_options.rfind("?")
        if qmark > 0:
            # Walk back to find the start of the question sentence
            sentence_start = pre_options.rfind(".", 0, qmark)
            if sentence_start > 0:
                passage = pre_options[: sentence_start + 1].strip()
                stem = pre_options[sentence_start + 1 :].strip()
            else:
                passage = ""
                stem = pre_options
        else:
            # No clear split; use full text as prompt
            passage = ""
            stem = pre_options

    return passage, stem, options


# ── Import logic ───────────────────────────────────────────────────


def import_questions():
    """Import all CR questions into the database."""
    init_db()
    db.connect(reuse_if_open=True)

    files = [
        ("data/external/gre_questions.csv", "csv"),
        ("data/external/gre_questions1.csv", "xlsx"),
        ("data/external/gre_questions2.csv", "xlsx"),
    ]

    all_questions = []
    for path, fmt in files:
        if not os.path.exists(path):
            print(f"  Skipping {path}: file not found")
            continue
        if fmt == "csv":
            qs = parse_csv_file(path)
        else:
            qs = parse_xlsx_file(path)
        print(f"  {path}: {len(qs)} questions parsed")
        all_questions.extend(qs)

    print(f"\nTotal questions to import: {len(all_questions)}")

    # Deduplicate by first 80 characters of question text
    seen = set()
    unique = []
    for text, answer in all_questions:
        key = re.sub(r"\s+", " ", text[:80]).lower()
        if key not in seen:
            seen.add(key)
            unique.append((text, answer))
    print(f"After deduplication: {len(unique)}")

    imported = 0
    skipped = 0
    parse_failed = 0

    with db.atomic():
        for text, correct_answer in unique:
            parsed = split_question_text(text)
            if parsed is None:
                parse_failed += 1
                continue

            passage, stem, options = parsed

            # Check for duplicate by first option text (more unique than stem)
            first_opt_text = options[0][1][:60] if options else ""
            opt_key = re.sub(r"\s+", " ", first_opt_text).lower().strip()
            if opt_key:
                existing = (
                    QuestionOption.select()
                    .join(Question)
                    .where(Question.measure == "verbal")
                    .where(QuestionOption.option_label == "A")
                    .where(QuestionOption.option_text.contains(opt_key[:40]))
                    .first()
                )
            if existing:
                skipped += 1
                continue

            # Create stimulus if there's a passage
            stimulus = None
            if passage and len(passage) > 20:
                stimulus = Stimulus.create(
                    stimulus_type="passage",
                    title="",
                    content=passage,
                )

            # Create question
            q = Question.create(
                measure="verbal",
                subtype="rc_single",
                stimulus=stimulus,
                prompt=stem,
                difficulty_target=3,
                time_target_seconds=105,  # ~1:45 per CR question
                concept_tags='["critical_reasoning"]',
                provenance="imported",
                explanation="",
            )

            # Create options
            for label, otext in options:
                QuestionOption.create(
                    question=q,
                    option_label=label,
                    option_text=otext,
                    is_correct=(label == correct_answer),
                )

            imported += 1

    print(f"\nImported: {imported}")
    print(f"Skipped (duplicates): {skipped}")
    print(f"Parse failures: {parse_failed}")

    total_verbal = Question.select().where(Question.measure == "verbal").count()
    total_all = Question.select().count()
    print(f"\nTotal verbal questions: {total_verbal}")
    print(f"Total questions (all): {total_all}")


if __name__ == "__main__":
    import_questions()
