#!/usr/bin/env python3
"""
GRE Mock Database Audit — Comprehensive Data Corruption Analysis
==================================================================

Systematically audits the gre_mock.db for:
- Verbal question answer-key mismatches with explanations
- Quant numeric/MCQ answer validity
- Cross-question corruption indicators (swapped explanations)
- LLM generation artifacts (self-corrections)

Usage:
    python scripts/audit_data_corruption.py              # full report
    python scripts/audit_data_corruption.py --summary    # counts only
    python scripts/audit_data_corruption.py --export     # JSON export
    python scripts/audit_data_corruption.py --ids-only   # list QIDs

Exit codes:
    0 = clean
    1 = corruption found
    2 = error
"""

import sys
import os
import re
import json
import argparse
from collections import defaultdict
from html import unescape

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DATABASE_URL', 'sqlite:///data/gre_mock.db')

from models.database import db, Question, QuestionOption, NumericAnswer, init_db


def classify_verbal_answer_key(question, options):
    """
    Classify verbal question by answer-key integrity.
    
    Returns: (category, details_str)
    Categories:
    - "Answer-key likely correct"
    - "Answer-key likely WRONG"
    - "Explanation-from-other"
    - "Explanation likely valid"
    - "Explanation likely wrong"
    - "No explanation"
    """
    explanation = unescape(question.explanation or "").strip()
    
    if not explanation or len(explanation) < 10:
        return ("No explanation", "")
    
    correct_labels = {opt.option_label for opt in options if opt.is_correct}
    if not correct_labels:
        return ("No correct marked", "")
    
    option_texts = {}
    for opt in options:
        opt_text_clean = opt.option_text.lower().strip()
        option_texts[opt.option_label] = opt_text_clean
    
    # Strategy 1: Explicit "correct answer is X"
    explicit_match = re.search(
        r'correct\s+answer\s+(?:is|=|:)?\s*\(?([A-F])\)?',
        explanation, re.IGNORECASE
    )
    if explicit_match:
        stated_answer = explicit_match.group(1).upper()
        if stated_answer in correct_labels:
            return ("Answer-key likely correct", f"Explicit: {stated_answer}")
        else:
            return (
                "Answer-key likely WRONG",
                f"Says {stated_answer}, marked {','.join(correct_labels)}"
            )
    
    # Strategy 2: Quoted words not in this question's options
    quoted_words = set()
    for m in re.finditer(r'"([^"]+)"', explanation):
        word = m.group(1).lower().strip()
        quoted_words.add(word)
    
    found_in_options = set()
    for quote in quoted_words:
        for label, opt_text in option_texts.items():
            if quote in opt_text:
                found_in_options.add(quote)
                break
    
    external_quotes = quoted_words - found_in_options
    if external_quotes and len(external_quotes) >= 2:
        return ("Explanation-from-other", str(list(external_quotes)[:3]))
    
    # Strategy 3: Vocab overlap
    prompt_words = set(re.findall(r'\b\w{4,}\b', question.prompt.lower()))
    option_words = set()
    for opt_text in option_texts.values():
        option_words.update(re.findall(r'\b\w{4,}\b', opt_text))
    
    expl_words = set(re.findall(r'\b\w{4,}\b', explanation.lower()))
    overlap = len(expl_words & (prompt_words | option_words))
    total = len(expl_words) if expl_words else 1
    overlap_pct = (overlap / total * 100)
    
    if overlap_pct > 30:
        return ("Explanation likely valid", f"{overlap_pct:.0f}% overlap")
    else:
        return ("Explanation likely wrong", f"{overlap_pct:.0f}% overlap")


