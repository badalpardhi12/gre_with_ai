#!/usr/bin/env python3
"""
GRE Mock Database Audit -- Answer-key Drift Detector
====================================================

Inverse of the existing "Answer-key likely WRONG" path in
``scripts/audit_data_corruption.py``: that script catches the literal
phrase "correct answer is X" but misses the more common (and more
insidious) failure mode where a question has the wrong option marked
``is_correct=True`` and the explanation reasons through to the
*actual* right answer **implicitly** without using a trigger phrase.

Pipeline
--------
1. Pull every live question with a non-trivial explanation.
2. Optional cheap pre-filter: skip rows where the explanation has no
   option-label mention at all (nothing for the LLM to disagree
   about).
3. Feed (prompt, options, explanation, current_correct, subtype) to an
   LLM judge. The judge answers strictly:
   "Based on the EXPLANATION's reasoning -- not your independent
   solving of the question -- which option does the explanation
   conclude is correct?"
4. Compare ``intended_correct`` to the currently-marked correct
   label(s).
5. Triage:
   - high confidence (>=0.9) AND ``intended_correct`` is a real
     option label AND mismatches the marked correct -> **flip**
   - medium confidence (0.7-0.9) OR multi-blank/multi-answer
     ambiguity -> **retire**
   - low confidence (<0.7) OR explanation incoherent -> **skip**

Outputs
-------
- ``data/audits/answer_key_drift_<date>.json`` -- full review trail
  (intended_correct, confidence, reasoning_summary per row).
- ``data/audits/answer_key_drift_decisions_<date>.json`` -- the
  flip + retire decision lists; this file is what migration ``_036``
  replays.

Usage
-----
    python scripts/audit_answer_key_drift.py --dry-run --limit 30
    python scripts/audit_answer_key_drift.py --limit 100
    python scripts/audit_answer_key_drift.py            # full live pool

Cost note: each call is one LLM round-trip. The full live pool is
~2,600 questions with explanations; budget accordingly. Use
``--limit`` for incremental runs and ``--resume`` to skip rows
already reviewed in a previous run's findings file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///data/gre_user.db")


# ── Output paths ────────────────────────────────────────────────────

AUDIT_DIR = PROJECT_ROOT / "data" / "audits"
TODAY = date.today().isoformat()
FINDINGS_PATH = AUDIT_DIR / f"answer_key_drift_{TODAY}.json"
DECISIONS_PATH = AUDIT_DIR / f"answer_key_drift_decisions_{TODAY}.json"


# ── LLM judge prompt ───────────────────────────────────────────────

# We instruct the judge to ground its verdict in the EXPLANATION's
# reasoning rather than re-solve the question independently. The bug
# we're hunting is "explanation reasons to one answer, key marks
# another"; we do NOT want the LLM to second-guess the explanation
# author when the reasoning is unambiguous about the intended answer.

SYSTEM_PROMPT = (
    "You are an expert GRE content auditor. Given a question, its answer "
    "options, the currently-marked correct option(s), and an EXPLANATION "
    "that someone wrote to justify the correct answer, your job is to "
    "determine which option THE EXPLANATION concludes is correct.\n\n"
    "Critical instruction: ground your verdict in the EXPLANATION's "
    "reasoning, NOT your independent solving of the question. If the "
    "explanation reasons toward option C but the question is currently "
    "marked B, you should return C -- you are reading the explanation, "
    "not grading the question.\n\n"
    "Return null (the JSON literal null, not the string \"null\") for "
    "intended_correct in any of these cases:\n"
    "  * the explanation is incoherent or contradicts itself\n"
    "  * the explanation does not commit to a single option\n"
    "  * the explanation only reasons in general terms without picking\n"
    "  * for a multi-blank/multi-answer item, the explanation only\n"
    "    discusses some of the blanks/answers (do not partially flip)\n\n"
    "For multi-blank text completion (TC) or multi-answer (mcq_multi) "
    "items, return a list of all option labels the explanation defends "
    "(e.g. [\"blank1_C\", \"blank2_A\"] or [\"A\", \"D\"]) when the "
    "explanation is unambiguous about ALL of them. Otherwise null.\n\n"
    "confidence is a float in [0, 1] reflecting how confidently you "
    "could pin down the explanation's intended answer. Use 0.95+ when "
    "the explanation explicitly names the option label and defends it; "
    "0.8-0.9 when reasoning is unmistakable but never names the label; "
    "0.6-0.8 when reasoning seems to lean one way but is muddy; <0.5 "
    "when the explanation is too vague.\n\n"
    "Respond with strict JSON only -- no markdown, no preamble. "
    "Schema: {\"intended_correct\": <label-string-or-list-or-null>, "
    "\"confidence\": <float>, \"reasoning_summary\": \"<<=300 chars\"}."
)


# ── Throttling & retry ─────────────────────────────────────────────

THROTTLE_SECONDS = 0.4
RETRY_COUNT = 3
RETRY_BACKOFF_BASE = 2.0


# ── Pre-filter heuristic ───────────────────────────────────────────

_LABEL_HINT_RE = re.compile(r"\b([A-F])\b")
_BLANK_LABEL_RE = re.compile(r"\bblank[12]_[A-F]\b", re.IGNORECASE)


def _explanation_mentions_any_label(explanation: str, option_labels) -> bool:
    """Quick gate: does the explanation reference at least one option
    label by name? An explanation that does NOT mention any label has
    nothing to disagree about with the answer key, so the LLM call
    would be wasted."""
    if not explanation:
        return False
    text = explanation
    for lbl in option_labels:
        if lbl in text:
            return True
    # Also accept bare A-F mentions (the explanation may say "option C"
    # without the underscored variants).
    if _LABEL_HINT_RE.search(text):
        return True
    if _BLANK_LABEL_RE.search(text):
        return True
    return False


# ── HTML cleanup ───────────────────────────────────────────────────

def _strip_html_for_prompt(text: str) -> str:
    if not text:
        return ""
    s = unescape(text)
    s = re.sub(r"<\s*br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    return s.strip()


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[:n].rstrip() + " ...[truncated]"


# ── Prompt builder ─────────────────────────────────────────────────

def build_user_prompt(question, options) -> str:
    """Render a question, its options, and its explanation for the judge."""
    correct_labels = sorted({opt.option_label for opt in options if opt.is_correct})
    lines = []
    lines.append(f"subtype: {question.subtype}")
    lines.append(f"currently_marked_correct: {correct_labels}")
    lines.append("")
    lines.append("PROMPT:")
    lines.append(_truncate(_strip_html_for_prompt(question.prompt or ""), 1500))
    lines.append("")
    lines.append("OPTIONS:")
    for opt in sorted(options, key=lambda o: o.option_label):
        marker = "*" if opt.is_correct else " "
        lines.append(
            f"  [{marker}] {opt.option_label}. "
            f"{_truncate(_strip_html_for_prompt(opt.option_text), 200)}"
        )
    lines.append("")
    lines.append("EXPLANATION:")
    lines.append(_truncate(_strip_html_for_prompt(question.explanation or ""), 2500))
    lines.append("")
    lines.append(
        "Question: based on the EXPLANATION's reasoning, which option "
        "(or list of options) does the explanation conclude is correct? "
        "Return the JSON now."
    )
    return "\n".join(lines)


# ── LLM call & parse ───────────────────────────────────────────────

def _coerce_intended(value):
    """Normalize ``intended_correct`` field to either None, a single
    label string, or a sorted list of label strings."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        if not v or v.lower() == "null":
            return None
        return v
    if isinstance(value, list):
        labels = []
        for item in value:
            if isinstance(item, str) and item.strip():
                labels.append(item.strip())
        if not labels:
            return None
        return sorted(set(labels))
    return None


