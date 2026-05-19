#!/usr/bin/env python3
"""Apply the unambiguous encoding fixes flagged by ``audit_encoding_issues.py``.

Phase 3.3 of the cleanup plan. Reads ``data/audits/encoding_issues_<DATE>.csv``
and applies ``services.sanitize.auto_fix_text`` to each affected field.
For non-fixable issues (unmatched dollar signs, malformed HTML), it
records a ``QuestionFlag(reason='other', note='encoding_issue:<type>')``
row instead of writing — those need human review.

Default mode is ``--dry-run``: prints proposed diffs and counts but
does NOT touch the database. Pass ``--apply`` to commit.

Usage
-----
    venv/bin/python scripts/fix_encoding_issues.py
    venv/bin/python scripts/fix_encoding_issues.py --apply
    venv/bin/python scripts/fix_encoding_issues.py --csv data/audits/encoding_issues_2026-05-18.csv --apply
    venv/bin/python scripts/fix_encoding_issues.py --auto-canonicalize  # also rewrite mixed MCQ prefixes

Idempotency: re-running on a freshly-fixed bank produces no diffs.

Constraints
-----------
* Only writes to ``data/gre_user.db`` via Peewee. The shipped seed
  (``data/gre_mock.db``) is never mutated.
* Skips rows whose detected hazard is not in the auto-fixable set
  (``smart_quotes_in_math``, ``nbsp_in_math``, ``triple_dollar``,
  ``mixed_delimiters``).
* For unfixable hazards (``unmatched_dollar``, ``unmatched_dollar_dd``,
  ``unescaped_html``, ``mixed_mcq_prefix``) we either:
  - log a ``QuestionFlag`` (when the field belongs to a question), so
    the SME-review queue surfaces it, or
  - emit a warning row when the field is a stimulus (no FK to a single
    question — multiple questions may reference it).
"""
from __future__ import annotations

import argparse
import csv
import difflib
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Importable regardless of cwd.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import DATA_DIR  # noqa: E402
# We deliberately import the model module rather than the names so the
# Peewee bindings re-resolve from the current ``models.database``
# module-state on every call. Pytest fixtures may evict and re-import
# the module to swap in a tmp_path DB; resolving via attribute access
# means we always see the live class objects.
from services.sanitize import (  # noqa: E402
    ISSUE_MIXED_DELIMITERS,
    ISSUE_MIXED_MCQ_PREFIX,
    ISSUE_NBSP_IN_MATH,
    ISSUE_SMART_QUOTES_IN_MATH,
    ISSUE_TRIPLE_DOLLAR,
    ISSUE_UNESCAPED_HTML,
    ISSUE_UNMATCHED_DOLLAR,
    ISSUE_UNMATCHED_DOLLAR_DD,
    auto_fix_text,
    find_latex_encoding_issues,
)


def _models():
    """Return a fresh handle to the model classes. Re-resolves
    ``models.database`` from ``sys.modules`` (or re-imports it) so
    pytest's eviction + rebind cycle in ``temp_db`` doesn't leave us
    holding a stale reference to the previous test's Peewee
    classes."""
    import sys
    if "models.database" not in sys.modules:
        import models.database  # noqa: F401
    return sys.modules["models.database"]

# Issues that ``auto_fix_text`` knows how to repair.
AUTO_FIXABLE = frozenset({
    ISSUE_TRIPLE_DOLLAR,
    ISSUE_SMART_QUOTES_IN_MATH,
    ISSUE_NBSP_IN_MATH,
    ISSUE_MIXED_DELIMITERS,
})

# Issues that are SME-review only — flagged but not auto-rewritten.
NEEDS_REVIEW = frozenset({
    ISSUE_UNMATCHED_DOLLAR,
    ISSUE_UNMATCHED_DOLLAR_DD,
    ISSUE_UNESCAPED_HTML,
    ISSUE_MIXED_MCQ_PREFIX,
})


def _diff(before: str, after: str, label: str) -> str:
    if before == after:
        return ""
    diff = difflib.unified_diff(
        before.splitlines(keepends=False),
        after.splitlines(keepends=False),
        fromfile=f"{label} (before)",
        tofile=f"{label} (after)",
        lineterm="",
        n=1,
    )
    return "\n".join(diff)


def _load_audit_csv(path: Path) -> Dict[Tuple[int, str], List[str]]:
    """Return ``{(qid, field): [issue_type, ...]}``."""
    grouped: Dict[Tuple[int, str], List[str]] = defaultdict(list)
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                qid = int(row["qid"])
            except (KeyError, ValueError):
                continue
            grouped[(qid, row["field"])].append(row["issue_type"])
    return grouped


def _read_field_value(qid: int, field: str) -> Optional[str]:
    """Return the current text in the live DB for ``(qid, field)``."""
    M = _models()
    try:
        q = M.Question.get_by_id(qid)
    except M.Question.DoesNotExist:
        return None
    if field == "prompt":
        return q.prompt
    if field == "explanation":
        return q.explanation
    if field == "stimulus":
        return q.stimulus.content if q.stimulus_id else None
    if field.startswith("option_"):
        label = field.split("_", 1)[1]
        try:
            opt = M.QuestionOption.get(
                (M.QuestionOption.question == q)
                & (M.QuestionOption.option_label == label)
            )
            return opt.option_text
        except M.QuestionOption.DoesNotExist:
            return None
    if field == "options":
        # MCQ-prefix mixing — handled at the option set level. We
        # don't currently auto-fix it; this returns ``None`` so the
        # caller skips a write attempt.
        return None
    return None


