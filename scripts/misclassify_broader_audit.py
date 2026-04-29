"""
Broader misclassification audit — 2026-04-28.

Classifies every live question in the bank by content using Opus 4.7 via the
Floodgate gateway, with a deterministic pre-filter to skip obviously correct
items. The audit runs against the union of live ids in gre_user.db and
gre_mock.db so that fixes can be propagated to both.

Usage:
    venv/bin/python scripts/misclassify_broader_audit.py --dry-run
    venv/bin/python scripts/misclassify_broader_audit.py --apply
    venv/bin/python scripts/misclassify_broader_audit.py --apply --limit 25 --batch-start 0

Batches of 25 items are committed to the audit log immediately so that a crash
or external kill does not lose progress. The final apply step runs at the end
on the aggregated decisions.
"""
import argparse
import json
import os
import random
import signal
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services._llm_gateway import FloodgateClient, MODEL_OPUS

REPO_ROOT = Path(__file__).resolve().parents[1]
USER_DB = REPO_ROOT / "data" / "gre_user.db"
MOCK_DB = REPO_ROOT / "data" / "gre_mock.db"
AUDIT_DIR = REPO_ROOT / "data" / "audits"
AUDIT_MD = AUDIT_DIR / "misclassify_audit_2026_04_28.md"
DECISIONS_JSONL = AUDIT_DIR / "misclassify_audit_2026_04_28.decisions.jsonl"

VALID_MEASURES = {"quant", "verbal"}
VALID_SUBTYPES = {
    "mcq_single", "mcq_multi", "qc", "numeric_entry", "data_interp",
    "tc", "se", "rc_single", "rc_multi", "rc_select_passage",
}

SYSTEM_PROMPT = """You classify GRE questions by content. You receive a question prompt, any stimulus excerpt, and the options. You output a JSON object with fields:
- correct_measure: "quant" or "verbal"
- correct_subtype: one of "mcq_single" | "mcq_multi" | "qc" | "numeric_entry" | "data_interp" | "tc" | "se" | "rc_single" | "rc_multi" | "rc_select_passage"
- confidence: "high" | "medium" | "low"
- reasoning: one brief sentence

Signals for quant: algebraic variables, equations, inequalities, numeric expressions, geometric figures, data interpretation from graphs/tables, mathematical operators (|x|, sqrt, fractions), numeric answer choices.
Signals for verbal: prose sentences with one or more blanks to fill with words, reading comprehension of a passage, vocabulary, grammar.

Subtype signals:
- qc: the stem compares "Quantity A" and "Quantity B"
- numeric_entry: no answer choices; a single number is entered
- data_interp: a graph/table stimulus with numeric data, multiple-choice
- tc: 1-3 blanks in a sentence; verbal vocabulary fill
- se: a single blank with 6 options; verbal sentence equivalence (pick 2)
- rc_single: reading passage, pick one answer
- rc_multi: reading passage OR "indicate all that apply" with 3 choices
- rc_select_passage: pick a sentence from a passage
- mcq_single: quantitative single-answer multi-choice that is not QC/NE/DI
- mcq_multi: quantitative multi-answer "indicate all that apply" that is not an RC passage item

Return ONLY the JSON object — no markdown fences, no commentary."""


def connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def fetch_union_live_items():
    """Fetch live items from both DBs, preferring user.db rows when present."""
    user = connect(USER_DB)
    mock = connect(MOCK_DB)

    # live ids in either DB
    user_live = {r["id"] for r in user.execute("SELECT id FROM question WHERE status='live'")}
    mock_live = {r["id"] for r in mock.execute("SELECT id FROM question WHERE status='live'")}
    all_ids = sorted(user_live | mock_live)

    items = []
    for qid in all_ids:
        row = None
        conn_used = None
        if qid in user_live:
            row = user.execute(
                "SELECT q.id, q.measure, q.subtype, q.status, q.source, q.prompt, q.stimulus_id "
                "FROM question q WHERE q.id=?", (qid,)
            ).fetchone()
            conn_used = user
        else:
            row = mock.execute(
                "SELECT q.id, q.measure, q.subtype, q.status, q.source, q.prompt, q.stimulus_id "
                "FROM question q WHERE q.id=?", (qid,)
            ).fetchone()
            conn_used = mock
        if not row:
            continue

        stim_type = None
        stim_content = None
        if row["stimulus_id"]:
            stim = conn_used.execute(
                "SELECT stimulus_type, content FROM stimulus WHERE id=?",
                (row["stimulus_id"],),
            ).fetchone()
            if stim:
                stim_type = stim["stimulus_type"]
                stim_content = (stim["content"] or "")[:300]

        opts = list(conn_used.execute(
            "SELECT option_label, option_text, is_correct FROM questionoption "
            "WHERE question_id=? ORDER BY option_label", (qid,)
        ))
        items.append({
            "id": qid,
            "measure": row["measure"],
            "subtype": row["subtype"],
            "source": row["source"],
            "prompt": row["prompt"] or "",
            "stimulus_id": row["stimulus_id"],
            "stimulus_type": stim_type,
            "stimulus_excerpt": stim_content,
            "options": [
                {"label": o["option_label"], "text": o["option_text"], "is_correct": bool(o["is_correct"])}
                for o in opts
            ],
        })
    user.close()
    mock.close()
    return items


