"""Princeton subtopic backfill via Haiku.

All 991 `source='princeton_2012'` items in the consolidated DB shipped
with empty ``subtopic`` — this blocks the subtopic-drill feature. We
classify each item with Haiku (cheapest Claude model available through
the gateway) against the canonical taxonomy in ``models/taxonomy.py``.

Batching: up to 50 items per call. Response is structured JSON mapping
each qid to a subtopic id. Items Haiku routes to an out-of-vocabulary
subtopic are force-set to ``'unclassified'`` (the caller can SME-triage
those later).

Retry policy: per-batch retry up to 5 times with 2→5→10→20→40s backoff
on 429/502/503/504 or connection errors. Two timeouts → write the
batch to ``'unclassified'`` and continue.

Cache: ``data/extracted/princeton/subtopic_cache.json`` stores qid →
subtopic so re-runs are idempotent.

Usage:
  venv/bin/python scripts/backfill_princeton_subtopics.py
  venv/bin/python scripts/backfill_princeton_subtopics.py --batch-size 50 --limit 100
  venv/bin/python scripts/backfill_princeton_subtopics.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


CACHE_PATH = REPO / "data" / "extracted" / "princeton" / "subtopic_cache.json"
UNCLASSIFIED = "unclassified"


def load_allowlist() -> Dict[str, Dict[str, Any]]:
    """Return a dict mapping subtopic_id → {measure, topic, display_name}.

    Every caller-visible subtopic the taxonomy ships with is included,
    plus the sentinel ``unclassified``.
    """
    from models.taxonomy import QUANT_TAXONOMY, VERBAL_TAXONOMY
    out: Dict[str, Dict[str, Any]] = {}
    for topic, td in QUANT_TAXONOMY.items():
        for sub_id, sd in td["subtopics"].items():
            out[sub_id] = {
                "measure": "quant",
                "topic": topic,
                "display_name": sd["display_name"],
                "concepts": sd.get("concepts", []),
            }
    for topic, td in VERBAL_TAXONOMY.items():
        for sub_id, sd in td["subtopics"].items():
            out[sub_id] = {
                "measure": "verbal",
                "topic": topic,
                "display_name": sd["display_name"],
                "concepts": sd.get("concepts", []),
            }
    out[UNCLASSIFIED] = {
        "measure": "any",
        "topic": "unclassified",
        "display_name": "(unclassified)",
        "concepts": [],
    }
    return out


def build_prompt(batch: List[Dict[str, Any]], allowlist: Dict[str, Dict[str, Any]],
                 measure: str) -> str:
    """Build a single Haiku prompt for a batch restricted to one measure."""
    # Filter allowlist to the current measure so Haiku doesn't pick
    # "triangles" for a verbal RC item.
    measure_allow = {sid: meta for sid, meta in allowlist.items()
                     if meta["measure"] in (measure, "any")}
    catalog_lines: List[str] = []
    for sid, meta in sorted(measure_allow.items()):
        if sid == UNCLASSIFIED:
            continue
        concepts = ", ".join(meta["concepts"][:6]) if meta["concepts"] else ""
        catalog_lines.append(
            f"  - {sid}: {meta['display_name']}"
            + (f" ({concepts})" if concepts else "")
        )
    catalog = "\n".join(catalog_lines)

    items_lines: List[str] = []
    for it in batch:
        stem = (it["stem"] or "").strip().replace("\n", " ")
        if len(stem) > 500:
            stem = stem[:500] + "…"
        items_lines.append(
            f'  {{"qid": {it["qid"]}, "subtype": "{it["subtype"]}", '
            f'"stem": {json.dumps(stem)}}}'
        )
    items_block = ",\n".join(items_lines)

    valid_ids = sorted(sid for sid in measure_allow.keys()
                       if sid != UNCLASSIFIED)
    return f"""You are classifying GRE {measure.upper()} practice items against the canonical subtopic taxonomy.

The allowed subtopics for {measure} are:
{catalog}

Also allowed: "{UNCLASSIFIED}" (use ONLY when no subtopic fits).

You will be given a list of items. For EACH item, return the single
best-fitting subtopic id from the list above. Return ONLY a JSON array
of {{"qid": int, "subtopic": str}} objects — no preamble, no markdown
fences, no trailing prose.

Items:
[
{items_block}
]

Output a JSON array, one entry per item, same order. Each entry must
have exactly the keys "qid" (integer) and "subtopic" (one of:
{", ".join(valid_ids + [UNCLASSIFIED])})."""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    """Parse Haiku's JSON-array response tolerantly."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = [ln for ln in text.split("\n")
                 if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    # Find the outermost [...] or {...}.
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    # Fall back: try { "items": [...] } shape.
    start2 = text.find("{")
    end2 = text.rfind("}")
    if start2 >= 0 and end2 > start2:
        try:
            obj = json.loads(text[start2:end2 + 1])
            if isinstance(obj, dict):
                for k in ("items", "classifications", "results"):
                    if isinstance(obj.get(k), list):
                        return obj[k]
        except json.JSONDecodeError:
            pass
    return []