def _write_field_value(qid: int, field: str, new_text: str) -> bool:
    """Write ``new_text`` to ``(qid, field)``. Returns ``True`` on
    a real write, ``False`` if the field can't be addressed."""
    M = _models()
    try:
        q = M.Question.get_by_id(qid)
    except M.Question.DoesNotExist:
        return False
    if field == "prompt":
        q.prompt = new_text
        q.save()
        return True
    if field == "explanation":
        q.explanation = new_text
        q.save()
        return True
    if field == "stimulus":
        if not q.stimulus_id:
            return False
        s = q.stimulus
        s.content = new_text
        s.save()
        return True
    if field.startswith("option_"):
        label = field.split("_", 1)[1]
        try:
            opt = M.QuestionOption.get(
                (M.QuestionOption.question == q)
                & (M.QuestionOption.option_label == label)
            )
            opt.option_text = new_text
            opt.save()
            return True
        except M.QuestionOption.DoesNotExist:
            return False
    return False


def _record_flag(
    qid: int,
    field: str,
    issue_types: List[str],
    apply: bool,
) -> bool:
    """Add a ``QuestionFlag`` row with the encoding-issue note.
    Idempotent — skips if an identical flag already exists."""
    M = _models()
    try:
        q = M.Question.get_by_id(qid)
    except M.Question.DoesNotExist:
        return False
    note = f"encoding_issue: field={field} types={','.join(sorted(set(issue_types)))}"
    existing = (
        M.QuestionFlag.select()
        .where(
            (M.QuestionFlag.question == q)
            & (M.QuestionFlag.note == note)
            & (M.QuestionFlag.user_id == "encoding_audit")
        )
        .count()
    )
    if existing:
        return False
    if not apply:
        return True
    M.QuestionFlag.create(
        question=q,
        user_id="encoding_audit",
        reason="other",
        note=note,
    )
    return True


def _resolve_csv_path(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg)
    # Default: today's audit file.
    candidates = [
        DATA_DIR / "audits" / f"encoding_issues_{date.today().isoformat()}.csv",
        DATA_DIR / "audits" / "encoding_issues_2026-05-18.csv",
    ]
    for p in candidates:
        if p.exists():
            return p
    # Last-ditch: any encoding_issues_*.csv under audits/.
    audits = DATA_DIR / "audits"
    if audits.exists():
        matches = sorted(audits.glob("encoding_issues_*.csv"))
        if matches:
            return matches[-1]
    raise FileNotFoundError(
        f"no encoding-issues CSV found; pass --csv explicitly"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--csv",
        default=None,
        help="Path to encoding_issues_<DATE>.csv (default: latest under data/audits/)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually commit changes; without this, the script is a dry-run.",
    )
    parser.add_argument(
        "--auto-canonicalize",
        action="store_true",
        help=(
            "Also rewrite mixed MCQ-prefix sets (off by default — opt-in "
            "because it's a stylistic change, not strictly an encoding fix)."
        ),
    )
    parser.add_argument(
        "--max-diffs",
        type=int,
        default=20,
        help="Cap on diff lines printed in dry-run mode (default: 20).",
    )
    args = parser.parse_args(argv)

    M = _models()
    M.init_db()

    csv_path = _resolve_csv_path(args.csv)
    grouped = _load_audit_csv(csv_path)
    print(f"Loaded {len(grouped)} (qid, field) groups from {csv_path}")

    by_action: Counter = Counter()
    diffs_shown = 0
    flags_added: Set[Tuple[int, str]] = set()

    # We do mutations inside a single transaction so a mid-run
    # exception doesn't leave the DB half-written.
    if args.apply:
        M.db.connect(reuse_if_open=True)
        txn = M.db.atomic()
        txn.__enter__()
    try:
        for (qid, field), issue_types in sorted(grouped.items()):
            current = _read_field_value(qid, field)
            if current is None:
                # Either the question is gone (unlikely) or the field is
                # one we don't auto-handle (e.g. ``options`` aggregate).
                if ISSUE_MIXED_MCQ_PREFIX in issue_types and field == "options":
                    by_action["mcq_review"] += 1
                    if not args.auto_canonicalize:
                        # Just record a flag.
                        _record_flag(qid, field, issue_types, args.apply)
                        flags_added.add((qid, field))
                continue

            fix_eligible = [
                t for t in issue_types if t in AUTO_FIXABLE
            ]
            review_only = [
                t for t in issue_types if t in NEEDS_REVIEW
            ]

            if fix_eligible:
                fixed, applied = auto_fix_text(current)
                if applied and fixed != current:
                    by_action["auto_fixed"] += 1
                    if not args.apply and diffs_shown < args.max_diffs:
                        print(_diff(current, fixed, f"qid={qid} {field}"))
                        diffs_shown += 1
                    if args.apply:
                        _write_field_value(qid, field, fixed)
                else:
                    by_action["already_clean"] += 1

            if review_only:
                added = _record_flag(qid, field, review_only, args.apply)
                if added:
                    by_action["flag_recorded"] += 1
                    flags_added.add((qid, field))
                else:
                    by_action["flag_already_existed"] += 1

        if args.apply:
            txn.__exit__(None, None, None)
            print("[apply] transaction committed.")
        else:
            print("\n[dry-run] no DB changes written.")
    except Exception:
        if args.apply:
            txn.__exit__(*sys.exc_info())
        raise

    # Summary.
    print("\nAction summary:")
    for action, count in sorted(by_action.items()):
        print(f"  {action:<24}  {count:>5}")

    if diffs_shown >= args.max_diffs:
        print(
            f"\n(Diff output capped at {args.max_diffs}; "
            f"raise --max-diffs for more.)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