def deterministic_skip(item, sample_rate=0.05):
    """Return (skip:bool, reason:str). If skip and sample hit, return False to audit anyway."""
    sub = item["subtype"]
    meas = item["measure"]
    prompt = item["prompt"] or ""
    stim_type = item["stimulus_type"]
    n_opts = len(item["options"])

    # Obvious awa measure is always wrong if it has a quant-style subtype
    if meas == "awa" and sub in ("mcq_single", "numeric_entry", "qc", "mcq_multi"):
        return False, "awa-mislabel"

    # passage + rc_* → usually correct; sample 5% for audit
    if stim_type == "passage" and sub.startswith("rc_"):
        if random.random() < sample_rate:
            return False, "rc-sample"
        return True, "rc-passage-skip"

    # qc with Quantity markers
    if sub == "qc" and "Quantity A" in prompt and "Quantity B" in prompt:
        if random.random() < sample_rate:
            return False, "qc-sample"
        return True, "qc-marker-skip"

    # se with exactly 6 options and measure=verbal
    if sub == "se" and n_opts == 6 and meas == "verbal":
        if random.random() < sample_rate:
            return False, "se-sample"
        return True, "se-6opts-skip"

    return False, "needs-audit"


def build_user_prompt(item):
    opts_text = "\n".join(
        f"  {o['label']}. {(o['text'] or '')[:160]}" + (" [correct]" if o["is_correct"] else "")
        for o in item["options"]
    ) or "  (no options)"
    stim_line = ""
    if item["stimulus_excerpt"]:
        stim_line = f"\nStimulus ({item['stimulus_type']}): {item['stimulus_excerpt']}"
    return f"""Current measure: {item['measure']}
Current subtype: {item['subtype']}
Prompt: {(item['prompt'] or '')[:800]}{stim_line}
Options ({len(item['options'])}):
{opts_text}

Classify. Return JSON only."""


def classify_with_opus(client, item, attempt_timeout=60):
    """Call Opus 4.7 with one retry on failure. Returns decision dict or None."""
    user_prompt = build_user_prompt(item)
    for attempt in range(2):
        try:
            raw = client.call_anthropic(
                model=MODEL_OPUS,
                messages=[{"role": "user", "content": user_prompt}],
                system=SYSTEM_PROMPT,
                max_tokens=300,
                max_retries=5,  # SDK-level 429/5xx retries
            )
            text = raw.strip()
            if text.startswith("```"):
                lines = [ln for ln in text.split("\n") if not ln.strip().startswith("```")]
                text = "\n".join(lines)
            data = json.loads(text)
            meas = data.get("correct_measure")
            sub = data.get("correct_subtype")
            conf = data.get("confidence", "low")
            reasoning = (data.get("reasoning") or "").replace("\n", " ").strip()
            if meas not in VALID_MEASURES or sub not in VALID_SUBTYPES:
                if attempt == 0:
                    continue
                return None
            if conf not in ("high", "medium", "low"):
                conf = "low"
            return {
                "measure": meas,
                "subtype": sub,
                "confidence": conf,
                "reasoning": reasoning[:300],
            }
        except Exception as exc:
            if attempt == 0:
                time.sleep(3)
                continue
            return {"measure": None, "subtype": None, "confidence": "low",
                    "reasoning": f"classifier error: {type(exc).__name__}: {exc}"[:300]}
    return None


def apply_to_both_dbs(qid, new_measure, new_subtype):
    """UPDATE both DBs if the row exists there. Preserves status unchanged."""
    updated = []
    for db_path in (USER_DB, MOCK_DB):
        conn = connect(db_path)
        # Verify row exists and status unchanged from SELECT before update
        row = conn.execute("SELECT measure, subtype, status FROM question WHERE id=?", (qid,)).fetchone()
        if not row:
            conn.close()
            continue
        old_status = row["status"]
        conn.execute(
            "UPDATE question SET measure=?, subtype=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_measure, new_subtype, qid),
        )
        # Sanity: status must not have changed
        after = conn.execute("SELECT status FROM question WHERE id=?", (qid,)).fetchone()
        if after["status"] != old_status:
            conn.rollback()
            conn.close()
            raise RuntimeError(f"qid={qid} status changed unexpectedly during reclassify")
        conn.commit()
        conn.close()
        updated.append(db_path.name)
    return updated


