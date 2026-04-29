"""
CLI: run the figure-mispair audit against live questions in the bank.

For each live question whose stimulus contains an embedded image
(data:image/...), call Opus 4.7 + Sonnet 4.6 vision judges and ask
whether the image plausibly belongs to the stem.

Processing
----------
* Batches of 10 questions.
* Per-batch progress print (flushed), git commit after each batch.
* Per-judge 60s timeout + one retry (handled in figure_mispair_audit).
* 2-way parallel judges per question (module default).
* Idempotent: per-(qid,sid) cache at
  data/extracted/mispair_audit_cache.json. Re-runs skip cached items.

Confirmed mispairings (both judges: matches=false @ high) are routed
to status='draft' with a review_notes entry. The stimulus row is NOT
deleted — it may be valid for some other item.

Usage
-----
    venv/bin/python scripts/audit_figure_mispairs.py [--db PATH]
                                                    [--limit N]
                                                    [--batch-size N]
                                                    [--dry-run]
                                                    [--cache PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.figure_mispair_audit import (
    MispairVerdict,
    audit_pair,
    extract_first_image,
)

import base64


# The Floodgate gateway module (services._llm_gateway) lives only in
# the main repo, not in the per-agent worktree's `services/`. We import
# it lazily inside the judge factory so unit tests can stub the factory
# without triggering a real auth handshake.
_MAIN_REPO = os.environ.get(
    "GRE_MAIN_REPO",
    "/Users/chiku/Documents/side_projects/gre_with_ai",
)


def _load_gateway():
    if _MAIN_REPO not in sys.path:
        sys.path.append(_MAIN_REPO)
    # The worktree has a local `services/` which shadows the main repo's
    # `services/`. Reach in explicitly by absolute path so we resolve the
    # main-repo gateway even when running from the worktree.
    import importlib.util
    gw_path = Path(_MAIN_REPO) / "services" / "_llm_gateway.py"
    spec = importlib.util.spec_from_file_location(
        "_figure_mispair_gateway", str(gw_path),
    )
    gw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gw)  # type: ignore[union-attr]
    return gw


DEFAULT_CACHE_PATH = _PROJECT_ROOT / "data" / "extracted" / "mispair_audit_cache.json"


# ── Judge factories ──────────────────────────────────────────────────

def _make_anthropic_judge(model_id: str):
    gw = _load_gateway()
    client = gw.get_client()

    def _call(system: str, user: str, image_bytes: bytes, media_type: str) -> str:
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
            },
        }
        # max_retries=5 handles the 403/504/502/503/429 backoff ladder.
        return client.call_anthropic(
            model=model_id,
            system=system,
            messages=[{
                "role": "user",
                "content": [
                    image_block,
                    {"type": "text", "text": user},
                ],
            }],
            max_tokens=600,
            max_retries=5,
        )
    return _call


# ── DB helpers ───────────────────────────────────────────────────────

def fetch_audit_candidates(db_path: str) -> List[Dict[str, Any]]:
    """Return every live question whose stimulus carries an image."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT q.id, q.subtype, q.source, q.prompt, q.status, q.review_notes,
               s.id AS sid, s.stimulus_type, s.title AS stimulus_title,
               s.content AS stimulus_content
        FROM question q
        JOIN stimulus s ON q.stimulus_id = s.id
        WHERE q.status = 'live'
          AND s.stimulus_type IN ('graph', 'table', 'chart', 'diagram', 'figure')
          AND (s.content LIKE '%data:image/%' OR s.content LIKE '%<img%')
        ORDER BY q.id
        """
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


def mark_mispair_as_draft(
    db_path: str,
    question_id: int,
    verdict: MispairVerdict,
    *,
    dry_run: bool,
) -> None:
    """Flip a confirmed-mispair question to draft + append review_notes."""
    if dry_run:
        return
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT status, review_notes FROM question WHERE id = ?",
        (question_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return
    prev_status, prev_notes = row
    note_lines = [
        "[figure-mispair-audit]",
        f"  both Opus 4.7 + Sonnet 4.6 vision judges returned "
        f"matches=false @ confidence=high",
    ]
    for j in verdict.judgments:
        suspicious = (", " + ", ".join(j.suspicious)) if j.suspicious else ""
        note_lines.append(
            f"  {j.judge}: {j.reasoning}{suspicious}"
        )
    appended = "\n".join(note_lines)
    new_notes = (prev_notes.strip() + "\n\n" + appended) if prev_notes else appended
    cur.execute(
        "UPDATE question SET status = 'draft', review_notes = ? WHERE id = ?",
        (new_notes, question_id),
    )
    conn.commit()
    conn.close()


# ── Cache ────────────────────────────────────────────────────────────

def load_cache(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(path: Path, cache: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    tmp.replace(path)


def cache_key(question_id: int, stimulus_id: int) -> str:
    return f"{question_id}:{stimulus_id}"


# ── Git commit helper (per-batch) ────────────────────────────────────

def _git_commit(msg: str, paths: List[Path]) -> None:
    try:
        rel_paths = []
        for p in paths:
            try:
                rel_paths.append(str(p.resolve().relative_to(_PROJECT_ROOT)))
            except Exception:
                rel_paths.append(str(p))
        subprocess.run(
            ["git", "add", "--"] + rel_paths,
            cwd=str(_PROJECT_ROOT),
            check=False,
        )
        # Allow empty commits so the per-batch cadence is preserved
        # even if nothing changed on disk (rare, but possible).
        subprocess.run(
            ["git", "commit", "-m", msg, "--allow-empty"],
            cwd=str(_PROJECT_ROOT),
            check=False,
        )
    except Exception as e:
        print(f"  git commit skipped: {e!r}", flush=True)


# ── Audit loop ───────────────────────────────────────────────────────

def audit(
    *,
    db_path: str,
    cache_path: Path,
    batch_size: int = 10,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    print(f"[audit] db={db_path}", flush=True)
    print(f"[audit] cache={cache_path}", flush=True)
    print(f"[audit] batch_size={batch_size} limit={limit} dry_run={dry_run}",
          flush=True)

    candidates = fetch_audit_candidates(db_path)
    print(f"[audit] candidate live questions: {len(candidates)}", flush=True)

    cache = load_cache(cache_path)
    print(f"[audit] cache hits at start: {len(cache)}", flush=True)

    # Filter to pending items.
    pending = []
    for row in candidates:
        key = cache_key(row["id"], row["sid"])
        if key in cache:
            continue
        pending.append(row)
    if limit is not None:
        pending = pending[:limit]
    print(f"[audit] pending to judge: {len(pending)}", flush=True)
    if not pending:
        print("[audit] nothing to do.", flush=True)
        return _summarize(candidates, cache)

    # Lazy judge init so --limit 0 / empty-pool paths don't even touch auth.
    gw = _load_gateway()
    opus_call = _make_anthropic_judge(gw.MODEL_OPUS)
    sonnet_call = _make_anthropic_judge(gw.MODEL_SONNET)

    total_batches = (len(pending) + batch_size - 1) // batch_size
    start_ts = time.time()

    for batch_idx in range(total_batches):
        batch = pending[batch_idx * batch_size:(batch_idx + 1) * batch_size]
        batch_num = batch_idx + 1
        batch_start = time.time()
        print(f"\n[batch {batch_num}/{total_batches}] "
              f"({len(batch)} items) starting...", flush=True)

        batch_matches = 0
        batch_mismatches = 0
        batch_tier2 = 0
        batch_errors = 0

        for i, row in enumerate(batch, start=1):
            q_id = row["id"]
            s_id = row["sid"]
            key = cache_key(q_id, s_id)
            img = extract_first_image(row["stimulus_content"])
            if img is None:
                print(f"  q{q_id} sid{s_id}: no embedded image (skipped)",
                      flush=True)
                cache[key] = {
                    "question_id": q_id,
                    "stimulus_id": s_id,
                    "skipped_reason": "no_image",
                }
                continue
            img_bytes, media_type = img

            t0 = time.time()
            try:
                verdict = audit_pair(
                    question_id=q_id,
                    stimulus_id=s_id,
                    stem=row["prompt"],
                    image_bytes=img_bytes,
                    media_type=media_type,
                    opus_call=opus_call,
                    sonnet_call=sonnet_call,
                    subtype=row.get("subtype") or "",
                    source=row.get("source") or "",
                    stimulus_title=row.get("stimulus_title") or "",
                    parallel=True,
                )
            except Exception as e:
                batch_errors += 1
                print(f"  q{q_id} sid{s_id}: audit_pair raised {e!r}",
                      flush=True)
                continue

            elapsed = time.time() - t0
            cache[key] = verdict.as_dict()

            if verdict.confirmed_mispair:
                batch_mismatches += 1
                tag = "MISMATCH"
            elif verdict.tier2_disagreement:
                batch_tier2 += 1
                tag = "tier2"
            else:
                batch_matches += 1
                tag = "match"
            judge_errs = [j.error for j in verdict.judgments if j.error]
            err_tag = f" errors={judge_errs}" if judge_errs else ""
            print(f"  q{q_id} sid{s_id} {tag} ({elapsed:.1f}s){err_tag}",
                  flush=True)

            if verdict.confirmed_mispair:
                mark_mispair_as_draft(
                    db_path, q_id, verdict, dry_run=dry_run,
                )

            # Force progress print if the batch is running long so the
            # harness watchdog can observe forward motion.
            if time.time() - batch_start > 300:
                print(f"  [progress] batch {batch_num}: "
                      f"{i}/{len(batch)} items processed so far",
                      flush=True)

            # Wall-time guard per spec: abort batch if >6 min.
            if time.time() - batch_start > 360:
                print(f"  [abort-batch] {batch_num} exceeded 6min; "
                      f"committing partial and stopping",
                      flush=True)
                save_cache(cache_path, cache)
                _git_commit(
                    f"Mispair audit: batch {batch_num}/{total_batches} "
                    f"ABORTED at {i}/{len(batch)} items",
                    [cache_path, Path(db_path)],
                )
                return _summarize(candidates, cache)

        # Save cache + commit after each batch.
        save_cache(cache_path, cache)

        batch_elapsed = time.time() - batch_start
        total_elapsed = time.time() - start_ts
        print(
            f"[batch {batch_num}/{total_batches}] done in {batch_elapsed:.0f}s "
            f"| matches={batch_matches} "
            f"mismatches={batch_mismatches} "
            f"tier2={batch_tier2} "
            f"errors={batch_errors} "
            f"| total_elapsed={total_elapsed:.0f}s",
            flush=True,
        )
        _git_commit(
            f"Mispair audit: batch {batch_num}/{total_batches} "
            f"({batch_matches} match, {batch_mismatches} confirmed "
            f"mispair, {batch_tier2} tier2)",
            [cache_path, Path(db_path)],
        )

    return _summarize(candidates, cache)


def _summarize(
    candidates: List[Dict[str, Any]],
    cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    confirmed = 0
    tier2 = 0
    matches = 0
    skipped = 0
    errors = 0
    by_source: Dict[str, Dict[str, int]] = {}

    candidate_by_id = {(c["id"], c["sid"]): c for c in candidates}
    for key, entry in cache.items():
        try:
            qid_str, sid_str = key.split(":")
            qid, sid = int(qid_str), int(sid_str)
        except ValueError:
            continue
        candidate = candidate_by_id.get((qid, sid))
        source = (candidate or {}).get("source") or "unknown"
        bucket = by_source.setdefault(source, {
            "total": 0, "match": 0, "mismatch": 0,
            "tier2": 0, "skipped": 0, "errors": 0,
        })
        bucket["total"] += 1
        if entry.get("skipped_reason"):
            skipped += 1
            bucket["skipped"] += 1
            continue
        if entry.get("confirmed_mispair"):
            confirmed += 1
            bucket["mismatch"] += 1
        elif entry.get("tier2_disagreement"):
            tier2 += 1
            bucket["tier2"] += 1
        else:
            # Also check for judgment errors.
            judge_errs = [
                j for j in entry.get("judgments", [])
                if j.get("error")
            ]
            if judge_errs and len(judge_errs) >= 2:
                errors += 1
                bucket["errors"] += 1
            else:
                matches += 1
                bucket["match"] += 1

    return {
        "candidates": len(candidates),
        "cached": len(cache),
        "matches": matches,
        "confirmed_mispair": confirmed,
        "tier2_disagreement": tier2,
        "skipped": skipped,
        "errors": errors,
        "by_source": by_source,
    }


# ── CLI ──────────────────────────────────────────────────────────────

def _cli():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None,
                    help="DB path; defaults to data/gre_user.db")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE_PATH))
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.db:
        db_path = args.db
    else:
        db_path = str(_PROJECT_ROOT / "data" / "gre_user.db")

    summary = audit(
        db_path=db_path,
        cache_path=Path(args.cache),
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print("\n=== AUDIT SUMMARY ===", flush=True)
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    _cli()
