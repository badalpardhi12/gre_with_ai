"""Generate audit markdown from visual_sweep_cache.json.

Run after (or during) scripts/visual_sweep.py. Produces
data/audits/visual_sweep_2026_04_28.md with:

- per-issue histogram (counts across Sonnet + Opus)
- action breakdown (keep_live / log_only / demote)
- per-source rates
- top-N examples per issue (qid + Sonnet reasoning + source + action)

Usage:
    venv/bin/python scripts/visual_sweep_report.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CACHE = ROOT / "data" / "extracted" / "visual_sweep_cache.json"
OUT = ROOT / "data" / "audits" / "visual_sweep_2026_04_28.md"
TOP_N = 20


def main() -> int:
    if not CACHE.exists():
        print(f"missing cache: {CACHE}", file=sys.stderr)
        return 1
    with open(CACHE) as f:
        cache = json.load(f)

    # Aggregate
    n_total = len(cache)
    action_counts: Counter = Counter()
    issue_counts: Counter = Counter()
    sonnet_issue_counts: Counter = Counter()
    opus_issue_counts: Counter = Counter()
    per_source_actions: dict[str, Counter] = defaultdict(Counter)
    per_source_total: Counter = Counter()
    issue_examples: dict[str, list[dict]] = defaultdict(list)
    opus_calls = 0
    sonnet_calls = 0
    parse_failures = 0

    for qid_str, v in cache.items():
        qid = int(qid_str)
        action = v.get("action", "keep_live")
        src = v.get("source", "?")
        per_source_total[src] += 1
        action_counts[action] += 1
        per_source_actions[src][action] += 1
        sonnet_calls += 1
        if v.get("opus") is not None:
            opus_calls += 1
        sonnet = v.get("sonnet") or {}
        if sonnet.get("_parse_failed"):
            parse_failures += 1
        for iss in sonnet.get("issues", []) or []:
            sonnet_issue_counts[iss] += 1
            issue_counts[iss] += 1
        opus = v.get("opus") or {}
        if opus:
            for iss in opus.get("issues", []) or []:
                opus_issue_counts[iss] += 1
                issue_counts[iss] += 1

        # Collect examples keyed by Sonnet issues (primary judge).
        # Prefer items where action != keep_live (i.e. Sonnet flagged).
        if sonnet.get("issues") and not sonnet.get("coherent", True):
            for iss in sonnet["issues"]:
                issue_examples[iss].append({
                    "qid": qid,
                    "source": src,
                    "subtype": v.get("subtype"),
                    "action": action,
                    "sonnet_conf": sonnet.get("confidence"),
                    "sonnet_reasoning": sonnet.get("reasoning", ""),
                    "opus_agree": bool(opus and not opus.get("coherent", True)
                                       and iss in (opus.get("issues") or [])),
                })

    OUT.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# Visual Sweep Audit — 2026-04-28")
    lines.append("")
    lines.append(f"_Generated {datetime.utcnow().isoformat(timespec='seconds')}Z_")
    lines.append("")
    lines.append(
        "Content-based audit of the live question pool. Each item was sent "
        "to Sonnet 4.6 vision as (stem + stimulus + options + attached "
        "figure). Items that Sonnet flagged as not-coherent OR "
        "low-confidence were escalated to Opus 4.7 as a second judge. "
        "live→draft demotion required both judges to agree at high "
        "confidence on a structural issue."
    )
    lines.append("")
    lines.append("## Pipeline totals")
    lines.append("")
    lines.append(f"- Items audited: **{n_total}**")
    lines.append(f"- Sonnet calls: {sonnet_calls}")
    lines.append(f"- Opus escalations: {opus_calls}")
    lines.append(f"- Parse failures: {parse_failures}")
    lines.append("")
    lines.append("## Action breakdown")
    lines.append("")
    lines.append("| action | count | pct |")
    lines.append("|---|---:|---:|")
    for act, n in action_counts.most_common():
        pct = 100.0 * n / max(n_total, 1)
        lines.append(f"| {act} | {n} | {pct:.1f}% |")
    lines.append("")

    lines.append("## Issue histogram (union of Sonnet + Opus tags)")
    lines.append("")
    lines.append("| issue | count | sonnet | opus |")
    lines.append("|---|---:|---:|---:|")
    for iss, n in issue_counts.most_common():
        lines.append(f"| {iss} | {n} | {sonnet_issue_counts[iss]} | "
                     f"{opus_issue_counts[iss]} |")
    lines.append("")

    lines.append("## Per-source rates")
    lines.append("")
    lines.append("| source | total | kept | logged | demoted | demote rate |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for src, total in sorted(per_source_total.items(), key=lambda kv: -kv[1]):
        acts = per_source_actions[src]
        kept = acts.get("keep_live", 0)
        logged = acts.get("log_only", 0)
        demoted = acts.get("demote", 0)
        rate = 100.0 * demoted / max(total, 1)
        lines.append(f"| {src} | {total} | {kept} | {logged} | {demoted} | "
                     f"{rate:.1f}% |")
    lines.append("")

    lines.append("## Top samples per issue")
    lines.append("")
    for iss, n in issue_counts.most_common():
        examples = issue_examples.get(iss) or []
        # Sort: demoted first, then opus-agree, then by qid
        examples.sort(key=lambda e: (
            0 if e["action"] == "demote" else 1,
            0 if e["opus_agree"] else 1,
            e["qid"],
        ))
        lines.append(f"### `{iss}` (total tagged: {n})")
        lines.append("")
        if not examples:
            lines.append("_(no flagged samples for this tag — Sonnet voted coherent)_")
            lines.append("")
            continue
        for e in examples[:TOP_N]:
            agree = " [opus agrees]" if e["opus_agree"] else ""
            lines.append(
                f"- **qid {e['qid']}** ({e['source']} / {e['subtype']}) "
                f"— action=`{e['action']}` conf={e['sonnet_conf']}{agree}"
            )
            r = e.get("sonnet_reasoning", "").strip().replace("\n", " ")
            if r:
                lines.append(f"  - {r}")
        lines.append("")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT} ({n_total} items, {sum(action_counts.values())} actions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
