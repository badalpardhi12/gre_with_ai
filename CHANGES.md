# Consolidation — 2026-04-28

This change consolidates five parallel development streams into a single
branch so the app is ready for user testing.

Feature branch: `consolidate-all-2026-04-28` (off `main`).

## Code additions

### Session engine (worktree `agent-a20c899c`)
- `services/question_bank.py` — cluster-aware assembly (RC / DI
  passages pull every sibling under the same `stimulus_id`) + cross-session
  dedup (mastered items cool down, wrong items resurface sooner).
- `models/exam_session.py` — tracks served-item IDs so the next section
  avoids repeats.
- Tests: `tests/test_cluster_aware_assembly.py`,
  `tests/test_cross_session_dedup.py`, updated `tests/test_exam_session.py`.

### DI / RC cluster backfill (worktree `agent-afcf77e0`)
- `scripts/audit_cluster_anomalies.py` — detect orphan siblings,
  mixed-subtype clusters, partial cluster splits.
- `scripts/backfill_clusters.py` — idempotent re-stitcher.
- `data/audits/di_rc_anomaly_audit.md` — historical report of what the
  scripts fixed on main's DB (already applied in a prior session).

### Extraction modules
- `services/extraction_verification.py` — handles Princeton + Kaplan
  field-name conventions (Kaplan worktree's copy; 694 lines).
- `services/expert_review.py` — synthetic-pipeline's multi-judge rubric
  (worktree `agent-a5d145f1`'s copy; exports `EXPERT_AXES`,
  `ExpertReviewResult`, `aggregate_expert_panel`).
- `services/expert_review_kaplan.py` — Kaplan's flavor preserved
  side-by-side because `scripts/retroactive_expert_review_kaplan.py`
  depends on its `RUBRIC_AXES` / `JudgeReport` interface.
- `services/image_classifier.py` — deterministic + LLM classifier used by
  both Princeton and Kaplan (Kaplan worktree's; has specific
  numeric-box + bullet-glyph detectors).
- `services/kaplan_verification.py`, `services/vision_render.py`,
  `services/image_pipeline.py` — supporting modules for figure
  rendering + vision-verification path.

### Synthetic generation pipeline (worktree `agent-a5d145f1`)
- `services/synthetic/` (full tree): drafter, critic, judge, reviser,
  ambiguity/dedup/domain checks, figure renderers, calibration anchors.
- `scripts/run_synthetic_phase1.py`, `scripts/backfill_se.py`,
  `scripts/calibrate_synthetic_rubric.py`,
  `scripts/render_synthetic_sample.py`.
- `tests/synthetic/` (full tree).
- `config.py` — new `load_user_prefs` / `save_user_pref` with
  `include_ai_synthetic` toggle. `services/question_bank.py` threads the
  toggle through every query-producing helper so the Settings dialog
  actually hides synthetic items when flipped off.

### Extractors
- `scripts/extract_princeton.py`, `scripts/persist_princeton.py`,
  `scripts/verify_princeton_extraction.py`,
  `scripts/build_princeton_sample_md.py` — Princeton Review 1,014 EPUB.
- `scripts/extract_kaplan.py`, `scripts/persist_kaplan.py`,
  `scripts/retroactive_expert_review_kaplan.py` — Kaplan 2024 EPUB.
- `validators/kaplan.py` — Kaplan-specific field validators.

### Schema + migrations
- `models/database.py` — synthetic's canonical copy (Question lifecycle
  columns, SyntheticGenerationRun table) + Princeton's `source_anchor`
  column reconciled in.
- `models/migrations.py` — 14 migrations total:
  - `012_synthetic_provenance_2026_04` — `provenance_json`,
    `review_notes`, `generated_at`, `run_id` + `SyntheticGenerationRun`.
  - `013_question_lifecycle_2026_05` — pretest / IRT columns
    (`pretest_started_at`, `pretest_n_responses`, `pretest_p_correct`,
    `pretest_disc_proxy`, `irt_b_estimate`, `irt_a_estimate`,
    `promotion_at`) + partial index on `status='pretest'`.
  - `014_source_anchor_2026_04` (NEW this consolidation) — adds
    `source_anchor` column. Princeton's original migration 012 also added
    `review_notes`, but synthetic's 012 already did; this new 014 only
    adds the unique `source_anchor` column.

### Consolidation tooling
- `scripts/consolidate_dbs.py` (NEW) — idempotent upsert from each
  worktree DB into main's runtime DB (`data/gre_user.db`). Upserts by
  `(source, source_anchor)` when available, falls back to a
  content-hash of `(prompt, stimulus.content, options_text)`. Children
  (options, numeric_answer) are deleted and recreated under the
  resolved parent id. Reruns are no-ops.

## Data delta

Baseline (before consolidation):

| source              | live | draft | candidate | retired |
|---------------------|------|-------|-----------|---------|
| ai_generated        | 1033 |       |           | 4       |
| manhattan_5lb_2018  | 1370 |       |           | 69      |
| imported            |      |       |           | 1259    |

After consolidation:

| source              | live | draft | candidate | retired |
|---------------------|------|-------|-----------|---------|
| ai_generated        | 1033 |       |           | 4       |
| ai_synthetic        | 181  | 79    | 44        |         |
| kaplan_2024         | 151  | 70    |           |         |
| manhattan_5lb_2018  | 1370 |       |           | 69      |
| princeton_2012      | 543  | 448   |           |         |
| imported            |      |       |           | 1259    |

Totals added: 991 Princeton + 221 Kaplan + 304 synthetic = **+1516 items**.

Both `data/gre_mock.db` (the LFS-tracked seed) and `data/gre_user.db`
(the runtime copy) hold the consolidated content. Pre-consolidation
backups are saved at:

- `data/gre_mock.db.pre-consolidation.bak`
- `data/gre_user.db.pre-consolidation.bak`

## Tests

All **366** tests pass (1 skipped). Full suite:
`venv/bin/python -m pytest -q`.

## Known outstanding

- **Princeton subtopic mapping**: all 991 Princeton items land with empty
  `subtopic`. This matches the source worktree — the Princeton extractor
  mapped `topic` but not `subtopic`. Topic-drill UX is fine; subtopic
  drill won't surface these until a later mapping pass.
- **Kaplan retroactive expert review**: approximately 33 Kaplan items
  were not re-reviewed under the updated rubric (user paused the retro
  review); those rows sit at status=live without the latest `review_notes`.
- **Princeton figure-item review**: approximately 188 Princeton items
  bypass the text-only expert review because they contain figures that
  the text-only prompt can't inspect; these rows are at status=draft
  pending a vision-capable review pass.
- **Local-only helpers**: `services/_vision_adapter.py` (Apple-internal
  vision client used by `extract_kaplan.py`) stays gitignored; the
  extractor's import is lazy inside a function body, so `extract_kaplan.py`
  still imports cleanly without the adapter present.

## How to merge

From `main`:

```
git merge consolidate-all-2026-04-28
```

The branch is local-only; nothing is pushed.

## Rollback

If anything looks wrong after merging:

```
cp data/gre_user.db.pre-consolidation.bak data/gre_user.db
cp data/gre_mock.db.pre-consolidation.bak data/gre_mock.db
```

Then revert the merge commit and re-checkout the feature branch to
investigate.