def _coerce_judgement(parsed) -> Optional[Dict]:
    if not isinstance(parsed, dict):
        return None
    intended = _coerce_intended(parsed.get("intended_correct"))
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    summary = parsed.get("reasoning_summary") or ""
    if not isinstance(summary, str):
        summary = str(summary)
    summary = summary.strip().replace("\n", " ")[:400]
    return {
        "intended_correct": intended,
        "confidence": confidence,
        "reasoning_summary": summary,
    }


def _call_with_retry(llm_service, system_prompt, user_prompt, model=None):
    last_err = None
    for attempt in range(RETRY_COUNT):
        try:
            raw = llm_service.generate(
                system_prompt, user_prompt, model=model
            )
            text = (raw or "").strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [
                    line for line in lines
                    if not line.strip().startswith("```")
                ]
                text = "\n".join(lines).strip()
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                start = text.find("{")
                end = text.rfind("}")
                if start != -1 and end != -1 and end > start:
                    parsed = json.loads(text[start:end + 1])
                else:
                    raise
            judgement = _coerce_judgement(parsed)
            if judgement is None:
                raise ValueError(f"malformed verdict; raw={text[:200]!r}")
            return judgement, None
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            wait = RETRY_BACKOFF_BASE * (2 ** attempt)
            print(
                f"[audit_answer_key_drift]   retry "
                f"{attempt + 1}/{RETRY_COUNT} after error: {exc} "
                f"(sleeping {wait:.1f}s)",
                file=sys.stderr,
            )
            time.sleep(wait)
    return None, last_err


