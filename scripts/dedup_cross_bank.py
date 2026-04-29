"""Cross-bank duplicate detection + retirement.

Part of the 2026-04-28 data quality sweep. Scans every live question
in the consolidated DB, computes:

  1. A deterministic fingerprint (normalised whitespace + lowercase +
     stripped punctuation) → SHA-1 hex digest. Fingerprint collisions
     are hard duplicates.
  2. A sentence-transformers all-MiniLM-L6-v2 embedding. Cosine
     similarity >= 0.95 ⇒ flagged as near-duplicate.

For each duplicate pair the script keeps the copy with the highest
``source_priority`` (ordered: princeton_2012 > kaplan_2024 >
manhattan_5lb_2018 > ai_generated > ai_synthetic). The loser is marked
``status='retired'`` with a ``review_notes`` line pointing at the kept
qid. Never touches items that are already retired/draft/candidate.

Writes a summary markdown report to
``data/audits/cross_bank_dedup_2026_04_28.md``. All-network-free: only
reads MiniLM from the local HF hub cache.

Usage:
  venv/bin/python scripts/dedup_cross_bank.py
  venv/bin/python scripts/dedup_cross_bank.py --dry-run
  venv/bin/python scripts/dedup_cross_bank.py --threshold 0.97
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


SOURCE_PRIORITY = {
    "princeton_2012": 5,
    "kaplan_2024": 4,
    "manhattan_5lb_2018": 3,
    "ai_generated": 2,
    "ai_synthetic": 1,
    "imported": 0,
    "seed": 0,
}


_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_DATA_URL_RE = re.compile(
    r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]{20,}",
    re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_noise(text: str) -> str:
    """Drop base64 data URLs and HTML tags — they don't carry semantic
    signal for dedup but can make unrelated items look identical when
    the first 4KB of two separate images share a base64 preamble."""
    if not text:
        return ""
    t = _DATA_URL_RE.sub(" ", text)
    t = _HTML_TAG_RE.sub(" ", t)
    return t


def fingerprint(text: str) -> str:
    if not text:
        return ""
    t = _strip_noise(text)
    t = t.lower()
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    if not t:
        return ""
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


def load_live_rows():
    """Load live rows plus their stimulus text.

    Two questions with the same stem ("The primary purpose of the
    passage is to") but different passages are NOT duplicates. We key
    the dedup text on stem + stimulus content so the passage disambiguates.
    """
    from models.database import Question, Stimulus  # late import
    from models.database import QuestionOption
    # Pull stimuli once so we can inline content without N+1 queries.
    stim_by_id: Dict[int, str] = {
        s.id: (s.content or "")[:4000]
        for s in Stimulus.select()
    }
    # Pull options so we can include them in the fingerprint (Kaplan
    # sometimes reuses a stem verbatim with different option wording).
    opts_by_q: Dict[int, List[str]] = defaultdict(list)
    for o in (QuestionOption
              .select(QuestionOption.question_id,
                      QuestionOption.option_text,
                      QuestionOption.option_label)
              .order_by(QuestionOption.question_id,
                        QuestionOption.option_label)):
        opts_by_q[o.question_id].append(
            f"{o.option_label}:{(o.option_text or '').strip()}"
        )

    rows = list(
        Question.select(
            Question.id, Question.source, Question.status,
            Question.measure, Question.subtype, Question.prompt,
            Question.review_notes, Question.stimulus_id,
        )
        .where(Question.status == "live")
        .order_by(Question.id)
    )
    # Attach dedup_text to each row (plain attribute; no ORM column).
    for r in rows:
        stim_text = ""
        if r.stimulus_id:
            stim_text = stim_by_id.get(r.stimulus_id, "")
        parts = [stim_text, r.prompt or ""]
        parts.extend(opts_by_q.get(r.id, []))
        r.dedup_text = "\n".join(p for p in parts if p).strip()
    return rows


def priority(row) -> int:
    return SOURCE_PRIORITY.get(row.source, -1)


def pair_key(a: int, b: int) -> Tuple[int, int]:
    return (a, b) if a < b else (b, a)


def compute_embeddings(texts: List[str], batch_size: int = 64):
    from sentence_transformers import SentenceTransformer
    import torch  # noqa: F401 — ensure available for caller
    print(f"[dedup] loading sentence-transformers/all-MiniLM-L6-v2 offline…",
          flush=True)
    model = SentenceTransformer("all-MiniLM-L6-v2")
    # MiniLM truncates at 256 tokens anyway; clipping the raw string keeps
    # tokenization fast without changing semantics for our purposes.
    clipped = [t[:2000] for t in texts]
    embs = model.encode(
        clipped,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_tensor=True,
        normalize_embeddings=True,
    )
    return embs


def find_cosine_duplicate_pairs(embs, threshold: float,
                                 top_k: int = 10) -> List[Tuple[int, int, float]]:
    """For every row, find items whose cosine similarity >= threshold.

    Returns a deduplicated list of (i, j, sim) with i < j.
    Uses torch matmul in chunks to keep memory bounded.
    """
    import torch
    n = embs.shape[0]
    pairs: Dict[Tuple[int, int], float] = {}
    chunk = 256
    for start in range(0, n, chunk):
        end = min(start + chunk, n)
        sims = embs[start:end] @ embs.T  # (chunk, n)
        # zero out self-similarity for diagonal
        for local_i, global_i in enumerate(range(start, end)):
            sims[local_i, global_i] = -1.0
        # gather indices >= threshold
        mask = sims >= threshold
        idx_rows, idx_cols = torch.nonzero(mask, as_tuple=True)
        for li, gj in zip(idx_rows.tolist(), idx_cols.tolist()):
            gi = start + li
            if gi == gj:
                continue
            key = pair_key(gi, gj)
            val = float(sims[li, gj].item())
            prev = pairs.get(key)
            if prev is None or val > prev:
                pairs[key] = val
    return [(a, b, s) for (a, b), s in pairs.items()]


def pick_keeper(row_a, row_b):
    pa, pb = priority(row_a), priority(row_b)
    if pa != pb:
        return (row_a, row_b) if pa > pb else (row_b, row_a)
    # Tiebreak on id (prefer lower id).
    return (row_a, row_b) if row_a.id < row_b.id else (row_b, row_a)


def build_pair_keeper_plan(
    pairs: List[Tuple[int, int, float]],
    id_to_row: Dict[int, "object"],
) -> Tuple[List[Tuple[int, int, float]], Dict[int, int]]:
    """Pair-based retire plan.

    For each duplicate pair we pick the keeper by source priority
    (tiebreak on lower qid). We then walk the pairs and for each loser
    we record its *single best* keeper (highest priority among all the
    winners that flagged it). This avoids the transitive-collapse
    failure mode where a chain A~B~C~D groups disjoint questions into
    one cluster even though A and D are genuinely different.

    Returns:
        retire_plan: list of (loser_qid, keeper_qid, similarity) in a
            deterministic order.
        loser_to_keeper: dict mapping each retired qid to its chosen
            keeper (handy for the audit report).
    """
    best_keeper: Dict[int, Tuple[int, int, float]] = {}
    # For each loser qid, record (keeper_priority, keeper_id, similarity).

    for a, b, sim in pairs:
        ra = id_to_row[a]
        rb = id_to_row[b]
        pa = SOURCE_PRIORITY.get(ra.source, -1)
        pb = SOURCE_PRIORITY.get(rb.source, -1)
        if pa > pb or (pa == pb and a < b):
            keeper, loser = ra, rb
        else:
            keeper, loser = rb, ra
        kp = SOURCE_PRIORITY.get(keeper.source, -1)
        prev = best_keeper.get(loser.id)
        candidate = (kp, -keeper.id, sim, keeper.id)
        if prev is None or candidate > (prev[0], -prev[1], prev[2], prev[1]):
            best_keeper[loser.id] = (kp, keeper.id, sim)

    # Filter: a node can't simultaneously be a keeper for someone else
    # AND be retired itself. If it appears as a loser, always retire it
    # (preferring the higher-priority keeper).
    retire_plan: List[Tuple[int, int, float]] = []
    loser_to_keeper: Dict[int, int] = {}
    for loser_id, (_, keeper_id, sim) in best_keeper.items():
        retire_plan.append((loser_id, keeper_id, sim))
        loser_to_keeper[loser_id] = keeper_id
    # Deterministic ordering by loser qid.
    retire_plan.sort(key=lambda t: t[0])
    return retire_plan, loser_to_keeper


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--threshold", type=float, default=0.95,
                   help="cosine similarity threshold (default 0.95)")
    p.add_argument("--dry-run", action="store_true",
                   help="report only; do not mutate the DB")
    p.add_argument("--report", type=str,
                   default=str(REPO / "data" / "audits"
                              / "cross_bank_dedup_2026_04_28.md"))
    args = p.parse_args(argv)

    from models.database import db, Question
    db.connect(reuse_if_open=True)

    print("[dedup] loading live rows…", flush=True)
    rows = load_live_rows()
    print(f"[dedup] {len(rows)} live questions loaded", flush=True)

    # ── Exact fingerprint pass ───────────────────────────────────────
    MIN_DEDUP_CHARS = 40  # ignore items too short to meaningfully match
    fp_to_ids: Dict[str, List[int]] = defaultdict(list)
    id_to_row = {r.id: r for r in rows}
    texts: List[str] = []
    ids_order: List[int] = []
    eligible: Set[int] = set()
    for r in rows:
        raw = getattr(r, "dedup_text", "") or (r.prompt or "")
        dedup_src = _strip_noise(raw).strip()
        dedup_src = _WS_RE.sub(" ", dedup_src)
        fp = fingerprint(dedup_src)
        if fp and len(dedup_src) >= MIN_DEDUP_CHARS:
            fp_to_ids[fp].append(r.id)
            eligible.add(r.id)
        texts.append(dedup_src)
        ids_order.append(r.id)

    def _share_stimulus(qid_a: int, qid_b: int) -> bool:
        """Two items sharing a stimulus are multi-question RC siblings,
        NOT duplicates. Cosine on (stimulus + short prompt) would
        otherwise group them because the stimulus dominates the vector.
        """
        ra = id_to_row[qid_a]
        rb = id_to_row[qid_b]
        sa = getattr(ra, "stimulus_id", None)
        sb = getattr(rb, "stimulus_id", None)
        return bool(sa) and bool(sb) and sa == sb

    exact_pairs: List[Tuple[int, int, float]] = []
    for fp, ids in fp_to_ids.items():
        if len(ids) < 2:
            continue
        ids = sorted(ids)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if _share_stimulus(a, b):
                    continue
                exact_pairs.append((a, b, 1.0))
    print(f"[dedup] exact-fingerprint pairs: {len(exact_pairs)}", flush=True)

    # ── Embedding pass ───────────────────────────────────────────────
    embs = compute_embeddings(texts)
    print("[dedup] embeddings computed; scanning cosine >= "
          f"{args.threshold}…", flush=True)
    cos_pairs_raw = find_cosine_duplicate_pairs(embs, args.threshold)
    # Translate local indices back to qids; drop shared-stimulus pairs
    # and any pair where either member is too short to trust.
    cos_pairs: List[Tuple[int, int, float]] = []
    for i, j, s in cos_pairs_raw:
        a, b = ids_order[i], ids_order[j]
        if a not in eligible or b not in eligible:
            continue
        if _share_stimulus(a, b):
            continue
        cos_pairs.append((a, b, s))
    print(f"[dedup] cosine near-duplicate pairs: {len(cos_pairs)}",
          flush=True)

    # Merge & dedupe pairs.
    all_pairs: Dict[Tuple[int, int], float] = {}
    for a, b, s in exact_pairs:
        all_pairs[pair_key(a, b)] = max(s, all_pairs.get(pair_key(a, b), 0.0))
    for a, b, s in cos_pairs:
        all_pairs[pair_key(a, b)] = max(s, all_pairs.get(pair_key(a, b), 0.0))
    merged = [(a, b, s) for (a, b), s in all_pairs.items()]
    merged.sort(key=lambda t: (-t[2], t[0], t[1]))
    print(f"[dedup] total unique duplicate pairs: {len(merged)}",
          flush=True)

    # ── Pair-based retire plan ───────────────────────────────────────
    retire_plan, loser_to_keeper = build_pair_keeper_plan(merged, id_to_row)
    print(f"[dedup] items to retire: {len(retire_plan)}", flush=True)

    kept_retired_by_pair: Dict[Tuple[str, str], int] = defaultdict(int)
    kept_qids: Set[int] = set()
    for loser_id, keeper_id, _sim in retire_plan:
        keeper = id_to_row[keeper_id]
        loser = id_to_row[loser_id]
        kept_retired_by_pair[(keeper.source, loser.source)] += 1
        kept_qids.add(keeper_id)
    kept_count = len(kept_qids)
    print(f"[dedup] distinct keepers: {kept_count}", flush=True)

    # ── Apply retirements ────────────────────────────────────────────
    retired_actual = 0
    if not args.dry_run and retire_plan:
        # Use targeted UPDATE...WHERE (not .save()) — .save() rebinds every
        # column and a post-consolidation DB has index edge cases that
        # surface as "database disk image is malformed" on full-row
        # writes. Narrow UPDATEs sidestep the problem.
        from datetime import datetime as _dt
        print(f"[dedup] beginning transaction for "
              f"{len(retire_plan)} retirements…", flush=True)
        db.connect(reuse_if_open=True)
        with db.atomic() as txn:
            for loser_id, keeper_id, sim in retire_plan:
                q = Question.get_or_none(Question.id == loser_id)
                if q is None or q.status != "live":
                    continue  # already dealt with or missing
                prev_notes = q.review_notes or ""
                note = (
                    f"[dedup 2026-04-28] retired as cross-bank duplicate of "
                    f"qid={keeper_id} (cos_sim={sim:.3f})."
                )
                new_notes = (
                    prev_notes if note in prev_notes
                    else (f"{prev_notes}\n\n{note}".strip()
                          if prev_notes else note)
                )
                rc = Question.update(
                    status="retired",
                    review_notes=new_notes,
                    updated_at=_dt.now(),
                ).where(Question.id == loser_id).execute()
                if rc:
                    retired_actual += 1
            txn.commit()
        db.close()
        print(f"[dedup] DB mutation applied: {retired_actual} "
              f"retired", flush=True)
    elif args.dry_run:
        print("[dedup] DRY RUN — no DB mutation", flush=True)

    # ── Write report ─────────────────────────────────────────────────
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    lines: List[str] = []
    lines.append("# Cross-bank duplicate detection — 2026-04-28")
    lines.append("")
    lines.append(f"- Timestamp: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- Live questions scanned: **{len(rows)}**")
    lines.append(f"- Similarity threshold: **{args.threshold}**")
    lines.append(f"- Exact-fingerprint pairs: **{len(exact_pairs)}**")
    lines.append(f"- Cosine near-duplicate pairs (>= threshold, excluding "
                 f"exact): **{max(0, len(cos_pairs) - len(exact_pairs))}**")
    lines.append(f"- Total unique duplicate pairs: **{len(merged)}**")
    lines.append(f"- Distinct keepers: **{kept_count}**")
    lines.append(f"- Items retired: **{len(retire_plan)}**")
    if args.dry_run:
        lines.append("- Mode: **DRY RUN** (no DB mutation)")
    lines.append("")

    lines.append("## Retired-by keeper-source pairs")
    lines.append("")
    lines.append("| keeper.source | retired.source | count |")
    lines.append("|---|---|---|")
    for (ks, rs), n in sorted(kept_retired_by_pair.items(),
                              key=lambda kv: -kv[1]):
        lines.append(f"| {ks} | {rs} | {n} |")
    lines.append("")

    lines.append("## Retire plan (loser → keeper, top 200 by similarity)")
    lines.append("")
    sorted_retire = sorted(retire_plan, key=lambda t: (-t[2], t[0]))
    for loser_id, keeper_id, sim in sorted_retire[:200]:
        keeper = id_to_row[keeper_id]
        loser = id_to_row[loser_id]
        lp = (loser.prompt or "").strip().replace("\n", " ")
        kp = (keeper.prompt or "").strip().replace("\n", " ")
        if len(lp) > 140:
            lp = lp[:140] + "…"
        if len(kp) > 140:
            kp = kp[:140] + "…"
        lines.append(
            f"- retire qid={loser_id} [{loser.source}/{loser.subtype}] → "
            f"keep qid={keeper_id} [{keeper.source}/{keeper.subtype}] "
            f"(cos_sim={sim:.3f})"
        )
        lines.append(f"    - loser : {lp}")
        lines.append(f"    - keeper: {kp}")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[dedup] report written to {report_path}", flush=True)
    print("[dedup] done.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