def audit_database(verbose=False, export_json=False, ids_only=False,
                   include_retired=False):
    """Run full database audit. Return (corruption_found, report_dict).

    By default audits only `status='live'` rows — those are the ones
    actually served to users. Pass include_retired=True for a historical
    view that also surfaces already-retired corruption."""

    init_db()
    db.connect(reuse_if_open=True)

    query = Question.select()
    if not include_retired:
        query = query.where(Question.status == "live")
    all_questions = list(query)
    verbal_qs = [q for q in all_questions if q.measure == "verbal"]
    quant_qs = [q for q in all_questions if q.measure == "quant"]
    
    report = {
        "timestamp": str(__import__('datetime').datetime.now()),
        "total_questions": len(all_questions),
        "verbal_count": len(verbal_qs),
        "quant_count": len(quant_qs),
        "verbal_classifications": {},
        "quant_issues": {},
        "worst_questions": [],
        "llm_artifacts": [],
    }
    
    # ──── VERBAL AUDIT ────
    verbal_categories = defaultdict(list)
    option_leak_re = re.compile(
        r"Question[s]?\s*\d+(?:\s*[-–]\s*\d+)?\s*refer[s]?\s*to",
        re.I,
    )
    option_leak_count = 0
    for q in verbal_qs:
        options = list(QuestionOption.select().where(QuestionOption.question == q))
        cat, details = classify_verbal_answer_key(q, options)
        verbal_categories[cat].append({
            'id': q.id,
            'subtype': q.subtype,
            'details': details,
        })
        # Structural check: the "Questions N-M refer to the following
        # passage" marker should never appear in option text — when it
        # does, an option ran past its boundary and absorbed the next
        # set's passage marker.
        for opt in options:
            if option_leak_re.search(opt.option_text or ""):
                option_leak_count += 1
                verbal_categories["Option-text leakage"].append({
                    'id': q.id,
                    'subtype': q.subtype,
                    'details': f"opt {opt.option_label} contains 'Questions N refer to'",
                })
                break

    report["verbal_classifications"] = {
        k: len(v) for k, v in verbal_categories.items()
    }
    
    # ──── QUANT AUDIT ────
    quant_issues = defaultdict(int)
    for q in quant_qs:
        options = list(QuestionOption.select().where(QuestionOption.question == q))
        na = NumericAnswer.get_or_none(NumericAnswer.question == q)

        if q.subtype == "numeric_entry":
            if na and isinstance(na.exact_value, (int, float)):
                pass  # valid
            else:
                quant_issues["numeric_broken"] += 1
        elif q.subtype in ("mcq_single", "qc"):
            correct_count = sum(1 for opt in options if opt.is_correct)
            if correct_count != 1:
                quant_issues["mcq_key_broken"] += 1

        # Structural check: QC must declare both Quantity A: and Quantity B:
        # in the prompt — otherwise the user can't see the comparison.
        if q.subtype == "qc":
            p = q.prompt or ""
            has_a = re.search(r"Quantity\s*A\s*:", p, re.I) is not None
            has_b = re.search(r"Quantity\s*B\s*:", p, re.I) is not None
            if not (has_a and has_b):
                quant_issues["qc_missing_quantity_labels"] += 1

        # Chart/graph reference without a stimulus = unanswerable DI question
        chart_re = re.compile(
            r"\b(the chart|the table|shown above|preceding chart|preceding graph"
            r"|donations from Company)\b",
            re.I,
        )
        if chart_re.search(q.prompt or "") and q.stimulus_id is None:
            quant_issues["chart_referenced_no_stimulus"] += 1

    report["quant_issues"] = dict(quant_issues)
    
    # ──── WORST QUESTIONS ────
    worst = (
        verbal_categories.get("Answer-key likely WRONG", []) +
        verbal_categories.get("Explanation-from-other", []) +
        verbal_categories.get("Option-text leakage", [])
    )
    
    for q_info in worst[:5]:
        q = Question.get_by_id(q_info['id'])
        options = list(QuestionOption.select().where(QuestionOption.question == q))
        correct_labels = {opt.option_label for opt in options if opt.is_correct}
        
        report["worst_questions"].append({
            'id': q.id,
            'subtype': q.subtype,
            'marked_correct': list(correct_labels),
            'prompt_preview': q.prompt[:120],
            'explanation_preview': q.explanation[:150] if q.explanation else "",
            'issue': q_info['details'],
        })
    
    # ──── LLM ARTIFACTS ────
    artifact_query = Question.select().where(
        (Question.explanation.contains("Wait—let me reconsider")) |
        (Question.explanation.contains("Let me reconsider"))
    )
    if not include_retired:
        artifact_query = artifact_query.where(Question.status == "live")
    llm_artifacts = list(artifact_query)
    report["llm_artifacts"] = [
        {'id': q.id, 'subtype': q.subtype}
        for q in llm_artifacts[:10]
    ]
    
    corruption_found = len(worst) > 0 or len(quant_issues) > 0

    # Don't unconditionally close — when called from a long-running
    # process (main_frame at launch), the caller wants the connection
    # to stay open. Close only if we opened it ourselves; the
    # peewee.SqliteDatabase tolerates `reuse_if_open` being a no-op.
    return corruption_found, report