# ── Triage logic ───────────────────────────────────────────────────

def triage(question, options, judgement) -> Tuple[str, str]:
    """Map a judgement to a (decision, reason) pair.

    decision ∈ {"flip", "retire", "skip"}.

    Critical: a flip is destructive (changes which answer is graded
    correct) and easy to get wrong, so we require BOTH:
      1. high LLM confidence (>=0.9) AND
      2. independent heuristic agreement -- the explanation must
         explicitly defend the LLM's intended_correct label using
         the same near-positive-verdict heuristic from
         ``scripts.audit_data_corruption._count_option_label_defenses``.

    The heuristic acts as a "second opinion": if the LLM says the
    explanation defends C but the heuristic finds zero defense
    counts for C, the LLM is probably hallucinating. In that case
    we retire instead of flip (or skip if confidence is medium).
    """
    intended = judgement["intended_correct"]
    confidence = judgement["confidence"]
    summary = judgement["reasoning_summary"]

    # No verdict -> skip.
    if intended is None:
        return ("skip", "judge declined to commit (intended_correct=null)")

    current_correct = sorted(
        {opt.option_label for opt in options if opt.is_correct}
    )
    real_labels = {opt.option_label for opt in options}

    is_multi = (
        question.subtype in ("tc", "mcq_multi", "se")
        or any("_" in lbl for lbl in real_labels)
    )

    # Normalize intended to a sorted list for comparison.
    if isinstance(intended, list):
        intended_set = sorted(set(intended))
    else:
        intended_set = [intended]

    # Validate: every intended label must be a real option label on
    # this question. If not, the LLM hallucinated a label -> skip.
    bogus = [lbl for lbl in intended_set if lbl not in real_labels]
    if bogus:
        return ("skip", f"judge hallucinated label(s): {bogus}")

    # Same as marked? No drift -> skip silently.
    if intended_set == current_correct:
        return ("skip", "judge agrees with current key")

    # Low confidence is always skip, regardless of subtype. The
    # judge isn't sure enough for either flip OR retire.
    if confidence < 0.7:
        return ("skip", f"low confidence ({confidence:.2f})")

    # Multi-blank / multi-answer: ALWAYS retire on drift, never flip.
    # Partial flips are worse than no flip for the learner.
    if is_multi:
        return (
            "retire",
            f"multi-blank/multi-answer drift "
            f"(judge says {intended_set}, key says {current_correct}, "
            f"conf={confidence:.2f})",
        )

    # Single-answer items.
    if confidence >= 0.9:
        # Sanity: the explanation summary must defend the intended
        # label. If the summary is empty, the judge wasn't grounded.
        if not summary:
            return (
                "retire",
                f"single-answer drift but judge produced no reasoning "
                f"(conf={confidence:.2f})",
            )
        # Independent heuristic second-opinion.
        try:
            from scripts.audit_data_corruption import (
                _count_option_label_defenses,
            )
        except Exception:  # noqa: BLE001
            _count_option_label_defenses = None
        if _count_option_label_defenses is not None:
            defenses = _count_option_label_defenses(
                question.explanation or "", real_labels,
            )
            intended_label = intended_set[0]
            current_label = current_correct[0] if current_correct else None
            intended_count = defenses.get(intended_label, 0)
            current_count = (
                defenses.get(current_label, 0) if current_label else 0
            )
            # Heuristic strongly disagrees with LLM: heuristic finds
            # the explanation defends the CURRENT marked correct
            # option but NOT the LLM's claimed intended one. The LLM
            # is almost certainly hallucinating drift; SKIP rather
            # than retire (retiring would remove a perfectly good
            # question from the pool).
            if intended_count == 0 and current_count > 0:
                return (
                    "skip",
                    f"LLM says flip to {intended_label} (conf="
                    f"{confidence:.2f}) but heuristic finds 0 defense "
                    f"counts for {intended_label} vs {current_count} "
                    f"for marked {current_label}. Likely LLM error; "
                    f"keeping question live."
                )
            # Heuristic finds neither option defended explicitly --
            # the explanation may be vague but the LLM is committed.
            # Skip rather than retire: in practice this branch fires
            # most often when the LLM hallucinates drift on a fine
            # single-answer question, and retiring valid content is
            # worse than the (rare) missed real bug. The launch-time
            # ``audit_data_corruption.py`` heuristic is the safety
            # net for the truly-broken cases that this skip lets
            # through.
            if intended_count == 0:
                return (
                    "skip",
                    f"LLM says flip to {intended_label} (conf="
                    f"{confidence:.2f}) but heuristic finds 0 "
                    f"explicit defenses for any option. Likely LLM "
                    f"error; keeping question live."
                )
        return (
            "flip",
            f"explanation defends {intended_set} but key has "
            f"{current_correct}; conf={confidence:.2f}",
        )

    # Single-answer with confidence in [0.7, 0.9): drift is real
    # enough to act on, but not confident enough to flip. Retire so
    # a learner doesn't see a question we suspect is broken.
    return (
        "retire",
        f"medium-confidence drift "
        f"(conf={confidence:.2f}); retire rather than flip",
    )


