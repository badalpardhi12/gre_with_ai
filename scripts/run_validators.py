#!/usr/bin/env python3
"""Run all live-content validators and write findings to ``data/audits/``.

Usage:
    venv/bin/python scripts/run_validators.py
    venv/bin/python scripts/run_validators.py --sample 200
    venv/bin/python scripts/run_validators.py --measure quant

Outputs (idempotent, overwritten on each run):
    data/audits/validator_findings_2026_05_18.csv
    data/audits/validator_summary_2026_05_18.json

Exit codes:
    0 = ran successfully (findings may still be present; see CSV)
    1 = unexpected error
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Make project root importable when running from any cwd.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from models.database import (  # noqa: E402
    AWAPrompt,
    Question,
    db,
    init_db,
)
from validators import (  # noqa: E402
    validate_awa,
    validate_quant,
    validate_verbal,
)


AUDIT_DATE_TAG = "2026_05_18"
AUDIT_DIR = _ROOT / "data" / "audits"
CSV_PATH = AUDIT_DIR / f"validator_findings_{AUDIT_DATE_TAG}.csv"
JSON_PATH = AUDIT_DIR / f"validator_summary_{AUDIT_DATE_TAG}.json"


def _route(question):
    """Pick a validator for a Question row by measure."""
    measure = (question.measure or "").lower()
    if measure == "quant":
        return validate_quant
    if measure == "verbal":
        return validate_verbal
    return None  # AWA Question rows not currently used; AWAPrompt loop handles AWA.


def _walk_questions(measure_filter, sample_limit):
    """Yield (qid, measure, subtype, finding) tuples for live questions."""
    q = Question.select().where(Question.status == "live")
    if measure_filter:
        q = q.where(Question.measure == measure_filter)
    q = q.order_by(Question.id)
    if sample_limit:
        q = q.limit(sample_limit)
    for row in q:
        validator = _route(row)
        if validator is None:
            continue
        try:
            findings = validator(row)
        except Exception as exc:  # noqa: BLE001
            yield (
                row.id,
                row.measure,
                row.subtype,
                _make_runtime_finding(row, exc),
            )
            continue
        for f in findings:
            yield row.id, row.measure, row.subtype, f


def _walk_awa():
    """Yield (pid, 'awa', 'awa_issue', finding) tuples for every AWA prompt."""
    for row in AWAPrompt.select().order_by(AWAPrompt.id):
        try:
            findings = validate_awa(row)
        except Exception as exc:  # noqa: BLE001
            yield (
                row.id,
                "awa",
                "awa_issue",
                _make_runtime_finding(row, exc, prefix="AWA"),
            )
            continue
        for f in findings:
            yield row.id, "awa", "awa_issue", f


def _make_runtime_finding(row, exc, prefix="QUESTION"):
    from validators.findings import SEVERITY_ERROR, ValidationFinding
    return ValidationFinding(
        rule_id=f"{prefix}_VALIDATOR_RAISED",
        severity=SEVERITY_ERROR,
        message=f"validator raised {type(exc).__name__}: {exc}",
        details={"id": row.id, "exception_type": type(exc).__name__},
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample", type=int, default=None,
        help="Cap the number of live Questions checked (per-measure cap "
             "applies before AWA prompts).",
    )
    parser.add_argument(
        "--measure", choices=["quant", "verbal", "awa"], default=None,
        help="Restrict to a single measure (default: all).",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-rule summary print at end.",
    )
    args = parser.parse_args(argv)

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    if db.is_closed():
        db.connect(reuse_if_open=True)

    started_at = datetime.utcnow().isoformat() + "Z"

    rows = []
    sample_qids = defaultdict(list)
    by_rule = Counter()
    by_severity = Counter()

    # Quant + Verbal walk (AWA Questions are not live; Phase 6 may add them).
    if args.measure != "awa":
        for qid, measure, subtype, f in _walk_questions(
                args.measure, args.sample):
            rows.append((qid, measure, subtype, f))
            by_rule[f.rule_id] += 1
            by_severity[f.severity] += 1
            if len(sample_qids[f.rule_id]) < 5:
                sample_qids[f.rule_id].append(qid)

    if args.measure in (None, "awa"):
        for pid, measure, subtype, f in _walk_awa():
            rows.append((pid, measure, subtype, f))
            by_rule[f.rule_id] += 1
            by_severity[f.severity] += 1
            if len(sample_qids[f.rule_id]) < 5:
                sample_qids[f.rule_id].append(pid)

    # Total live items checked (for the summary header).
    total_q = (
        Question
        .select()
        .where(Question.status == "live")
        .count()
    )
    if args.measure and args.measure != "awa":
        total_q = (
            Question
            .select()
            .where(Question.status == "live", Question.measure == args.measure)
            .count()
        )
    if args.sample is not None:
        total_q = min(total_q, args.sample)
    total_awa = AWAPrompt.select().count()
    if args.measure == "quant" or args.measure == "verbal":
        total_awa = 0
    if args.measure == "awa":
        total_q = 0

    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "qid", "measure", "subtype",
            "rule_id", "severity", "message", "details_json",
        ])
        for qid, measure, subtype, f in rows:
            w.writerow([
                qid, measure, subtype,
                f.rule_id, f.severity, f.message,
                json.dumps(f.details, sort_keys=True),
            ])

    summary = {
        "generated_at": started_at,
        "total_live_questions": total_q,
        "total_awa_prompts": total_awa,
        "total_findings": len(rows),
        "by_severity": {
            "error": by_severity.get("error", 0),
            "warning": by_severity.get("warning", 0),
        },
        "by_rule_id": dict(sorted(by_rule.items())),
        "sample_qids": {k: v for k, v in sorted(sample_qids.items())},
        "args": {
            "measure": args.measure,
            "sample": args.sample,
        },
    }
    JSON_PATH.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    if not args.quiet:
        print(f"Validators run at {started_at}")
        print(f"  live questions checked: {total_q}")
        print(f"  awa prompts checked:    {total_awa}")
        print(f"  total findings:         {len(rows)} "
              f"(error={by_severity.get('error', 0)}, "
              f"warning={by_severity.get('warning', 0)})")
        if by_rule:
            print("  by rule_id:")
            for rule_id, count in sorted(
                    by_rule.items(), key=lambda kv: -kv[1]):
                print(f"    {rule_id:40s} {count}")
        print(f"\n  CSV:  {CSV_PATH}")
        print(f"  JSON: {JSON_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
