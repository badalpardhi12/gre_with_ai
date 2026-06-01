# Production Hardening — WS0 Findings & Canonical Worklists (2026-06-01)

Branch: `production-hardening`. DB backups: `data/audits/backups/*.20260601_095356.bak`.

This document is the authoritative output of WS0 (definitive audits). It pins the
exact worklists consumed by WS A/B/C and records the detection logic so the
audits stay trustworthy.

## New audits

- `scripts/audit_option_graft.py` — detects the option-graft corruption class
  that `audit_answer_key_drift.py` is structurally blind to. Two high-precision
  signals (earlier "any shared option-set" / "option tokens absent from
  explanation" drafts were discarded as hopelessly noisy):
  - **provenance_divergence** (mcq_multi only): numeric tokens in the immutable
    `provenance_json.judge_result` rationales (the ORIGINAL options) barely
    intersect the currently-stored option tokens. Reliable for mcq_multi because
    the judge enumerates both correct AND wrong values; unreliable for
    single-answer / DI items, so restricted.
  - **distinctive_shared_set**: distinct questions sharing an identical option
    set that is distinctive (fractions/ratios/LaTeX/text — NOT plain integers).
    Verbal subtypes excluded (their shared sets are duplicate ingestions → WS C).
- `scripts/audit_figure_render.py` — replicates the renderer's resolution logic
  (renders `stimulus.content` only) to classify every figure-bearing item:
  RENDERS / PHANTOM_SPEC_WIRED / PHANTOM_SPEC_ASSET / PHANTOM_CAPTION_ONLY /
  PROMPT_FIGURE_NONE. RC passages mis-typed as `graph` are excluded (they carry
  full passage text and render fine).

## WS A worklist — option grafts (15 high-confidence mcq_multi)

All `ai_synthetic_v2` except where noted. Repair = reconstruct correct options +
`is_correct` from `provenance_json` + explanation, then LLM re-validate.

- **LIVE (4):** 5375, 5378, 5384, 5394
  - 5375 — options are bare numbers; prompt demands "must be true" *statements*. GRAFT.
  - 5378 — k∈{1..8} expected; options `−40,−30,0,8,20,48,60`. GRAFT.
  - 5384 — "total number of chips" expected; options are probabilities. GRAFT.
  - 5394 — ratio options `1:2…` MATCH prompt+explanation → likely the NATIVE owner
    (5374 is the graft victim). Expect LLM verdict: no repair needed.
- **RETIRED (9):** 5374, 5376, 5377, 5380, 5381, 5382, 5386, 5388, 5389 — repair for
  past-test-review integrity (user still sees them in completed-session review).
- **OTHER (2, non-live):** 3863 (princeton, retired), 4808 (princeton, draft).

**Review tier (76, mcq_single):** mostly coincidental shared LaTeX-integer/fraction
option sets (false positives) + the legit shared menu scenario (5401/5403) +
cross-ingestion `imported`/`princeton_2012` duplicate pairs (→ WS C). Do NOT blind-
repair; LLM spot-check the live ones; route true dupes to WS C. Note also q5400
(live, DI mcq_single): explanation is self-inconsistent ("Re-check…", lands on a
different value than the marked answer) — a quality bug for WS A/D validation, not a
graft.

## WS B worklist — figures

Decision: generate real figures + wire `render_spec` through the renderer.

- **LIVE phantom (7):**
  - Generate real figures (reconstruct from `render_spec.spec`, inline base64): 5121
    (triangle), 5170 (triangle), 5172 (circle), 5173 (polygon), 5180 (coordinate),
    5181 (triangle).
  - Retire: 4252 (princeton mcq_multi, no stimulus, prompt needs a figure, nothing
    recoverable).
- **Also reconstructable (draft/retired, 45 total ai_synthetic SPEC_WIRED):** fix in the
  same generator pass so promotion is safe.
- **PHANTOM_CAPTION_ONLY (58, princeton, retired) / PROMPT_FIGURE_NONE (54):** retired/
  draft; retire or leave — not live, lower priority. Confirm none are live before close.

## WS E note (taxonomy)

- q4906, q4907 (kaplan_2024 rc_single): `stimulus_type='graph'` but content is the RC
  passage → reclassify to `passage`. Renders fine today; cosmetic/taxonomy only.

## Baselines (existing audits, from inventory pass)

live=2,586; retired=1,929; draft=761. Sources (live): manhattan 1,156 / ai_generated 721 /
princeton 362 / ai_synthetic 180 / kaplan 121 / ai_synthetic_v2 46. Known debt: taxonomy
137 (121 Kaplan), encoding 37 (34 Manhattan), structural-validator 74, answer_key_drift
10 retires.

---

## Outcome (shipped — migrations 039–043)

- **WS-A** (039): repaired 12 option-grafted `mcq_multi` items from provenance, retired 1
  unrecoverable. Live grafts: 0.
- **WS-B** (040 + renderer): `render_spec` geometry now renders via
  `services/figures/geometry.py`, wired into `question_bank.get_question`. Live phantom
  figures: 0.
- **WS-C** (041): `duplicate_group_id` + assembly group-dedup + cross-mock window N=3;
  retired 4 exact dupes. In-mock duplicate co-occurrence: 0 over 40 trials.
- **WS-D** (042): 84 QC items normalized to canonical ETS text. Shape violations: 0.
- **WS-E** (043): 10 explanations' inline-`$` math converted to `\(…\)`; Kaplan stimulus
  reclassified. Taxonomy backfill DEFERRED (run `scripts/llm_judge_taxonomy.py`).
- **WS-F**: `scripts/run_all_audits.py` aggregate gate + `tests/test_production_gate.py`.

## Pipeline hazard — DO NOT reintroduce (root cause of the WS-A graft)

The option-graft corruption came from mirroring synthetic content into the seed in **two
passes** (commit `0628858`): `questionoption`/`numericanswer`/`stimulus` rows were copied in
one pass and the `question` rows re-keyed in another, so `questionoption.question_id`
pointed at the wrong (neighboring) stem. **Any future seed mirroring MUST copy
`question` + `questionoption` + `numericanswer` + `stimulus` atomically under a single id
map (one transaction), never option-rows-first / questions-later.** The aggregate gate
(`scripts/run_all_audits.py`) and `services/seed_sync.py` invariants are the backstop, but
the atomic-mirror discipline is the real prevention.

## Documented follow-ups (not shipped)

- Taxonomy backfill: 137 live items (121 Kaplan carry chapter/practice-set labels spanning
  multiple canonical subtopics). Run `venv/bin/python scripts/llm_judge_taxonomy.py`
  (derives `question_type` deterministically, LLM only for topic/subtopic against the
  taxonomy allowlist, dual-writes both DBs). Internal routing metadata only — no
  per-item correctness impact.
- Encoding: 16 `unescaped_html` items are markdown tables in DI prompts; need a
  markdown→HTML table pass (left as-is to avoid risky transforms). Remaining
  `unmatched_dollar` are inert literal currency `$`, not a render bug.
- Pre-existing, unrelated test failure: `tests/test_minhash_dedup.py::
  test_held_out_detection_f1_at_persisted_threshold` (ML LSH F1 threshold over dedup eval
  data) — fails identically on `main`; not touched by this work.