def retire_in_both_dbs(qid):
    updated = []
    for db_path in (USER_DB, MOCK_DB):
        conn = connect(db_path)
        row = conn.execute("SELECT status FROM question WHERE id=?", (qid,)).fetchone()
        if not row:
            conn.close()
            continue
        conn.execute(
            "UPDATE question SET status='retired', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (qid,),
        )
        conn.commit()
        conn.close()
        updated.append(db_path.name)
    return updated


def write_audit_md(decisions, skip_counts, counters):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Broader misclassification audit — 2026-04-28",
        "",
        "Classifier: anthropic.claude-opus-4-7 via Floodgate. Pre-filter skips obviously ",
        "correct items (rc_* with passage stimulus, qc with Quantity markers, se with 6 ",
        "options) with a 5% sample audit. Fixes apply to both gre_user.db and gre_mock.db.",
        "",
        "## Summary",
        "",
        f"- Live items scanned: {counters['scanned']}",
        f"- Pre-filter skipped: {counters['skipped']} (sampled {counters['sampled']} for audit)",
        f"- Classified via Opus: {counters['classified']}",
        f"- Reclassified (high confidence): {counters['applied']}",
        f"- Flagged for review (medium confidence): {counters['flagged']}",
        f"- Retired (low confidence / classifier error): {counters['retired']}",
        f"- Agreed with current classification: {counters['agreed']}",
        "",
        "## Pre-filter skip breakdown",
        "",
    ]
    for reason, n in sorted(skip_counts.items()):
        lines.append(f"- `{reason}`: {n}")
    lines.append("")
    lines.append("## Subtype transitions (old → new)")
    lines.append("")
    lines.append("| old_measure.old_subtype | new_measure.new_subtype | count |")
    lines.append("|---|---|---|")
    trans = Counter()
    for d in decisions:
        if d["action"] != "applied":
            continue
        trans[((d["old_measure"], d["old_subtype"]), (d["new_measure"], d["new_subtype"]))] += 1
    for (old, new), n in sorted(trans.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {old[0]}.{old[1]} | {new[0]}.{new[1]} | {n} |")
    lines.append("")
    lines.append("## Per-source counts (applied)")
    lines.append("")
    per_source = Counter()
    for d in decisions:
        if d["action"] == "applied":
            per_source[d["source"]] += 1
    lines.append("| source | reclassified |")
    lines.append("|---|---|")
    for source, n in sorted(per_source.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {source} | {n} |")
    lines.append("")
    lines.append("## Detailed decisions")
    lines.append("")
    lines.append("| qid | source | old_measure | old_subtype | new_measure | new_subtype | confidence | action | reasoning |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for d in sorted(decisions, key=lambda x: x["qid"]):
        reasoning = (d.get("reasoning") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {d['qid']} | {d['source']} | {d['old_measure']} | {d['old_subtype']} | "
            f"{d.get('new_measure') or ''} | {d.get('new_subtype') or ''} | "
            f"{d.get('confidence') or ''} | {d['action']} | {reasoning} |"
        )
    AUDIT_MD.write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write to DBs")
    ap.add_argument("--dry-run", action="store_true", help="no DB writes")
    ap.add_argument("--limit", type=int, default=0, help="max items to audit (0=all)")
    ap.add_argument("--batch-start", type=int, default=0)
    ap.add_argument("--sample-rate", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=1728)
    args = ap.parse_args()

    random.seed(args.seed)

    if not args.apply and not args.dry_run:
        print("Pass --dry-run or --apply.")
        sys.exit(2)

    print("[+] Fetching live items from both DBs...", flush=True)
    items = fetch_union_live_items()
    print(f"[+] {len(items)} unique live items across gre_user.db + gre_mock.db", flush=True)

    skip_counts = Counter()
    to_classify = []
    sampled_from_skip = set()
    for item in items:
        skip, reason = deterministic_skip(item, sample_rate=args.sample_rate)
        if skip:
            skip_counts[reason] += 1
        else:
            skip_counts.setdefault("audited", 0)
            if reason.endswith("-sample"):
                sampled_from_skip.add(item["id"])
            to_classify.append(item)
    print(f"[+] Pre-filter: {len(items) - len(to_classify)} skipped, {len(to_classify)} need Opus", flush=True)
    print(f"[+] Sampled from skip pool: {len(sampled_from_skip)}", flush=True)

    if args.limit > 0:
        to_classify = to_classify[args.batch_start:args.batch_start + args.limit]
        print(f"[+] Windowed to {len(to_classify)} items", flush=True)

    DECISIONS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    # Resume: read any already-decided qids from the jsonl.
    decided = {}
    if DECISIONS_JSONL.exists():
        with open(DECISIONS_JSONL) as fp:
            for ln in fp:
                try:
                    rec = json.loads(ln)
                    decided[rec["qid"]] = rec
                except Exception:
                    pass
    print(f"[+] Loaded {len(decided)} prior decisions from {DECISIONS_JSONL.name}", flush=True)

    client = FloodgateClient()

    decisions = list(decided.values())
    batch_size = 25
    last_progress = time.time()
    counters = Counter({
        "scanned": len(items),
        "skipped": len(items) - len(to_classify) - len(sampled_from_skip),
        "sampled": len(sampled_from_skip),
        "classified": 0,
        "applied": 0, "flagged": 0, "retired": 0, "agreed": 0,
    })

    def flush_progress(force=False):
        nonlocal last_progress
        now = time.time()
        if force or now - last_progress > 30:
            print(f"    progress: {counters['classified']} classified, "
                  f"{counters['applied']} applied, {counters['flagged']} flagged, "
                  f"{counters['retired']} retired, {counters['agreed']} agreed",
                  flush=True)
            last_progress = now

    pending_to_audit = [it for it in to_classify if it["id"] not in decided]
    print(f"[+] {len(pending_to_audit)} items to classify (after resume)", flush=True)

    out_fp = open(DECISIONS_JSONL, "a")
    try:
        for i, item in enumerate(pending_to_audit):
            t0 = time.time()
            result = classify_with_opus(client, item)
            elapsed = time.time() - t0
            counters["classified"] += 1

            dec = {
                "qid": item["id"],
                "source": item["source"],
                "old_measure": item["measure"],
                "old_subtype": item["subtype"],
                "elapsed_s": round(elapsed, 2),
            }
            if not result or result.get("measure") is None:
                dec.update({
                    "action": "retired",
                    "new_measure": None, "new_subtype": None,
                    "confidence": "low",
                    "reasoning": (result or {}).get("reasoning", "no result") if result else "no result",
                })
                counters["retired"] += 1
            else:
                dec.update({
                    "new_measure": result["measure"],
                    "new_subtype": result["subtype"],
                    "confidence": result["confidence"],
                    "reasoning": result["reasoning"],
                })
                same = (result["measure"] == item["measure"] and
                        result["subtype"] == item["subtype"])
                if same:
                    dec["action"] = "agreed"
                    counters["agreed"] += 1
                elif result["confidence"] == "high":
                    dec["action"] = "applied"
                    counters["applied"] += 1
                elif result["confidence"] == "medium":
                    dec["action"] = "flagged"
                    counters["flagged"] += 1
                else:
                    dec["action"] = "retired"
                    counters["retired"] += 1

            out_fp.write(json.dumps(dec) + "\n")
            out_fp.flush()
            decisions.append(dec)

            if (i + 1) % batch_size == 0:
                flush_progress(force=True)
            else:
                flush_progress()

        out_fp.close()
    except KeyboardInterrupt:
        print("[!] interrupted", flush=True)
        out_fp.close()
        raise

    # Stop condition: > 10% flipped
    flip_rate = counters["applied"] / max(1, counters["classified"])
    if flip_rate > 0.10:
        print(f"[!] STOP CONDITION: flip rate {flip_rate:.2%} > 10%. "
              f"Not applying DB changes. Surface for review.", flush=True)
        write_audit_md(decisions, skip_counts, counters)
        return 3

    # Apply DB updates for "applied" decisions + retire "retired" ones
    if args.apply:
        print(f"[+] Applying {counters['applied']} reclassifications + {counters['retired']} retirements...", flush=True)
        for d in decisions:
            if d["action"] == "applied":
                apply_to_both_dbs(d["qid"], d["new_measure"], d["new_subtype"])
            elif d["action"] == "retired":
                retire_in_both_dbs(d["qid"])
        print("[+] DB updates complete.", flush=True)
    else:
        print("[+] Dry run — DB untouched.", flush=True)

    write_audit_md(decisions, skip_counts, counters)
    print(f"[+] Wrote audit to {AUDIT_MD}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
