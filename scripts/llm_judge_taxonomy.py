#!/usr/bin/env python3
"""Phase 2.2 — LLM-judge taxonomy backfill.

Backfills the ``topic``, ``subtopic``, and ``question_type`` columns on
live ``Question`` rows whose values are NULL/empty/non-canonical, using
an LLM to classify each question against ``models.taxonomy``.

Why this script
---------------
The audit (``scripts/audit_taxonomy.py``) flags ~1,500 live rows with
empty ``topic``/``subtopic`` and ~2,500 with empty ``question_type``.
The cleanup plan calls for an LLM judge that fills the gaps with values
constrained to the canonical taxonomy enums. ``question_type`` is
deterministically derived from ``Question.subtype`` via the same
mapping the audit uses; only ``topic``/``subtopic`` actually require
LLM inference.

Strict-output contract
----------------------
The LLM is asked to return JSON ``{"topic": ..., "subtopic": ...}``.
Anything outside the allowlist for the row's ``measure`` is rejected;
on rejection we retry once with a stricter prompt and skip + log if
still invalid.

Dual-DB writes
--------------
Both ``data/gre_user.db`` (Peewee) and ``data/gre_mock.db`` (raw
sqlite3) are updated for each row. Without writing the seed, the next
``services.seed_sync.reconcile_if_stale`` would clobber the user-DB
backfill: ``topic``/``subtopic``/``question_type`` are seed-authored
columns. Each row is its own transaction so an interrupt mid-run
leaves a partial-but-consistent state, and the next run resumes by
skipping rows that are already populated.

Usage
-----
    venv/bin/python scripts/llm_judge_taxonomy.py --dry-run --limit 20
    venv/bin/python scripts/llm_judge_taxonomy.py --limit 50
    venv/bin/python scripts/llm_judge_taxonomy.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Project root on sys.path so ``models`` / ``services`` import cleanly.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DB_PATH, SEED_DB_PATH  # noqa: E402
from models.taxonomy import (  # noqa: E402
    AWA_TAXONOMY,
    QUANT_TAXONOMY,
    VERBAL_TAXONOMY,
)


# ── subtype → canonical question_type ───────────────────────────────
# Mirrors ``scripts.audit_taxonomy.CANONICAL_QUESTION_TYPE_FOR_SUBTYPE``
# but maps to a single canonical value (we *write* one value, the audit
# *accepts* multiple). Picking the long-form canonical label everywhere.
SUBTYPE_TO_QUESTION_TYPE: Dict[str, str] = {
    # Quant
    "qc": "quantitative_comparison",
    "mcq_single": "multiple_choice",
    "mcq_multi": "multiple_choice_select_all",
    "numeric_entry": "numeric_entry",
    "data_interp": "data_interpretation",
    # Verbal
    "tc": "text_completion",
    "se": "sentence_equivalence",
    "rc_single": "reading_comprehension",
    "rc_multi": "reading_comprehension_multi",
    "rc_select_passage": "select_in_passage",
    # AWA
    "awa_issue": "analyze_an_issue",
}


def measure_taxonomy(measure: str) -> Dict[str, Any]:
    """Return the taxonomy dict for a given measure."""
    if measure == "quant":
        return QUANT_TAXONOMY
    if measure == "verbal":
        return VERBAL_TAXONOMY
    if measure == "awa":
        return AWA_TAXONOMY
    raise ValueError("unknown measure: {!r}".format(measure))


def allowed_topic_subtopic_pairs(measure: str) -> Set[Tuple[str, str]]:
    """Return the set of valid (topic, subtopic) tuples for a measure."""
    pairs: Set[Tuple[str, str]] = set()
    tax = measure_taxonomy(measure)
    for topic, td in tax.items():
        for sub in td.get("subtopics", {}):
            pairs.add((topic, sub))
    return pairs


def taxonomy_summary_for_prompt(measure: str) -> str:
    """Build a compact, LLM-readable enumeration of valid (topic, subtopic)
    pairs for the given measure, including the human-friendly
    ``display_name`` and (when present) example concepts. Concepts give
    the model enough signal to disambiguate without the full taxonomy
    bloating the prompt."""
    tax = measure_taxonomy(measure)
    lines: List[str] = []
    for topic, td in tax.items():
        topic_display = td.get("display_name", topic)
        lines.append(
            "TOPIC: {} (id={!r})".format(topic_display, topic)
        )
        for sub_id, sd in td.get("subtopics", {}).items():
            sub_display = sd.get("display_name", sub_id)
            concepts = sd.get("concepts") or []
            concepts_text = ""
            if concepts:
                concepts_text = " — concepts: " + ", ".join(concepts)
            lines.append(
                "  - subtopic id={!r} ({}{})".format(
                    sub_id, sub_display, concepts_text
                )
            )
    return "\n".join(lines)


SYSTEM_PROMPT_TEMPLATE = (
    "You are a GRE content classifier. You will be given a single GRE "
    "question (measure={measure}, subtype={subtype}). Your job is to map it "
    "to the single most-appropriate (topic, subtopic) pair from the "
    "canonical {measure} taxonomy below.\n\n"
    "{measure} taxonomy:\n{taxonomy_text}\n\n"
    "Output rules (STRICT):\n"
    "- Respond with a single JSON object: "
    "{{\"topic\": \"<topic_id>\", \"subtopic\": \"<subtopic_id>\"}}\n"
    "- ``topic`` MUST be exactly one of the topic ids listed above.\n"
    "- ``subtopic`` MUST be exactly one of the subtopic ids nested under that topic.\n"
    "- Do NOT invent new ids, do NOT translate ids to display names, do NOT add other keys.\n"
    "- No prose, no markdown fences, no commentary — JSON object only."
)

RETRY_SUFFIX = (
    "\n\nYour previous reply was rejected because it was either invalid JSON "
    "or contained ids not in the allowlist. Output ONLY the JSON object with "
    "ids exactly as listed in the taxonomy above."
)


def build_user_prompt(
    qid: int,
    prompt: str,
    options: List[Tuple[str, str, bool]],
    explanation: str,
    measure: str,
    subtype: str,
) -> str:
    """Assemble the user prompt that asks the LLM to classify one question."""
    lines: List[str] = []
    lines.append("Question id: {}".format(qid))
    lines.append("Measure: {} | Subtype: {}".format(measure, subtype))
    lines.append("")
    lines.append("=== PROMPT ===")
    lines.append(prompt or "(empty)")
    if options:
        lines.append("")
        lines.append("=== OPTIONS ===")
        for label, text, is_correct in options:
            marker = "*" if is_correct else " "
            lines.append("{} {}: {}".format(marker, label, text))
    if explanation:
        lines.append("")
        lines.append("=== EXPLANATION ===")
        lines.append(explanation)
    lines.append("")
    lines.append(
        "Classify this question. Reply with the JSON object only."
    )
    return "\n".join(lines)


def parse_judge_response(
    raw: Any,
    measure: str,
) -> Optional[Dict[str, str]]:
    """Validate an LLM judge response.

    Returns the validated dict ``{"topic", "subtopic"}`` or ``None`` if
    the payload is malformed or contains values outside the canonical
    taxonomy for ``measure``.

    Accepts either a pre-parsed dict (``llm_service.generate_json``) or
    a raw JSON string (defensive: handles ``generate`` callers).
    """
    payload: Any
    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            # Strip markdown fences in case ``generate_json`` didn't.
            stripped_lines = [
                ln for ln in text.split("\n")
                if not ln.strip().startswith("```")
            ]
            text = "\n".join(stripped_lines)
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            return None
    else:
        return None

    if not isinstance(payload, dict):
        return None

    topic = payload.get("topic")
    subtopic = payload.get("subtopic")
    if not isinstance(topic, str) or not isinstance(subtopic, str):
        return None
    topic = topic.strip()
    subtopic = subtopic.strip()
    if not topic or not subtopic:
        return None

    pairs = allowed_topic_subtopic_pairs(measure)
    if (topic, subtopic) not in pairs:
        return None

    return {"topic": topic, "subtopic": subtopic}


# ── DB helpers ──────────────────────────────────────────────────────


def _is_empty(val) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and val.strip() == "":
        return True
    return False


def fetch_pending_questions(
    user_conn: sqlite3.Connection,
    limit: Optional[int],
) -> List[sqlite3.Row]:
    """Return live ``question`` rows whose taxonomy is incomplete.

    A row is "pending" if ANY of ``topic`` / ``subtopic`` /
    ``question_type`` is empty. We skip rows that already have all
    three populated, which is what makes resume-after-interrupt safe.
    """
    user_conn.row_factory = sqlite3.Row
    sql = (
        "SELECT id, measure, subtype, topic, subtopic, question_type, "
        "       prompt, explanation "
        "FROM question "
        "WHERE status='live' "
        "  AND ((topic IS NULL OR TRIM(topic)='')"
        "    OR (subtopic IS NULL OR TRIM(subtopic)='')"
        "    OR (question_type IS NULL OR TRIM(question_type)=''))"
        "ORDER BY id"
    )
    if limit is not None:
        sql += " LIMIT {}".format(int(limit))
    return list(user_conn.execute(sql))


def fetch_options(
    user_conn: sqlite3.Connection, qid: int
) -> List[Tuple[str, str, bool]]:
    """Return ``[(label, text, is_correct), ...]`` for a question."""
    rows = user_conn.execute(
        "SELECT option_label, option_text, is_correct "
        "FROM questionoption WHERE question_id=? "
        "ORDER BY option_label",
        (qid,),
    ).fetchall()
    out: List[Tuple[str, str, bool]] = []
    for r in rows:
        out.append((r[0] or "", r[1] or "", bool(r[2])))
    return out


def write_taxonomy_both_dbs(
    user_conn: sqlite3.Connection,
    seed_conn: sqlite3.Connection,
    qid: int,
    topic: str,
    subtopic: str,
    question_type: str,
) -> None:
    """Update ``topic``, ``subtopic``, ``question_type`` on a question
    in BOTH databases atomically.

    User DB is written via the same connection passed in; seed DB is
    written via its own connection. Each connection runs in its own
    implicit transaction; we commit both at the end. If the seed write
    raises we roll the user back to keep the two DBs in sync.
    """
    user_conn.execute(
        "UPDATE question SET topic=?, subtopic=?, question_type=?, "
        "                    updated_at=? "
        "WHERE id=?",
        (topic, subtopic, question_type,
         datetime.now().isoformat(timespec="seconds"), qid),
    )
    try:
        seed_conn.execute(
            "UPDATE question SET topic=?, subtopic=?, question_type=?, "
            "                    updated_at=? "
            "WHERE id=?",
            (topic, subtopic, question_type,
             datetime.now().isoformat(timespec="seconds"), qid),
        )
    except Exception:
        user_conn.rollback()
        raise
    user_conn.commit()
    seed_conn.commit()


# ── LLM-judge driver ────────────────────────────────────────────────


def judge_one(
    llm_service_obj,
    measure: str,
    subtype: str,
    qid: int,
    prompt_text: str,
    options: List[Tuple[str, str, bool]],
    explanation: str,
    model: Optional[str] = None,
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Ask the LLM to classify one question; retry once on invalid output.

    Returns ``(decision, error_reason)`` where exactly one of the two
    is non-None. ``decision`` is ``{"topic", "subtopic"}`` validated
    against the canonical taxonomy; ``error_reason`` describes why we
    couldn't classify.
    """
    taxonomy_text = taxonomy_summary_for_prompt(measure)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        measure=measure,
        subtype=subtype,
        taxonomy_text=taxonomy_text,
    )
    user_prompt = build_user_prompt(
        qid, prompt_text, options, explanation, measure, subtype
    )

    # First try.
    try:
        raw = llm_service_obj.generate_json(
            system_prompt, user_prompt, model=model
        )
    except Exception as exc:  # pylint: disable=broad-except
        # Fall through to a single retry on non-JSON / network-ish errors.
        raw = None
        first_err = "first_call_error: {}".format(exc)
    else:
        first_err = None

    decision = parse_judge_response(raw, measure)
    if decision is not None:
        return decision, None

    # Retry once with a stricter suffix.
    system_prompt_strict = system_prompt + RETRY_SUFFIX
    try:
        raw2 = llm_service_obj.generate_json(
            system_prompt_strict, user_prompt, model=model
        )
    except Exception as exc:  # pylint: disable=broad-except
        return None, "retry_call_error: {} (first={})".format(
            exc, first_err or "invalid_response"
        )

    decision = parse_judge_response(raw2, measure)
    if decision is not None:
        return decision, None

    return None, "invalid_after_retry"


