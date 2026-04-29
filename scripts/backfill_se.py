"""
SE-only backfill driver for the synthetic pipeline.

After the SE key-swap fix (commit b50f37c), this script regenerates
Sentence Equivalence items to reach the 20-item live target. It reuses
the Phase-1 pipeline primitives (drafter → critic/revise → jury →
adversarial solvers with SE reconcile → ambiguity probe → persist →
expert review) but builds a seed coverage focused purely on SE:
difficulty mix + subtopic mix only.

Usage:
    venv/bin/python scripts/backfill_se.py \\
        --count 15 \\
        --run-id se-backfill-2026-04-27 \\
        --start-at 0

Audit logs + assets land under data/synthetic/runs/<run_id>/.
Persisted items go into the `question` table with source='ai_synthetic',
subtype='se', run_id=<run_id>. Expert-review promotion rules apply,
same as Phase-1 prod.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Re-use Phase-1 machinery directly.
from scripts.run_synthetic_phase1 import (  # noqa: E402
    JsonlAudit, _EXPERT_FACTORY, _build_factory, _build_pipeline,
    _register_local_backend, _run_one_seed, subtopic_display,
)
import scripts.run_synthetic_phase1 as phase1_mod  # noqa: E402
from models.database import (  # noqa: E402
    Question, db, init_db,
)
from services.log import get_logger  # noqa: E402
from services.synthetic.dedup import make_default_deduper  # noqa: E402
from services.synthetic.types import Seed  # noqa: E402

logger = get_logger("synthetic.backfill_se")


# Distribution: cover both SE subtopics and all difficulty tiers.
# 2 easy / 4 medium / 4 hard per cycle; script cycles this list.
SE_SEED_TEMPLATES: List[Tuple[str, str, int]] = [
    ("sentence_equivalence", "se_synonyms", 2),
    ("sentence_equivalence", "se_synonyms", 3),
    ("sentence_equivalence", "se_contrast", 3),
    ("sentence_equivalence", "se_synonyms", 4),
    ("sentence_equivalence", "se_contrast", 4),
    ("sentence_equivalence", "se_synonyms", 3),
    ("sentence_equivalence", "se_contrast", 2),
    ("sentence_equivalence", "se_synonyms", 4),
    ("sentence_equivalence", "se_contrast", 3),
    ("sentence_equivalence", "se_synonyms", 3),
]

VERBAL_PERSONAS = (
    "academic_neutral", "journalistic", "scientific_textbook",
    "policy_brief", "historical_essay",
)
VERBAL_SCENARIOS = (
    "humanities", "biological_sciences", "physical_sciences",
    "social_sciences", "everyday",
)


def _build_se_seeds(count: int, rng: random.Random) -> List[Seed]:
    seeds: List[Seed] = []
    idx = 0
    while len(seeds) < count:
        topic, subtopic, difficulty = SE_SEED_TEMPLATES[
            idx % len(SE_SEED_TEMPLATES)
        ]
        idx += 1
        seeds.append(Seed(
            measure="verbal",
            topic=topic,
            subtopic=subtopic,
            subtype="se",
            difficulty_target=difficulty,
            extra={
                "scenario_class": rng.choice(VERBAL_SCENARIOS),
                "persona": rng.choice(VERBAL_PERSONAS),
                "structural_frame": "default",
            },
        ))
    return seeds


def _count_live_se() -> int:
    return (Question
            .select()
            .where((Question.source == "ai_synthetic")
                   & (Question.subtype == "se")
                   & (Question.status == "live"))
            .count())


def _count_persisted_se_this_run(run_id: str) -> int:
    return (Question
            .select()
            .where((Question.source == "ai_synthetic")
                   & (Question.subtype == "se")
                   & (Question.run_id == run_id))
            .count())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=15,
                   help="How many SE candidates to generate.")
    p.add_argument("--run-id", type=str,
                   default=f"se-backfill-{datetime.now():%Y%m%d-%H%M%S}")
    p.add_argument("--seed", type=int, default=2027)
    p.add_argument("--start-at", type=int, default=0)
    args = p.parse_args()

    init_db()
    _register_local_backend()
    factory = _build_factory()
    pipeline = _build_pipeline(factory, drafter_alias="opus")
    # Wire the expert-review factory (Phase-1 main() sets this global).
    phase1_mod._EXPERT_FACTORY = factory

    rng = random.Random(args.seed)
    seeds = _build_se_seeds(args.count, rng=rng)

    run_dir = ROOT / "data" / "synthetic" / "runs" / args.run_id
    assets_dir = run_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    audit = JsonlAudit(run_dir / "audit.jsonl")

    live_before = _count_live_se()
    audit.emit("run_start", run_id=args.run_id, subtype="se",
                n_seeds=len(seeds), live_before=live_before,
                start_at=args.start_at)
    print(f"[backfill] run_id={args.run_id} seeds={len(seeds)} "
          f"live_SE_before={live_before}", flush=True)

    deduper = make_default_deduper()
    cluster_state: Dict[str, Any] = {}
    persisted_qids: List[int] = []
    rejects = 0
    key_swaps = 0

    for i, seed in enumerate(seeds):
        if i < args.start_at:
            continue
        t0 = time.time()
        try:
            rec = _run_one_seed(
                seed_idx=i,
                seed=seed,
                pipeline=pipeline,
                run_id=args.run_id,
                assets_dir=assets_dir,
                audit=audit,
                cluster_state=cluster_state,
                deduper=deduper,
            )
        except Exception as exc:
            audit.emit("seed_exception", seed_idx=i, error=str(exc),
                        traceback=traceback.format_exc())
            print(f"[backfill] seed {i} EXCEPTION: {exc}", flush=True)
            continue
        elapsed = time.time() - t0
        if rec and rec.get("persisted"):
            persisted_qids.append(rec["qid"])
            print(f"[backfill] seed {i} -> qid={rec['qid']} "
                  f"decision={rec['decision']} ({elapsed:.1f}s)", flush=True)
        else:
            rejects += 1
            reason = rec.get("reject_reason", "") if rec else "None"
            print(f"[backfill] seed {i} REJECTED: {reason[:80]} "
                  f"({elapsed:.1f}s)", flush=True)

    # Count key swaps from audit
    try:
        with open(run_dir / "audit.jsonl") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                    if ev.get("event") == "se_key_swap":
                        key_swaps += 1
                except Exception:
                    pass
    except Exception:
        pass

    live_after = _count_live_se()
    run_persisted = _count_persisted_se_this_run(args.run_id)

    summary = {
        "run_id": args.run_id,
        "seeds_run": len(seeds),
        "persisted": len(persisted_qids),
        "rejected": rejects,
        "key_swaps": key_swaps,
        "pass_rate": (len(persisted_qids) / max(1, len(seeds))),
        "live_se_before": live_before,
        "live_se_after": live_after,
        "run_persisted_se": run_persisted,
    }
    audit.emit("run_done", **summary)
    audit.close()
    print(f"[backfill] DONE {summary}", flush=True)


if __name__ == "__main__":
    main()