def print_report(report):
    """Pretty-print audit report."""
    print("=" * 80)
    print("GRE DATABASE AUDIT REPORT")
    print("=" * 80)
    print(f"\nTimestamp: {report['timestamp']}")
    print(f"Total questions: {report['total_questions']}")
    print(f"  Verbal: {report['verbal_count']}")
    print(f"  Quant: {report['quant_count']}")
    
    print("\n" + "=" * 80)
    print("VERBAL QUESTION CLASSIFICATIONS")
    print("=" * 80)
    
    for cat in sorted(report['verbal_classifications'].keys()):
        count = report['verbal_classifications'][cat]
        pct = (count / report['verbal_count'] * 100) if report['verbal_count'] else 0
        print(f"  {cat}: {count} ({pct:.1f}%)")
    
    print("\n" + "=" * 80)
    print("QUANT ISSUES")
    print("=" * 80)
    
    if report['quant_issues']:
        for issue, count in sorted(report['quant_issues'].items()):
            print(f"  {issue}: {count}")
    else:
        print("  (none)")
    
    print("\n" + "=" * 80)
    print("TOP 5 WORST QUESTIONS")
    print("=" * 80)
    
    for i, q in enumerate(report['worst_questions'], 1):
        print(f"\n[{i}] QID {q['id']} ({q['subtype']})")
        print(f"  Marked correct: {q['marked_correct']}")
        print(f"  Prompt: {q['prompt_preview']}...")
        print(f"  Explanation: {q['explanation_preview']}...")
        print(f"  Issue: {q['issue']}")
    
    if report['llm_artifacts']:
        print("\n" + "=" * 80)
        print("LLM SELF-CORRECTION ARTIFACTS")
        print("=" * 80)
        print(f"Found {len(report['llm_artifacts'])} questions with 'let me reconsider':")
        for q in report['llm_artifacts'][:5]:
            print(f"  QID {q['id']} ({q['subtype']})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--summary', action='store_true',
        help='Print only summary counts'
    )
    parser.add_argument(
        '--export', action='store_true',
        help='Export full report as JSON'
    )
    parser.add_argument(
        '--ids-only', action='store_true',
        help='List only QIDs of corrupted questions'
    )
    parser.add_argument(
        '--include-retired', action='store_true',
        help='Include status=retired rows (default: live only)'
    )

    args = parser.parse_args()

    corruption_found, report = audit_database(
        include_retired=args.include_retired,
    )
    
    if args.export:
        print(json.dumps(report, indent=2))
    elif args.ids_only:
        corrupted_ids = [q['id'] for q in report['worst_questions']]
        print(" ".join(str(qid) for qid in corrupted_ids))
    elif args.summary:
        for cat, count in sorted(report['verbal_classifications'].items()):
            pct = (count / report['verbal_count'] * 100)
            print(f"{cat}: {count} ({pct:.1f}%)")
    else:
        print_report(report)

    db.close()
    sys.exit(1 if corruption_found else 0)


if __name__ == "__main__":
    main()