# ── Findings / Decisions IO ────────────────────────────────────────

def load_findings(path: Path) -> Dict[int, Dict]:
    """Load existing findings keyed by qid for resume support."""
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            doc = json.load(f)
    except (ValueError, OSError):
        return {}
    findings = doc.get("findings", []) if isinstance(doc, dict) else []
    out = {}
    for row in findings:
        try:
            out[int(row["qid"])] = row
        except (KeyError, TypeError, ValueError):
            continue
    return out


def write_findings(path: Path, findings: List[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(),
        "count": len(findings),
        "findings": findings,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(path)


def write_decisions(path: Path, findings: List[Dict]) -> Dict:
    """Pull the flip + retire decisions out of the findings list and
    write a small machine-replayable JSON file. This is what migration
    ``_036_*`` reads as its source of truth."""
    flips = []
    retires = []
    for row in findings:
        decision = row.get("decision")
        if decision == "flip":
            flips.append({
                "qid": int(row["qid"]),
                "from_correct": row["current_correct"],
                "to_correct": row["intended_correct"],
                "confidence": row["confidence"],
                "reason": row["decision_reason"],
            })
        elif decision == "retire":
            retires.append({
                "qid": int(row["qid"]),
                "reason": row["decision_reason"],
                "current_correct": row["current_correct"],
                "intended_correct": row["intended_correct"],
                "confidence": row["confidence"],
            })
    payload = {
        "generated_at": datetime.now().isoformat(),
        "flip_count": len(flips),
        "retire_count": len(retires),
        "flips": flips,
        "retires": retires,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(path)
    return payload


# ── Main audit loop ────────────────────────────────────────────────

def audit_answer_key_drift(
    *,
    limit: Optional[int] = None,
    resume: bool = True,
    dry_run: bool = False,
    skip_no_label_mention: bool = True,
    model: Optional[str] = None,
    findings_path: Path = FINDINGS_PATH,
    decisions_path: Path = DECISIONS_PATH,
) -> Dict:
    """Run the answer-key drift audit. Returns a counts summary."""
    # Lazy import so tests using ``temp_db`` (which evicts ``models.*``
    # from sys.modules between tests) re-bind to the current test's DB
    # rather than holding a reference from script-import time.
    from models.database import (
        db, Question, QuestionOption, init_db,
    )

    init_db()
    db.connect(reuse_if_open=True)

    # Pull the live pool. Skip questions with very short or empty
    # explanations (the judge has nothing to reason about).
    queryset = (
        Question
        .select()
        .where(
            (Question.status == "live")
            & (Question.explanation.is_null(False))
        )
    )
    candidates = []
    for q in queryset:
        expl = (q.explanation or "").strip()
        if len(expl) < 40:
            continue
        candidates.append(q)

    # Sort deterministically so resume is stable.
    candidates.sort(key=lambda q: q.id)

    # Resume support: drop already-reviewed qids.
    existing = load_findings(findings_path) if resume else {}
    if existing:
        candidates = [q for q in candidates if q.id not in existing]
        print(
            f"[audit_answer_key_drift] resume: skipping "
            f"{len(existing)} already-reviewed qids"
        )

    if limit is not None:
        candidates = candidates[:limit]

    print(
        f"[audit_answer_key_drift] reviewing {len(candidates)} "
        f"live questions (dry_run={dry_run})"
    )

    # Lazy import the LLM service so a dry run on an empty test DB
    # doesn't require an API key.
    llm_service = None
    if not dry_run:
        from services.llm_service import llm_service as _svc
        llm_service = _svc

    findings: List[Dict] = list(existing.values())
    counts = defaultdict(int)
    counts["reviewed"] = 0
    counts["flip"] = 0
    counts["retire"] = 0
    counts["skip"] = 0
    counts["filtered_no_label_mention"] = 0
    counts["llm_error"] = 0

    for i, q in enumerate(candidates, start=1):
        options = list(
            QuestionOption.select().where(QuestionOption.question == q)
        )
        if not options:
            counts["skip"] += 1
            continue

        correct_labels = sorted(
            {opt.option_label for opt in options if opt.is_correct}
        )
        if not correct_labels:
            counts["skip"] += 1
            continue

        all_labels = {opt.option_label for opt in options}

        # Cheap pre-filter: bail out when the explanation has no
        # option-label mention. Skips questions whose explanations
        # don't even discuss specific options (purely conceptual).
        if skip_no_label_mention:
            if not _explanation_mentions_any_label(
                q.explanation or "", all_labels
            ):
                counts["filtered_no_label_mention"] += 1
                # Record so resume sees this row was considered.
                findings.append({
                    "qid": q.id,
                    "subtype": q.subtype,
                    "current_correct": correct_labels,
                    "intended_correct": None,
                    "confidence": 0.0,
                    "reasoning_summary": "",
                    "decision": "skip",
                    "decision_reason": "no option-label mention in explanation",
                })
                continue

        if dry_run:
            judgement = {
                "intended_correct": None,
                "confidence": 0.0,
                "reasoning_summary": "[dry-run]",
            }
            decision, reason = "skip", "dry-run"
        else:
            counts["reviewed"] += 1
            user_prompt = build_user_prompt(q, options)
            judgement, err = _call_with_retry(
                llm_service, SYSTEM_PROMPT, user_prompt, model=model,
            )
            if judgement is None:
                counts["llm_error"] += 1
                findings.append({
                    "qid": q.id,
                    "subtype": q.subtype,
                    "current_correct": correct_labels,
                    "intended_correct": None,
                    "confidence": 0.0,
                    "reasoning_summary": "",
                    "decision": "skip",
                    "decision_reason": f"llm error: {err}",
                })
                # Persist progress so a long run survives a Ctrl-C.
                if i % 25 == 0:
                    write_findings(findings_path, findings)
                time.sleep(THROTTLE_SECONDS)
                continue
            decision, reason = triage(q, options, judgement)
            time.sleep(THROTTLE_SECONDS)

        counts[decision] += 1
        findings.append({
            "qid": q.id,
            "subtype": q.subtype,
            "current_correct": correct_labels,
            "intended_correct": judgement["intended_correct"],
            "confidence": judgement["confidence"],
            "reasoning_summary": judgement["reasoning_summary"],
            "decision": decision,
            "decision_reason": reason,
        })

        # Periodic checkpoint -- this run can take hours.
        if i % 25 == 0:
            print(
                f"[audit_answer_key_drift] {i}/{len(candidates)} "
                f"flip={counts['flip']} retire={counts['retire']} "
                f"skip={counts['skip']} err={counts['llm_error']}"
            )
            write_findings(findings_path, findings)

    # Final write.
    write_findings(findings_path, findings)
    decision_payload = write_decisions(decisions_path, findings)

    print(
        f"\n[audit_answer_key_drift] done.\n"
        f"  total reviewed: {counts['reviewed']}\n"
        f"  flip: {counts['flip']}\n"
        f"  retire: {counts['retire']}\n"
        f"  skip: {counts['skip']}\n"
        f"  filtered (no label mention): "
        f"{counts['filtered_no_label_mention']}\n"
        f"  llm errors: {counts['llm_error']}\n"
        f"  findings: {findings_path}\n"
        f"  decisions: {decisions_path}\n"
        f"    flips: {decision_payload['flip_count']}\n"
        f"    retires: {decision_payload['retire_count']}"
    )
    return dict(counts)


# ── CLI ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap how many questions to review this run.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip the LLM call; just walk the candidate set and "
             "print counts. Useful for verifying the pre-filter.",
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Ignore any existing findings file and start fresh.",
    )
    parser.add_argument(
        "--include-nolabel", action="store_true",
        help="Disable the 'no option-label mention' pre-filter.",
    )
    parser.add_argument(
        "--model", default=None,
        help="Override the LLM model (else uses the configured default).",
    )
    args = parser.parse_args()

    audit_answer_key_drift(
        limit=args.limit,
        resume=not args.no_resume,
        dry_run=args.dry_run,
        skip_no_label_mention=not args.include_nolabel,
        model=args.model,
    )


if __name__ == "__main__":
    main()
