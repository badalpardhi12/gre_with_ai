# Final merge — 2026-04-29

Ships the three outstanding feature branches on top of the prior
consolidation, plus the `difficulty_target` uniformity fix that was
gating real easy/hard filtering.

## Summary

Merged branches (in order, each via `--no-ff`):

1. **`data-quality-sweep-2026-04-28`** (B) — cross-bank dedup (164
   duplicates retired), Princeton subtopic backfill via Haiku (991
   classified), legacy `ai_generated` expert-review panel (407 items,
   130 demoted to draft), unified `services.expert_review` module.
   One conflict: `.gitignore` (trivially kept both backup suffixes).
2. **`audit-exam-assembly-2026-04-28`** (A) — ETS blueprint enforcement
   in `services.question_bank`, DI-cluster anchoring in every Quant
   section, 29 new pinned blueprint tests, `scripts/audit_exam_assembly.py`
   and `scripts/smoke_test_exam_assembly.py` probes, `data/audits/
   ets_blueprint_2026.md`. One conflict: `services/question_bank.py`,
   resolved by keeping main's `exclude_user_seen` API (already
   threaded through production callers) and layering A's DI-cluster
   anchor step + `_select_di_cluster` helper + `_composition_targets`
   static method + `CLUSTERED_VERBAL_SUBTYPES` export + RC
   cross-subtype atomicity into the existing flow.
3. **`vision-review-princeton-figures-2026-04-28`** (C) — migration 015
   adds `question.figure_refs`, vision-panel review of 51 Princeton draft
   figures (45 promoted / 6 demoted), 105 figure-ref backfills,
   `services/vision_expert_review.py`, 15 new tests. Three conflicts:
   `models/database.py` (merged the `figure_refs` field + helpers alongside
   main's 012/013/014 columns), `models/migrations.py` (kept all of
   012/013/014/015 in order), and `data/gre_mock.db` (row-level upsert via
   new `scripts/upsert_from_branch_db.py`, not a pick-one-file merge).

Test results after each merge:

| Stage              | pytest |
|--------------------|--------|
| Baseline (pre-fix) | 366 / 1 skipped |
| After difficulty fix | 369 |
| After B merge      | 413 |
| After A merge      | 442 |
| After C merge      | 459 |

Smoke test (`scripts/smoke_test_exam_assembly.py`): 5/5 exams pass the
blueprint, 0 dedup violations, exit 0.

## `difficulty_target` uniformity fix (commit c5139b3)

Before: every Princeton (991), Manhattan (1439), and legacy
`ai_generated` (1037) row carried `difficulty_target=3`, collapsing
`services.question_bank`'s easy (`<=2`) and hard (`>=4`) filters to
empty sets. Root cause: `scripts/persist_princeton.py` hardcoded
`"difficulty": 3` in `build_question_for_review` + the payload dict at
persist time, and the Manhattan/legacy-ai_generated imports never ran
a difficulty-rating pass afterwards.

Fix:

- `scripts/backfill_difficulty_target.py` — per-`(source, subtype)`
  quintile split on combined prompt + stimulus length, deterministic
  and idempotent. Subtype-aware curves (TC/SE push harder at the
  tails, RC singles lean easier).
- `scripts/persist_princeton.py::_estimate_difficulty` — replaces the
  hardcoded 3 so future Princeton runs ship a spread at persist time.
- `tests/test_difficulty_target_spread.py` — three regression guards
  (live pool must cover easy+medium+hard, each affected source must
  carry ≥3 distinct difficulties, estimator must be non-constant).

Post-fix live distribution: `d=1:137 d=2:571 d=3:1237 d=4:889 d=5:156`.

## Final row counts (`gre_user.db`, per source × status)

| Source               | live | draft | candidate | retired |
|----------------------|-----:|------:|----------:|--------:|
| `ai_generated`       | 749  | 130   | 0         | 158     |
| `ai_synthetic`       | 180  | 79    | 44        | 1       |
| `kaplan_2024`        | 151  | 70    | 0         | 0       |
| `manhattan_5lb_2018` | 1365 | 0     | 0         | 74      |
| `princeton_2012`     | 545  | 442   | 0         | 4       |
| `imported` (legacy)  | 0    | 0     | 0         | 1259    |

Total live: **2990**.

## Known outstanding

- **470 unreviewed `ai_generated` items.** Commit 7ee6130 expert-reviewed
  407 of the 877 legacy items (130 demoted). The remaining pool can be
  processed by re-running, incrementally:

  ```
  venv/bin/python scripts/expert_review_ai_generated.py \
      --resume --batch-size 20
  ```

  The script's per-item cache under `data/extracted/legacy_ai_generated/`
  makes re-runs idempotent; a full pass on the remaining 470 takes
  1–2 hours wall-clock and is safe to run in the background.

- **Princeton vision panel** only reviewed the 51 `needs_vision` items
  C surfaced. The broader 448-item Princeton draft pool still needs
  retirement/promotion decisions before shipping as live questions.

## Resume commands

- Legacy `ai_generated` review:
  ```
  venv/bin/python scripts/expert_review_ai_generated.py --resume
  ```
- Princeton subtopic backfill (already 991/991 classified, but the
  script is idempotent and catches new rows):
  ```
  venv/bin/python scripts/backfill_princeton_subtopics.py --only-missing
  ```
- Cross-bank dedup sweep:
  ```
  venv/bin/python scripts/dedup_cross_bank.py --apply
  ```
- Exam-assembly smoke test:
  ```
  venv/bin/python scripts/smoke_test_exam_assembly.py
  ```

## Rollback

Local pre-merge backups (gitignored):

- `data/gre_mock.db.pre-final-merge.bak` (main's DB before the three-way merge)
- `data/gre_user.db.pre-final-merge.bak` (runtime DB before the merge)

To roll main back to pre-merge:

```
git reset --hard 7115f80                             # pre-B-merge
cp data/gre_mock.db.pre-final-merge.bak data/gre_mock.db
cp data/gre_user.db.pre-final-merge.bak data/gre_user.db
```

(The difficulty-fix commit `c5139b3` and the three merge commits
`b54041c`, `a37b33c`, `6b345ec` will all go away with the reset.)


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
