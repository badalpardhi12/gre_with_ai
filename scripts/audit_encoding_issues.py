#!/usr/bin/env python3
"""Read-only audit: encoding-issue scan of every live question.

Phase 3.3 of the cleanup plan. Walks the live question pool plus the
stimuli they reference and runs ``services.sanitize.find_latex_encoding_issues``
over each text field. Per-row issues are written to a CSV; per-issue-type
counts to a JSON summary. Prints a human-readable summary on stdout.

Output paths
------------
    data/audits/encoding_issues_<DATE>.csv
    data/audits/encoding_summary_<DATE>.json

Usage
-----
    venv/bin/python scripts/audit_encoding_issues.py
    venv/bin/python scripts/audit_encoding_issues.py --date 2026-05-18
    venv/bin/python scripts/audit_encoding_issues.py --output-dir /tmp
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

# Importable regardless of cwd.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import DATA_DIR  # noqa: E402
from models.database import (  # noqa: E402
    Question,
    QuestionOption,
    Stimulus,
    init_db,
)
from services.sanitize import (  # noqa: E402
    find_latex_encoding_issues,
    find_mcq_option_issues,
)


CSV_HEADER = ["qid", "field", "issue_type", "snippet"]


def _scan_questions() -> Iterable[Tuple[int, str, str, str]]:
    """Yield ``(qid, field, issue_type, snippet)`` rows for every
    encoding hazard found in any live question's prompt, explanation,
    or option_text.

    For Stimulus, we report the qid of every question that references
    it (multiple emissions per stimulus, same qid prefix per emission).
    """
    # Pre-load stimulus issues once — many questions share a stimulus,
    # and re-running the regex per question would be wasteful.
    stim_issues: Dict[int, List[Tuple[str, str]]] = {}
    for s in Stimulus.select():
        hits = find_latex_encoding_issues(s.content or "")
        if hits:
            stim_issues[s.id] = hits

    # Walk live questions in stable order (by id) so the CSV is
    # diff-friendly across runs.
    for q in (
        Question.select()
        .where(Question.status == "live")
        .order_by(Question.id)
    ):
        # Prompt.
        for issue, snip in find_latex_encoding_issues(q.prompt or ""):
            yield q.id, "prompt", issue, snip

        # Explanation. Empty or default for many imported items.
        if q.explanation:
            for issue, snip in find_latex_encoding_issues(q.explanation):
                yield q.id, "explanation", issue, snip

        # Stimulus content (deduped via stim_issues).
        if q.stimulus_id and q.stimulus_id in stim_issues:
            for issue, snip in stim_issues[q.stimulus_id]:
                yield q.id, "stimulus", issue, snip

        # Options.
        opts = list(
            QuestionOption.select()
            .where(QuestionOption.question == q)
            .order_by(QuestionOption.option_label)
        )
        for opt in opts:
            for issue, snip in find_latex_encoding_issues(opt.option_text or ""):
                yield (
                    q.id,
                    f"option_{opt.option_label}",
                    issue,
                    snip,
                )

        # MCQ-prefix mixing — only meaningful at the option-set level.
        opt_texts = [o.option_text or "" for o in opts]
        for issue, snip in find_mcq_option_issues(opt_texts):
            yield q.id, "options", issue, snip


def _format_summary(by_type: Counter, by_field: Counter, total_q: int) -> str:
    width = max((len(k) for k in by_type), default=12)
    lines = ["", f"{'issue_type':<{width}}  count"]
    lines.append("-" * (width + 12))
    for issue, count in by_type.most_common():
        lines.append(f"{issue:<{width}}  {count:>5}")
    lines.append("")
    lines.append("by field:")
    width_f = max((len(k) for k in by_field), default=12)
    for field, count in by_field.most_common():
        lines.append(f"  {field:<{width_f}}  {count:>5}")
    lines.append("")
    lines.append(f"total live questions scanned: {total_q}")
    return "\n".join(lines)


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--output-dir",
        default=str(DATA_DIR / "audits"),
        help="Directory for the output CSV + JSON (default: data/audits/)",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Date stamp for output filenames (default: today)",
    )
    args = parser.parse_args(argv)

    started = time.time()
    init_db()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"encoding_issues_{args.date}.csv"
    json_path = output_dir / f"encoding_summary_{args.date}.json"

    by_type: Counter = Counter()
    by_field: Counter = Counter()
    seen_qids: set = set()
    rows_written = 0

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for qid, field, issue, snip in _scan_questions():
            writer.writerow([qid, field, issue, snip])
            by_type[issue] += 1
            by_field[field] += 1
            seen_qids.add(qid)
            rows_written += 1

    total_q = (
        Question.select().where(Question.status == "live").count()
    )
    summary = {
        "generated_at": args.date,
        "total_live_questions": total_q,
        "questions_with_at_least_one_issue": len(seen_qids),
        "rows_written": rows_written,
        "by_issue_type": dict(by_type.most_common()),
        "by_field": dict(by_field.most_common()),
        "csv_path": str(csv_path.relative_to(_REPO_ROOT)),
    }
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    elapsed = time.time() - started
    print(_format_summary(by_type, by_field, total_q))
    print(f"\nCSV:     {csv_path}")
    print(f"JSON:    {json_path}")
    print(f"elapsed: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