# ── Main loop ───────────────────────────────────────────────────────


def derive_question_type(subtype: str, existing: Optional[str]) -> str:
    """Pick the canonical ``question_type`` for a subtype.

    If ``existing`` is already populated and matches the canonical
    label, keep it; otherwise rewrite to the canonical one. Unknown
    subtypes fall through to whatever was already there (or empty).
    """
    canonical = SUBTYPE_TO_QUESTION_TYPE.get(subtype or "")
    if canonical:
        return canonical
    if existing and existing.strip():
        return existing.strip()
    return ""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0]
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print decisions without writing to either DB.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N questions (default: all).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="OpenRouter model id override (default: configured llm model).",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Optional path to write a JSONL audit log of decisions.",
    )
    parser.add_argument(
        "--print-every",
        type=int,
        default=10,
        help="Progress print cadence (default: every 10 rows).",
    )
    args = parser.parse_args(argv)

    # Lazy import so unit tests that mock the LLM can avoid importing
    # the OpenAI client at all.
    from services.llm_service import llm_service

    user_conn = sqlite3.connect(str(DB_PATH))
    seed_conn = sqlite3.connect(str(SEED_DB_PATH))
    user_conn.row_factory = sqlite3.Row

    pending = fetch_pending_questions(user_conn, args.limit)
    total = len(pending)
    print(
        "[llm_judge_taxonomy] {} pending question(s) "
        "(dry_run={}, limit={}, model={})".format(
            total, args.dry_run, args.limit, args.model or "<configured>"
        )
    )
    if total == 0:
        user_conn.close()
        seed_conn.close()
        return 0

    log_fh = None
    if args.log:
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "a", encoding="utf-8")

    decided = 0
    written = 0
    rejected = 0
    skipped_unknown_measure = 0
    started_at = time.time()

    try:
        for idx, row in enumerate(pending, 1):
            qid = row["id"]
            measure = (row["measure"] or "").strip()
            subtype = (row["subtype"] or "").strip()
            prompt_text = row["prompt"] or ""
            explanation = row["explanation"] or ""

            if measure not in ("quant", "verbal", "awa"):
                skipped_unknown_measure += 1
                rec = {
                    "qid": qid,
                    "status": "skipped_unknown_measure",
                    "measure": measure,
                }
                if log_fh:
                    log_fh.write(json.dumps(rec) + "\n")
                continue

            # Fast-path: row already has a canonical (topic, subtopic) pair
            # but only ``question_type`` is missing. ``question_type`` is
            # deterministic from ``subtype`` so we don't need the LLM.
            existing_topic = (row["topic"] or "").strip()
            existing_subtopic = (row["subtopic"] or "").strip()
            existing_qtype = (row["question_type"] or "").strip()
            if (
                existing_topic
                and existing_subtopic
                and (existing_topic, existing_subtopic)
                in allowed_topic_subtopic_pairs(measure)
            ):
                # Topic + subtopic are good — just need question_type.
                qtype_only = derive_question_type(subtype, existing_qtype)
                if not qtype_only:
                    # Unknown subtype, no canonical mapping — fall through
                    # to the LLM branch (it won't help, but the user logs
                    # the rejection consistently).
                    pass
                else:
                    if args.dry_run:
                        print(
                            "[{}/{}] qid={} qtype-only fast-path → {}".format(
                                idx, total, qid, qtype_only
                            )
                        )
                        decided += 1
                        rec = {
                            "qid": qid,
                            "status": "decided_qtype_only",
                            "measure": measure,
                            "subtype": subtype,
                            "topic": existing_topic,
                            "subtopic": existing_subtopic,
                            "question_type": qtype_only,
                        }
                        if log_fh:
                            log_fh.write(json.dumps(rec) + "\n")
                        continue
                    try:
                        write_taxonomy_both_dbs(
                            user_conn, seed_conn, qid,
                            existing_topic, existing_subtopic, qtype_only,
                        )
                        decided += 1
                        written += 1
                        rec = {
                            "qid": qid,
                            "status": "decided_qtype_only",
                            "measure": measure,
                            "subtype": subtype,
                            "topic": existing_topic,
                            "subtopic": existing_subtopic,
                            "question_type": qtype_only,
                        }
                        if log_fh:
                            log_fh.write(json.dumps(rec) + "\n")
                    except Exception as exc:  # pylint: disable=broad-except
                        rejected += 1
                        rec = {
                            "qid": qid,
                            "status": "write_failed",
                            "error": str(exc),
                        }
                        if log_fh:
                            log_fh.write(json.dumps(rec) + "\n")
                        print(
                            "[{}/{}] qid={} WRITE FAILED: {}".format(
                                idx, total, qid, exc
                            )
                        )
                    continue

            options = fetch_options(user_conn, qid)

            decision, err = judge_one(
                llm_service,
                measure=measure,
                subtype=subtype,
                qid=qid,
                prompt_text=prompt_text,
                options=options,
                explanation=explanation,
                model=args.model,
            )

            if decision is None:
                rejected += 1
                rec = {
                    "qid": qid,
                    "status": "rejected",
                    "measure": measure,
                    "subtype": subtype,
                    "error": err,
                }
                if log_fh:
                    log_fh.write(json.dumps(rec) + "\n")
                if args.dry_run:
                    print(
                        "[{}/{}] qid={} REJECTED ({})".format(
                            idx, total, qid, err
                        )
                    )
                continue

            decided += 1
            qtype = derive_question_type(subtype, row["question_type"])

            rec = {
                "qid": qid,
                "status": "decided",
                "measure": measure,
                "subtype": subtype,
                "topic": decision["topic"],
                "subtopic": decision["subtopic"],
                "question_type": qtype,
                "previous": {
                    "topic": row["topic"],
                    "subtopic": row["subtopic"],
                    "question_type": row["question_type"],
                },
            }
            if log_fh:
                log_fh.write(json.dumps(rec) + "\n")

            if args.dry_run:
                print(
                    "[{}/{}] qid={} {} → topic={} subtopic={} qtype={}".format(
                        idx, total, qid, measure,
                        decision["topic"], decision["subtopic"], qtype,
                    )
                )
                continue

            try:
                write_taxonomy_both_dbs(
                    user_conn, seed_conn, qid,
                    decision["topic"], decision["subtopic"], qtype,
                )
                written += 1
            except Exception as exc:  # pylint: disable=broad-except
                rejected += 1
                rec = {
                    "qid": qid,
                    "status": "write_failed",
                    "error": str(exc),
                }
                if log_fh:
                    log_fh.write(json.dumps(rec) + "\n")
                print(
                    "[{}/{}] qid={} WRITE FAILED: {}".format(
                        idx, total, qid, exc
                    )
                )

            if idx % max(1, args.print_every) == 0:
                elapsed = time.time() - started_at
                rate = idx / elapsed if elapsed > 0 else 0.0
                remaining = total - idx
                eta = remaining / rate if rate > 0 else 0.0
                print(
                    "[{}/{}] decided={} written={} rejected={} "
                    "skipped={} ({:.2f}/s, eta {:.0f}s)".format(
                        idx, total, decided, written, rejected,
                        skipped_unknown_measure, rate, eta,
                    )
                )

    finally:
        user_conn.close()
        seed_conn.close()
        if log_fh:
            log_fh.close()

    elapsed = time.time() - started_at
    print(
        "[llm_judge_taxonomy] DONE — total={} decided={} written={} "
        "rejected={} skipped_unknown_measure={} ({:.1f}s)".format(
            total, decided, written, rejected,
            skipped_unknown_measure, elapsed,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
