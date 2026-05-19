"""Two-stage dedup pipeline for the GRE question bank.

Stage 1 (1.2 — separate module ``minhash_stage``): cheap MinHash/LSH near-dup catch.
Stage 2 (1.3 — this package's ``embedding_stage``): semantic paraphrase catch via
sentence-transformer embeddings + cross-encoder re-ranking.

Threshold + model constants live in :mod:`services.dedup.config`. The 1.4
integration layer (:mod:`services.dedup.dedup_service`) composes both stages
into a single ``find_dup_for`` callable that ingest scripts call before each
``Question.create``.
"""

# Re-export the public API of the integration layer so callers can simply do
# ``from services.dedup import get_dedup_service``.
from services.dedup.dedup_service import (  # noqa: E402,F401
    DedupService,
    get_dedup_service,
    reset_dedup_service,
)