def load_cache() -> Dict[str, str]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_cache(cache: Dict[str, str]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _call_haiku_with_retry(client, model: str, system: str, user_msg: str,
                           max_retries: int = 5) -> str:
    delays = [2, 5, 10, 20, 40]
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return client.call_anthropic(
                model=model,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=4000,
                max_retries=1,  # gateway-level retry disabled; we manage
            )
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            is_retryable = any(code in msg for code in
                               ("403", "429", "502", "503", "504",
                                "timeout", "Connection"))
            if not is_retryable or attempt == max_retries - 1:
                break
            wait = delays[min(attempt, len(delays) - 1)]
            print(f"    [retry] {exc!r} — sleeping {wait}s", flush=True)
            time.sleep(wait)
    if last_exc:
        raise last_exc
    return ""


def classify_batch(client, batch: List[Dict[str, Any]],
                   allowlist: Dict[str, Dict[str, Any]],
                   measure: str) -> Dict[int, str]:
    """Classify one batch via Haiku; returns qid → subtopic."""
    # `_llm_gateway` is gitignored (carries an Apple-internal model id).
    # On any env that hasn't built the gateway — CI, a fresh clone — the
    # import would fail and drag the unit tests down with it. The tests
    # inject a fake client that ignores the model string, and production
    # callers only reach this function via the `__main__` path below
    # which imports and constructs a real FloodgateClient first.
    try:
        from services._llm_gateway import MODEL_HAIKU
    except ModuleNotFoundError:
        MODEL_HAIKU = "haiku"  # sentinel: never hits a real endpoint
    system = ("You are a GRE item classifier. Return strictly valid JSON; "
              "no markdown fences, no preamble.")
    user = build_prompt(batch, allowlist, measure)
    raw = _call_haiku_with_retry(client, MODEL_HAIKU, system, user)
    parsed = parse_response(raw)
    valid_ids = {sid for sid, meta in allowlist.items()
                 if meta["measure"] in (measure, "any")}
    result: Dict[int, str] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        try:
            qid = int(entry.get("qid"))
        except (TypeError, ValueError):
            continue
        sid = str(entry.get("subtopic", "") or "").strip()
        if sid not in valid_ids:
            sid = UNCLASSIFIED
        result[qid] = sid
    # Any item missing from Haiku's response → unclassified.
    for it in batch:
        result.setdefault(it["qid"], UNCLASSIFIED)
    return result


def fetch_princeton_empty_subtopic(db, Question) -> List[Dict[str, Any]]:
    rows = list(
        Question.select(Question.id, Question.measure, Question.subtype,
                        Question.prompt, Question.status,
                        Question.subtopic, Question.topic)
        .where((Question.source == "princeton_2012") &
               (Question.subtopic == ""))
        .order_by(Question.id)
    )
    return [
        {"qid": r.id, "measure": r.measure, "subtype": r.subtype,
         "stem": r.prompt, "status": r.status}
        for r in rows
    ]


def apply_subtopics(db, Question, updates: Dict[int, str],
                    allowlist: Dict[str, Dict[str, Any]]) -> int:
    from datetime import datetime as _dt
    updated = 0
    db.connect(reuse_if_open=True)
    with db.atomic() as txn:
        for qid, sid in updates.items():
            topic = allowlist.get(sid, {}).get("topic", "")
            Question.update(
                subtopic=sid,
                topic=topic,
                updated_at=_dt.now(),
            ).where(Question.id == qid).execute()
            updated += 1
        txn.commit()
    return updated


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--batch-size", type=int, default=25,
                   help="items per Haiku call (default 25)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap total items (smoke testing)")
    p.add_argument("--dry-run", action="store_true",
                   help="compute assignments but don't mutate DB")
    p.add_argument("--force", action="store_true",
                   help="ignore cache and re-call Haiku")
    p.add_argument("--report",
                   default=str(REPO / "data" / "audits"
                               / "princeton_subtopic_backfill_2026_04_28.md"))
    args = p.parse_args(argv)

    from models.database import db, Question
    from services._llm_gateway import FloodgateClient

    allowlist = load_allowlist()
    cache = load_cache() if not args.force else {}

    rows = fetch_princeton_empty_subtopic(db, Question)
    if args.limit:
        rows = rows[:args.limit]
    total = len(rows)
    if total == 0:
        print("[subtopic] nothing to classify", flush=True)
        return 0
    print(f"[subtopic] {total} Princeton items with empty subtopic",
          flush=True)

    client = FloodgateClient()

    # Split by measure so the prompt only exposes relevant subtopics.
    by_measure: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_measure[r["measure"]].append(r)

    all_results: Dict[int, str] = {}
    cache_hits = 0
    haiku_calls = 0
    started = time.time()
    last_progress = time.time()

    for measure in sorted(by_measure.keys()):
        items = by_measure[measure]
        print(f"[subtopic] {measure}: {len(items)} items", flush=True)
        pending: List[Dict[str, Any]] = []
        for it in items:
            cached = cache.get(str(it["qid"]))
            if cached and cached in allowlist:
                all_results[it["qid"]] = cached
                cache_hits += 1
            else:
                pending.append(it)

        batch_size = max(1, args.batch_size)
        n_batches = (len(pending) + batch_size - 1) // batch_size
        for b in range(n_batches):
            lo = b * batch_size
            hi = min(lo + batch_size, len(pending))
            batch = pending[lo:hi]
            t0 = time.time()
            try:
                batch_result = classify_batch(client, batch, allowlist,
                                              measure)
            except Exception as exc:
                print(f"  [batch {b+1}/{n_batches}] {measure} FAILED: "
                      f"{exc!r} — routing to unclassified",
                      flush=True)
                batch_result = {it["qid"]: UNCLASSIFIED for it in batch}
            dt = time.time() - t0
            haiku_calls += 1
            for qid, sid in batch_result.items():
                all_results[qid] = sid
                cache[str(qid)] = sid
            print(f"  [batch {b+1}/{n_batches}] {measure} "
                  f"{len(batch)} items -> "
                  f"{sum(1 for v in batch_result.values() if v == UNCLASSIFIED)} unclassified "
                  f"(dt={dt:.1f}s)",
                  flush=True)
            save_cache(cache)
            # Force progress print every ~5min.
            if time.time() - last_progress > 300:
                done = len(all_results)
                print(f"[subtopic] progress: {done}/{total} "
                      f"({100*done/max(1,total):.0f}%)",
                      flush=True)
                last_progress = time.time()

    # Histogram.
    hist = Counter(all_results.values())
    unclassified_count = hist.get(UNCLASSIFIED, 0)
    unclassified_pct = 100 * unclassified_count / max(1, total)

    # Hard stop if too many unclassified.
    if unclassified_pct > 30 and not args.dry_run:
        print(f"[subtopic] STOP CONDITION: {unclassified_pct:.1f}% items "
              f"routed to '{UNCLASSIFIED}' (> 30% threshold). "
              f"Skipping DB mutation. Review the taxonomy gap.",
              flush=True)
        _write_report(args.report, rows, all_results, cache_hits,
                      haiku_calls, time.time() - started, allowlist,
                      stopped=True, unclassified_pct=unclassified_pct)
        return 2

    # Apply to DB.
    if not args.dry_run:
        updated = apply_subtopics(db, Question, all_results, allowlist)
        print(f"[subtopic] DB mutation applied: {updated} items updated",
              flush=True)
    else:
        print("[subtopic] DRY RUN — no DB mutation", flush=True)

    _write_report(args.report, rows, all_results, cache_hits,
                  haiku_calls, time.time() - started, allowlist,
                  stopped=False, unclassified_pct=unclassified_pct)
    print(f"[subtopic] done. Cache hits: {cache_hits}. Haiku calls: "
          f"{haiku_calls}. Wall: {time.time() - started:.0f}s",
          flush=True)
    return 0


