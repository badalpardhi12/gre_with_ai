"""
Delete broken questions from the database:
1. Questions with any empty option text (unanswerable)
2. Questions referencing figures we don't have stored

Usage:
    python scripts/cleanup_broken_questions.py --dry-run    # preview
    python scripts/cleanup_broken_questions.py              # delete
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import db, init_db, Question, QuestionOption, NumericAnswer

# Patterns that indicate the question references a figure/image
FIGURE_PATTERNS = [
    r"figure\s+above", r"figure\s+below", r"figure\s+shown",
    r"in\s+the\s+figure", r"in\s+the\s+diagram",
    r"graph\s+above", r"graph\s+below", r"graph\s+shown",
    r"chart\s+above", r"chart\s+below", r"chart\s+shown",
    r"shown\s+above", r"shown\s+below",
    r"pie\s+chart", r"bar\s+graph", r"line\s+graph",
    r"the\s+chart", r"the\s+graph", r"the\s+diagram",
    r"refers?\s+to\s+the\s+(?:following\s+)?graphs?",
    r"refers?\s+to\s+the\s+(?:following\s+)?charts?",
    r"based\s+on\s+the\s+(?:following\s+)?graphs?",
    r"based\s+on\s+the\s+(?:following\s+)?charts?",
]
FIGURE_RE = re.compile("|".join(FIGURE_PATTERNS), re.IGNORECASE)

# Patterns indicating broken math notation (lost exponents/subscripts from inline images)
# e.g., "x 2 = y 2 + 1" should be "x² = y² + 1", "f ( x ) = 3 x 2" should be "f(x) = 3x²"
MATH_BROKEN_PATTERNS = [
    r"\b[a-z]\s+\d+\s+[,.=]",      # 'x 2 ,' or 'x 2 .' or 'x 2 ='
    r"\b[a-z]\s+\d+\s+[a-z]\b",    # 'x 2 y'
]
MATH_BROKEN_RE = re.compile("|".join(MATH_BROKEN_PATTERNS))


def find_broken_questions():
    """Return (empty_options_qs, figure_no_stim_qs, broken_math_qs)."""
    empty_opt_qs = []
    figure_no_stim_qs = []
    broken_math_qs = []

    # Find empty options
    questions_with_empty = set()
    for opt in QuestionOption.select():
        if not opt.option_text or not opt.option_text.strip():
            questions_with_empty.add(opt.question_id)

    for qid in questions_with_empty:
        try:
            q = Question.get_by_id(qid)
            empty_opt_qs.append(q)
        except Question.DoesNotExist:
            continue

    # Find figure-references without stimulus
    for q in Question.select():
        if q.id in questions_with_empty:
            continue
        if FIGURE_RE.search(q.prompt) and q.stimulus_id is None:
            figure_no_stim_qs.append(q)

    # Find quant questions with broken math notation (lost exponents)
    seen_ids = questions_with_empty | {q.id for q in figure_no_stim_qs}
    for q in Question.select().where(Question.measure == "quant"):
        if q.id in seen_ids:
            continue
        if MATH_BROKEN_RE.search(q.prompt):
            broken_math_qs.append(q)

    return empty_opt_qs, figure_no_stim_qs, broken_math_qs


def delete_questions(questions):
    """Delete questions and their related rows (Response, ItemStats, etc.)."""
    qids = [q.id for q in questions]
    if not qids:
        return 0

    # Import models lazily to handle missing optional models gracefully
    from models.database import Response

    try:
        from models.database import ItemStats
        has_item_stats = True
    except ImportError:
        has_item_stats = False

    with db.atomic():
        # Delete cascading children first
        Response.delete().where(Response.question_id.in_(qids)).execute()
        if has_item_stats:
            ItemStats.delete().where(ItemStats.question_id.in_(qids)).execute()
        QuestionOption.delete().where(QuestionOption.question_id.in_(qids)).execute()
        NumericAnswer.delete().where(NumericAnswer.question_id.in_(qids)).execute()
        deleted = Question.delete().where(Question.id.in_(qids)).execute()

    return deleted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview without deleting")
    args = parser.parse_args()

    init_db()
    db.connect(reuse_if_open=True)

    total_before = Question.select().count()
    print(f"Total questions before cleanup: {total_before}")

    empty_opt_qs, figure_no_stim_qs, broken_math_qs = find_broken_questions()
    print(f"\nFound {len(empty_opt_qs)} questions with empty options")
    print(f"Found {len(figure_no_stim_qs)} questions referencing figures without stimulus")
    print(f"Found {len(broken_math_qs)} questions with broken math notation")

    # Show samples
    print("\nSample empty-option questions:")
    for q in empty_opt_qs[:3]:
        print(f"  Q{q.id} ({q.subtype}): {q.prompt[:80]}")

    print("\nSample figure-reference questions:")
    for q in figure_no_stim_qs[:3]:
        print(f"  Q{q.id} ({q.subtype}): {q.prompt[:80]}")

    print("\nSample broken-math questions:")
    for q in broken_math_qs[:5]:
        print(f"  Q{q.id} ({q.subtype}): {q.prompt[:120]}")

    if args.dry_run:
        total = len(empty_opt_qs) + len(figure_no_stim_qs) + len(broken_math_qs)
        print(f"\n[DRY RUN] Would delete {total} questions")
        return

    n1 = delete_questions(empty_opt_qs)
    n2 = delete_questions(figure_no_stim_qs)
    n3 = delete_questions(broken_math_qs)

    total_after = Question.select().count()
    print(f"\nDeleted {n1} empty-option questions")
    print(f"Deleted {n2} figure-reference questions")
    print(f"Deleted {n3} broken-math questions")
    print(f"Total questions after cleanup: {total_after}")
    print(f"Removed: {total_before - total_after} ({100 * (total_before - total_after) // total_before}%)")

    # Final breakdown
    from collections import Counter
    counts = Counter()
    for q in Question.select():
        counts[q.subtype] += 1
    print("\nFinal subtype distribution:")
    for st, c in counts.most_common():
        print(f"  {st}: {c}")


if __name__ == "__main__":
    sys.exit(main() or 0)
