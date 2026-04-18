"""
One-shot data cleanup for three audit-found content bugs:

1. RC questions with no Stimulus AND a short prompt → truly broken; passage
   was lost during extraction. We mark these `status='retired'` so the smart
   selector skips them (the selector only picks `status='live'` questions).

2. RC questions with no Stimulus but a LONG prompt → "Critical Reasoning"
   style where the passage is inlined into the prompt. We split on the first
   "Which of the following…" / "The argument above…" / etc. signal, move
   the prefix into a new Stimulus row, and shorten the prompt to just the
   question stem. Functional fix (passage now renders in the dedicated
   passage panel).

3. Verbal questions where the explanation literally states "correct answer
   is X" but the marked is_correct option is Y → clear the explanation field.
   The on-demand LLM regenerator (services.explanation.get_explanation_async,
   wired in PR 6) will fetch + cache a fresh explanation the next time the
   user opens Show Answer for that question.

Idempotent — re-running it after cleanup is a no-op (the conditions no
longer match the rows that were already fixed).

Usage:
    venv/bin/python scripts/cleanup_qa_data.py            # dry-run report
    venv/bin/python scripts/cleanup_qa_data.py --apply    # actually write
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import db, Question, QuestionOption, Stimulus


# Verbal explanation patterns that explicitly claim a correct option label.
ANSWER_CLAIM_PATTERNS = [
    re.compile(r"correct answer is\s+\(?([A-Z])\)?", re.I),
    re.compile(r"answer:?\s*\(?([A-Z])\)?\b", re.I),
    re.compile(r"option\s+([A-Z])\s+is\s+correct", re.I),
]

# Match a question stem at the start of the prompt: any text up to the
# first ? (ASCII or full-width) that is at most ~250 chars long. Generic
# enough to catch "Which of the following…?", "Roger: …?",
# "The argument above is most weakened by which of the following?", etc.
# Full-width `？` (U+FF1F) is normalised to `?` before matching so the
# downstream regex stays simple.
_LEAD_IN_RE = re.compile(r"^(.{15,250}?\?)\s+", re.S)


def find_inlined_split(prompt: str):
    """If `prompt` is `<question stem>?\\n<argument paragraph>`, return
    (passage, stem). Otherwise None.

    Requires:
      - a `?` (or full-width `？`) within the first ~250 chars
      - >= 120 chars of substantive content after the question mark

    Strict enough to avoid false positives on truly-broken short prompts
    (no `?` at all) or mid-sentence truncations (no content after).
    """
    if not prompt:
        return None
    normalised = prompt.replace("？", "?")
    if len(normalised) < 200:
        return None
    m = _LEAD_IN_RE.match(normalised)
    if not m:
        return None
    stem = m.group(1).strip()
    passage = normalised[m.end():].strip()
    if len(passage) < 120:
        return None
    return passage, stem


def explanation_claims_wrong_letter(question) -> bool:
    """True iff the explanation says 'correct answer is X' and X disagrees
    with the actual marked is_correct option labels."""
    expl = question.explanation or ""
    claim = None
    for pat in ANSWER_CLAIM_PATTERNS:
        m = pat.search(expl)
        if m:
            claim = m.group(1).upper()
            break
    if not claim:
        return False
    actual = [o.option_label.upper()
              for o in QuestionOption.select().where(
                  QuestionOption.question == question,
                  QuestionOption.is_correct == True,
              )]
    return bool(actual) and claim not in actual


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write changes (default: dry-run)")
    args = ap.parse_args()

    db.connect(reuse_if_open=True)

    rc_subtypes = ("rc_single", "rc_multi", "rc_select_passage")
    no_stim_rc = list(Question.select().where(
        Question.subtype.in_(rc_subtypes),
        Question.status == "live",
        Question.stimulus.is_null(),
    ))

    truly_broken = []
    inlined = []
    for q in no_stim_rc:
        split = find_inlined_split(q.prompt or "")
        if split is None:
            truly_broken.append(q)
        else:
            inlined.append((q, split))

    mismatched_expl = []
    for q in Question.select().where(
        Question.measure == "verbal",
        Question.status == "live",
        Question.explanation != "",
    ):
        if q.subtype not in ("rc_single", "mcq_single"):
            continue
        if explanation_claims_wrong_letter(q):
            mismatched_expl.append(q)

    print("─" * 64)
    print(f"DRY-RUN" if not args.apply else "APPLYING")
    print("─" * 64)
    print(f"RC questions to retire (no passage + short prompt):  {len(truly_broken)}")
    for q in truly_broken[:5]:
        print(f"  qid={q.id}  prompt: {(q.prompt or '')[:70]!r}")
    if len(truly_broken) > 5:
        print(f"  …and {len(truly_broken) - 5} more")

    print()
    print(f"RC questions to split (inlined passage):             {len(inlined)}")
    for q, (passage, stem) in inlined[:3]:
        print(f"  qid={q.id}  passage_len={len(passage)}  stem={stem[:60]!r}")
    if len(inlined) > 3:
        print(f"  …and {len(inlined) - 3} more")

    print()
    print(f"Verbal explanations to clear (mismatched letter):    {len(mismatched_expl)}")
    for q in mismatched_expl[:6]:
        print(f"  qid={q.id}  prompt: {(q.prompt or '')[:60]!r}")

    if not args.apply:
        print()
        print("Re-run with --apply to write changes.")
        return

    print()
    print("Writing changes…")
    with db.atomic():
        for q in truly_broken:
            q.status = "retired"
            q.save()
        for q, (passage, stem) in inlined:
            stim = Stimulus.create(
                stimulus_type="passage",
                title="",
                content=passage,
            )
            q.stimulus_id = stim.id
            q.prompt = stem
            q.save()
        for q in mismatched_expl:
            q.explanation = ""
            q.save()
    print(f"Done. retired={len(truly_broken)}  split={len(inlined)}  "
          f"cleared_expl={len(mismatched_expl)}")
    db.close()


if __name__ == "__main__":
    main()