def _write_report(path: str, rows: List[Dict[str, Any]],
                  all_results: Dict[int, str],
                  cache_hits: int, haiku_calls: int, wall_s: float,
                  allowlist: Dict[str, Dict[str, Any]], *,
                  stopped: bool, unclassified_pct: float) -> None:
    hist = Counter(all_results.values())
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    lines.append("# Princeton subtopic backfill — 2026-04-28")
    lines.append("")
    lines.append(f"- Timestamp: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- Princeton items with empty subtopic: **{len(rows)}**")
    lines.append(f"- Classified: **{len(all_results)}**")
    lines.append(f"- Cache hits: **{cache_hits}**")
    lines.append(f"- Haiku calls: **{haiku_calls}**")
    lines.append(f"- Wall time: **{wall_s:.0f}s**")
    lines.append(f"- Unclassified: **{hist.get(UNCLASSIFIED, 0)}** "
                 f"({unclassified_pct:.1f}%)")
    if stopped:
        lines.append("- **STOPPED — unclassified rate exceeded 30%. "
                     "No DB mutation applied.**")
    lines.append("")
    lines.append("## Subtopic histogram")
    lines.append("")
    lines.append("| subtopic | measure | count |")
    lines.append("|---|---|---|")
    for sid, n in hist.most_common():
        meta = allowlist.get(sid, {})
        lines.append(f"| {sid} | {meta.get('measure','?')} | {n} |")
    lines.append("")
    lines.append("## Spot-check (30 random assignments)")
    lines.append("")
    import random
    rand = random.Random(42)
    sample = rand.sample(rows, min(30, len(rows)))
    for it in sample:
        sid = all_results.get(it["qid"], "?")
        stem = (it["stem"] or "").strip().replace("\n", " ")
        if len(stem) > 160:
            stem = stem[:160] + "…"
        lines.append(f"- qid={it['qid']} [{it['measure']}/{it['subtype']}] "
                     f"→ **{sid}**")
        lines.append(f"    > {stem}")
    report_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
