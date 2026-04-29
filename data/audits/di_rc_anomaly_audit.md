# DI + rc_multi cluster anomaly audit

Scope: two data-level anomalies surfaced by the session-engine agent.

1. Live `data_interp` (DI) questions with cluster size 1 -- ETS blueprint expects ~3 Qs per chart.
2. Live `rc_multi` questions with `stimulus_id IS NULL` (orphaned siblings).

Only **live** rows matter for session assembly; retired/draft rows are reported for completeness but not touched.

## 1. Starting state (before backfill)

| DB | total_q | total_stim | DI live cluster hist | rc_multi live orphans |
|---|---:|---:|---|---:|
| main | 3735 | 967 | `{1: 45}` | 45 |
| princeton | 3735 | 967 | `{1: 45}` | 45 |
| kaplan | 3956 | 984 | `{1: 45}` | 45 |
| synthetic | 3735 | 967 | `{1: 45}` | 45 |

**Key finding: all four DBs share the same pre-existing anomaly.** The Princeton / synthetic worktree DBs are unchanged snapshots of main; the Kaplan worktree DB adds +221 persisted `kaplan_2024` rows, one of which is also an rc_multi orphan (draft status, not yet live, left alone for the Kaplan agent).

## 2. DI classification

All 45 live DI items are `ai_generated`. Each question was synthesized with its own unique chart, so the 'cluster size 1' state is **not** an extractor dedup bug -- it is a **generator-pipeline gap** (`_generate_questions.py` emits one chart per question). Recovery by regrouping from existing data is impossible; the charts are genuinely unique.

**Deterministic dedup exception:** an SHA-1 scan over the base64 image payload finds exactly one pair of stimuli encoding the same chart image:

- stimuli 442 and 446 (title: 'Quarterly Revenue 2023') -- differ only in caption text. Questions 1852 & 1856 are merged onto the canonical (lowest id) stimulus.

All other AI-gen DI items remain as legitimate size-1 clusters. They are **serviceable**: each has a valid chart + answer + explanation. They just do not conform to the ETS 3-per-cluster shape. See follow-ups.

## 3. rc_multi orphan classification

All 45 live rc_multi orphans come from `manhattan_5lb_2018`. Inspecting prompts shows they were **misclassified at import**: the text is in almost every case a pure quant multi-select problem, not a passage-based verbal question. There is no passage in the DB to link them to.

Per spec ("If no -> retire"), all 45 were retired. Sub-categorization (main DB):

| Category | Count | Example IDs |
|---|---:|---|
| retire_empty | 1 | 3232 |
| retire_needs_figure | 6 | 3532, 3535, 3536, 3539, 3615 |
| self_contained | 38 | 2955, 2980, 2993, 3000, 3004 |

- **retire_empty (1)**: id 3232 has an empty `prompt` field (explanation survived but question is unusable).
- **retire_needs_figure (6)**: prompts explicitly reference "the graph above", "box-and-whisker plot shown", "which two towns", etc. The referenced figure does not exist in the DB; unanswerable. IDs: 3532, 3535, 3536, 3539, 3615, 3626.
- **self_contained (38)**: self-describing quant math problems (`If x^2 = y^2`, `n is divisible by 14 and 3`, word problems) that lost their `measure` classification on import. Retired to remove them from live verbal selection; see follow-ups for possible reclassification.

## 4. Backfill plan (per DB)

Identical across all 4 DBs (since they share the same base data):

- retire 45 live `rc_multi` orphans (1 empty + 6 figure-referencing + 38 self-contained)
- merge 1 DI question (q1856) onto canonical stimulus 442 (from dup stim 446) -- produces the only recoverable 2-Q DI cluster
- delete now-unreferenced stim 446

Total row touches per DB: 45 status updates + 1 stimulus_id update + 1 stimulus delete = 47 rows out of 3735 (1.26%) -- well under the 30% stop-condition threshold.

## 5. Post-apply state

| DB | questions_live | questions_retired | stimulus_count | DI live cluster hist | rc_multi live orphans |
|---|---:|---:|---:|---|---:|
| main | 2358 | 1377 | 966 | `{1: 43, 2: 1}` | 0 |
| princeton | 2358 | 1377 | 966 | `{1: 43, 2: 1}` | 0 |
| kaplan | 2545 | 1377 | 983 | `{1: 43, 2: 1}` | 0 |
| synthetic | 2358 | 1377 | 966 | `{1: 43, 2: 1}` | 0 |

## 6. Backups

- `/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-afcf77e0/data/audits/gre_mock.db.20260428T203649Z.bak`
- `/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-afcf77e0/data/audits/gre_mock.db.20260428T203654Z.bak`
- `/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-afcf77e0/data/audits/gre_mock.db.20260428T203658Z.bak`
- `/Users/chiku/Documents/side_projects/gre_with_ai/.claude/worktrees/agent-afcf77e0/data/audits/gre_mock.db.20260428T203715Z.bak`

## 7. Verification vs ETS blueprint

- DI cluster size: 43 size-1 clusters + 1 size-2 cluster. Still short of the ETS 3-per-cluster ideal, but that gap is structural to the generator, not the DB. A separate DI-regeneration pass (outside this task) is required to achieve true clusters.
- rc_multi live orphan count: **0** across all 4 DBs. Target met.

## 8. Follow-ups (for human triage)

1. **DI regeneration**: modify `scripts/_generate_questions.py` (or a new `generate_di_clusters.py`) to emit **clusters** of 3 questions per chart rather than 1 question per chart. Current 44 DI clusters (43 size-1 + 1 size-2) cover only ~15 of the ideal ETS count of 3+ clusters-per-quant-section across two sections; the bank is effectively depleted for DI-heavy practice sets.

2. **Reclassify retired self-contained orphans**: the 38 math problems retired in step 3 (IDs 2955, 2980, 2993, 3000, 3004, 3009, 3011, 3021, 3022, 3100, 3101, 3108, 3123, 3124, 3205, 3218, 3231, 3270, 3277, 3301, 3303, 3407, 3453, 3454, 3455, 3458, 3491, 3501, 3503, 3514, 3541, 3730, 3738, 3745, 3771, 3773, 3820, 3856) are valid quant `mcq_multi` content. A human could flip `measure: verbal -> quant`, `subtype: rc_multi -> mcq_multi`, clear `stimulus_id`, and restore `status: live`. That yields +38 quant multi-select items with no extraction work. Not done here because the spec was "backfill stimulus OR retire"; reclassification is a distinct decision.

3. **Kaplan draft orphan 3865**: rc_multi about Orwell (part of cluster 3864/3865/3866) with no passage in the DB. The Kaplan extraction agent should import the missing passage or retire the draft cluster.

4. **Broader orphan pattern**: while scoping this audit, I noticed many `rc_single`, `numeric_entry`, and `mcq_single` rows also have null `stimulus_id` (1055 mcq_single, 330 numeric_entry, 57 rc_single in main DB). Most appear to be standalone questions that never needed a stimulus, but a proper sweep would distinguish "genuinely self-contained" from "lost link to a shared stem". Out of scope here.
