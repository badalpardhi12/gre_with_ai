#!/usr/bin/env python3
"""Embed every live Question into a numpy matrix on disk.

Usage::

    venv/bin/python scripts/embed_question_bank.py [--out PATH] [--limit N]

Defaults to writing
``data/dedup_eval/embeddings_2026_05_18.npy`` plus a sidecar
``embeddings_2026_05_18.npy.qids.json`` with the qids in row-order.

This is the input for stage-2A (cosine retrieval) of the two-stage
dedup pipeline. Re-run when the question bank changes substantively or
when the bi-encoder version is bumped.

Python 3.9 compatible.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make the repo root importable when running this script directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from models.database import Question, db  # noqa: E402
from services.dedup import embedding_stage  # noqa: E402
from services.dedup.config import (  # noqa: E402
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_MODEL_NAME,
)


DEFAULT_OUT_PATH = REPO_ROOT / "data" / "dedup_eval" / "embeddings_2026_05_18.npy"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_PATH,
        help=f"Output .npy path (default: {DEFAULT_OUT_PATH})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Embed only the first N live questions (smoke test).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=EMBEDDING_BATCH_SIZE,
        help=f"Bi-encoder batch size (default: {EMBEDDING_BATCH_SIZE})",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=EMBEDDING_MODEL_NAME,
        help=f"sentence-transformers model id (default: {EMBEDDING_MODEL_NAME})",
    )
    args = parser.parse_args()

    out_path = args.out

    db.connect(reuse_if_open=True)
    try:
        query = (
            Question
            .select()
            .where(Question.status == "live")
            .order_by(Question.id)
        )
        if args.limit:
            query = query.limit(args.limit)
        # Materialise so we know n upfront and so the connection isn't
        # held open across the heavy encode call.
        questions = list(query)
    finally:
        db.close()

    if not questions:
        print("No live questions found. Aborting.", file=sys.stderr)
        return 1

    print(
        f"[embed_question_bank] embedding {len(questions)} live questions "
        f"with {args.model} (batch_size={args.batch_size})"
    )

    t0 = time.time()
    embeddings, qids = embedding_stage.embed_questions(
        questions,
        out_path=out_path,
        model_name=args.model,
        batch_size=args.batch_size,
    )
    t1 = time.time()

    sidecar = out_path.with_suffix(out_path.suffix + ".qids.json")
    print(
        f"[embed_question_bank] wrote {embeddings.shape} → {out_path}\n"
        f"[embed_question_bank] sidecar qids ({len(qids)}) → {sidecar}\n"
        f"[embed_question_bank] runtime: {t1 - t0:.1f}s "
        f"({(t1 - t0) / max(len(qids), 1) * 1000:.1f} ms/item)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
