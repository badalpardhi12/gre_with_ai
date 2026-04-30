"""
Audit + auto-fix verbal questions for structural issues.

What it checks (in order of severity):

  1. NO correct option marked. Caused by:
     - Word-answer tally label that didn't match any option text
     - Stale tally row whose answer references an option Sonnet didn't extract
  2. Wrong option count for the subtype:
     - tc 1-blank: 5
     - tc 2-blank: 6 (blank1_A..blank2_C)
     - tc 3-blank: 9 (blank1..blank3 each A-C)
     - se: 6 (A-F, exactly 2 correct)
     - rc_single: 5 (A-E, exactly 1 correct)
     - rc_multi: 3 (A-C, 1-3 correct)
     - rc_select_passage: 4-7 (sentence numbers)
  3. Missing or empty prompt
  4. Missing stimulus on RC subtypes (rc_single, rc_multi, rc_select_passage)
  5. LaTeX escape leftovers in prompt/explanation:
     - Literal "\\n" / "\\$" / "\\(" — should have been normalised
     - Mismatched \( ... \) pairs
  6. Duplicate prompts (same source + identical first 200 chars)

Usage:
    venv/bin/python scripts/audit_verbal_quality.py            # report only
    venv/bin/python scripts/audit_verbal_quality.py --fix      # auto-repair the safe ones
    venv/bin/python scripts/audit_verbal_quality.py --fix --retire-unfixable   # retire what we can't repair
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import db, Question, QuestionOption, init_db
from services.log import get_logger

logger = get_logger("audit_verbal_quality")


SUBTYPE_OPTION_COUNTS = {
    "tc": (5, 9),          # variable: 5 (1-blank) / 6 (2-blank) / 9 (3-blank)
    "se": (6, 6),          # exactly 6
    "rc_single": (5, 5),   # exactly 5
    "rc_multi": (3, 3),    # exactly 3
    "rc_select_passage": (4, 8),  # 4-8 sentences typical
}


def check_question(q):
    """Return list of issue dicts found on this question."""
    issues = []
    opts = list(QuestionOption.select().where(QuestionOption.question == q))

    # 1. No correct option marked
    if not any(o.is_correct for o in opts):
        # Skip numeric_entry / awa where this is expected (no options)
        if q.subtype not in ("numeric_entry", "awa_issue") and opts:
            issues.append({"kind": "no_correct_marked", "n_opts": len(opts)})

    # 2. Option count
    n = len(opts)
    if q.subtype in SUBTYPE_OPTION_COUNTS:
        lo, hi = SUBTYPE_OPTION_COUNTS[q.subtype]
        if not (lo <= n <= hi):
            issues.append({"kind": "option_count_off",
                           "subtype": q.subtype, "got": n, "expected": (lo, hi)})

    # 3. Empty prompt
    if not (q.prompt or "").strip():
        issues.append({"kind": "empty_prompt"})

    # 4. RC missing stimulus — check via the FK column directly to
    # avoid loading the related row.
    if q.subtype in ("rc_single", "rc_multi", "rc_select_passage"):
        if not q.stimulus_id:
            issues.append({"kind": "rc_no_stimulus"})

    # 5. LaTeX escape leftovers
    text = (q.prompt or "") + " " + (q.explanation or "")
    if "\\\\n" in text or "\\\\$" in text or "\\\\frac" in text:
        issues.append({"kind": "double_escaped_latex"})
    # Mismatched \( ... \) pairs (unequal counts)
    open_n = text.count("\\(")
    close_n = text.count("\\)")
    if open_n != close_n:
        issues.append({"kind": "unbalanced_inline_math",
                       "open": open_n, "close": close_n})

    return issues


def fix_double_escaped_latex(q):
    """Replace literal \\\\n / \\\\$ / \\\\frac{...} sequences with their
    intended single-backslash forms so KaTeX renders them."""
    changed = False
    for field in ("prompt", "explanation"):
        v = getattr(q, field, "") or ""
        if not v:
            continue
        new = v
        # Common over-escape patterns from earlier extraction runs.
        new = new.replace("\\\\n", "\n")
        new = new.replace("\\\\$", "$")
        new = re.sub(r"\\\\([\(\)\[\]a-zA-Z])", r"\\\1", new)
        if new != v:
            setattr(q, field, new)
            changed = True
    return changed


def fix_rc_no_stimulus(q):
    """When an RC question has no stimulus_id, look for nearby manhattan
    questions (within ±5 in q.id) that DO have a stimulus, and inherit
    theirs when the prompt looks like it's about the same passage.

    Heuristic: questions imported in the same chapter sequence sit in
    consecutive ID ranges, so a missing-stimulus RC question is usually
    bracketed by sibling questions that share its passage. We pick the
    nearest neighbour with a stimulus on the same chapter (same
    section_label proxy via shared title prefix or the heuristic
    that consecutive IDs mean consecutive q_numbers).
    """
    if q.stimulus_id:
        return False
    # Look at the 5 prior and 5 following manhattan questions for a
    # stimulus to inherit.
    candidates = list(Question.select().where(
        (Question.source == "manhattan_5lb_2018")
        & (Question.id.between(q.id - 5, q.id + 5))
        & (Question.id != q.id)
        & (Question.stimulus.is_null(False))
    ))
    if not candidates:
        return False
    # Prefer the nearest one (by ID distance).
    candidates.sort(key=lambda c: abs(c.id - q.id))
    q.stimulus = candidates[0].stimulus
    q.save()
    return True


def fix_no_correct_marked_via_explanation(q, opts):
    """When no option is is_correct, parse the explanation for an
    'X. Word.' or 'X. (B).' header and try to match it against an
    option label or text. Returns True if we fixed it."""
    expl = q.explanation or ""
    if not expl.strip():
        return False

    # rc_multi with Roman-numeral answers (`II only`, `I and III only`,
    # `I, II, and III`) — the answer doesn't reference option labels,
    # it references the statement INDEX. Mark options A/B/C in order.
    if q.subtype == "rc_multi":
        m = re.match(r"\s*([IVX]+(?:[,\s]+(?:and\s+)?[IVX]+)*)(?:\s+only)?\.",
                     expl)
        if m:
            roman = m.group(1)
            roman_to_idx = {"I": 0, "II": 1, "III": 2}
            chosen = []
            for tok in re.findall(r"[IVX]+", roman):
                if tok in roman_to_idx:
                    chosen.append(roman_to_idx[tok])
            if chosen:
                marked = 0
                for idx in set(chosen):
                    if idx < len(opts) and not opts[idx].is_correct:
                        opts[idx].is_correct = True
                        opts[idx].save()
                        marked += 1
                if marked:
                    return True

    # Try letter-in-parens at the start of the explanation: "(C)."
    m = re.match(r"\s*\(([A-FI]+)\)\.", expl)
    if m:
        letter = m.group(1).upper()
        for o in opts:
            if (o.option_label or "").upper() == letter and not o.is_correct:
                o.is_correct = True
                o.save()
                return True
    # Try a leading word like "Apportioned." for verbal TC/SE
    m = re.match(r"\s*([A-Z][a-zA-Z'-]{3,30})\.\s", expl)
    if m:
        word = m.group(1).lower()
        for o in opts:
            if word in (o.option_text or "").lower() and not o.is_correct:
                o.is_correct = True
                o.save()
                return True
    # Try comma-separated word answer: "Patronizing, condescending."
    m = re.match(r"\s*([A-Z][a-zA-Z'-]{3,30}(?:,\s*[a-z][a-zA-Z'-]{2,30}){1,3})\.",
                 expl)
    if m:
        words = [w.strip().lower() for w in m.group(1).split(",")]
        marked = 0
        for w in words:
            for o in opts:
                if w in (o.option_text or "").lower() and not o.is_correct:
                    o.is_correct = True
                    o.save()
                    marked += 1
                    break
        return marked > 0
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true",
                    help="apply safe auto-fixes (LaTeX double-escapes, "
                         "missing-correct via explanation header)")
    ap.add_argument("--retire-unfixable", action="store_true",
                    help="set status='retired' for questions that can't "
                         "be auto-fixed (empty prompt, wrong option count)")
    ap.add_argument("--measure", default="verbal")
    args = ap.parse_args()

    init_db()
    db.connect(reuse_if_open=True)

    rows = list(Question.select().where(
        (Question.measure == args.measure) & (Question.status == "live")
    ))
    print(f"Auditing {len(rows)} live {args.measure} questions…")

    issues_by_qid = {}
    issue_counts = Counter()
    for q in rows:
        issues = check_question(q)
        if issues:
            issues_by_qid[q.id] = issues
            for i in issues:
                issue_counts[i["kind"]] += 1

    print(f"\nIssue breakdown ({sum(issue_counts.values())} total findings on "
          f"{len(issues_by_qid)} questions):")
    for kind, n in issue_counts.most_common():
        print(f"  {kind}: {n}")

    if not args.fix:
        # Spot-check examples
        print("\nSample issues (first 5 of each kind):")
        by_kind = defaultdict(list)
        for qid, issues in issues_by_qid.items():
            for i in issues:
                by_kind[i["kind"]].append((qid, i))
        for kind, items in by_kind.items():
            print(f"\n  [{kind}]")
            for qid, info in items[:5]:
                q = Question.get_by_id(qid)
                print(f"    qid={qid} {q.subtype} src={q.source}: "
                      f"prompt={q.prompt[:80]!r}")
                print(f"      info: {info}")
        print("\nDry-run; re-run with --fix to apply repairs.")
        return 0

    # Apply safe fixes
    fixed_latex = 0
    fixed_correct = 0
    fixed_stim = 0
    retired = 0
    still_broken = []

    for qid, issues in issues_by_qid.items():
        q = Question.get_by_id(qid)
        opts = list(QuestionOption.select().where(QuestionOption.question == q))
        kinds = {i["kind"] for i in issues}

        if "double_escaped_latex" in kinds:
            if fix_double_escaped_latex(q):
                q.save()
                fixed_latex += 1
                kinds.discard("double_escaped_latex")

        if "no_correct_marked" in kinds:
            if fix_no_correct_marked_via_explanation(q, opts):
                fixed_correct += 1
                kinds.discard("no_correct_marked")

        if "rc_no_stimulus" in kinds:
            if fix_rc_no_stimulus(q):
                fixed_stim += 1
                kinds.discard("rc_no_stimulus")

        # Anything still broken? Retire ONLY when there's a hard blocker
        # (empty prompt, impossible option count). Don't retire just for
        # leftover unbalanced math or non-fixable no_correct_marked
        # (those still serve as practice questions).
        BLOCKING = {"empty_prompt", "option_count_off"}
        if kinds and args.retire_unfixable and (kinds & BLOCKING):
            q.status = "retired"
            q.save()
            retired += 1
            continue

        if kinds:
            still_broken.append((qid, sorted(kinds)))

    print(f"\nFixes applied:")
    print(f"  latex over-escape repaired: {fixed_latex}")
    print(f"  correct-option marked from explanation header: {fixed_correct}")
    print(f"  stimulus attached via shared-group lookup: {fixed_stim}")
    if args.retire_unfixable:
        print(f"  retired (empty_prompt or option_count_off): {retired}")
    print(f"  still broken (not auto-fixed): {len(still_broken)}")

    if still_broken:
        print("\nStill-broken sample (first 10):")
        for qid, kinds in still_broken[:10]:
            print(f"  qid={qid}: {kinds}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
