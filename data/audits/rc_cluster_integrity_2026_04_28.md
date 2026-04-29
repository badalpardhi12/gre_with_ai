# RC cluster integrity audit — 2026-04-29T15:57:13Z
DB: `data/gre_user.db`

## 1. Duplicate stimuli (same passage text in multiple rows)

**Total duplicate-content groups: 10**

| copies | stimulus ids | snippet |
|-------:|--------------|---------|
| 5 | 291,292,293,294,295 | Scandals involving authors of memoirs have raised questions about how much artis |
| 3 | 580,600,605 | <p style="text-align:center; font-style:italic; color:#a0a0a0; margin-top:6px;"> |
| 3 | 284,285,286 | It is a scientific fact that water is among the few substances that expand when  |
| 2 | 296,979 | Little is known about the elusive section of the earth’s atmosphere known as the |
| 2 | 280,281 | Most literature is written such that the order of reading is unambiguous. Indeed |
| 2 | 289,290 | The Battle of Trafalgar in 1805 was, perhaps, the most important British naval v |
| 2 | 303,1008 | The controversial concept of terraforming, or changing a planet’s atmosphere to  |
| 2 | 282,283 | The first recorded example of what we would recognize as organized religion was  |
| 2 | 693,695 | The passage is about birds. |
| 2 | 302,1005 | The wombat is a muscular quadruped, about 3 feet in length with a short tail. Th |

## 2. Stimuli containing cluster markers ("Questions N-M are based on...")

**Total: 11**

### qcount=2 (3 stimuli)
- stim 1041: <b>Questions 1 and 2 are based on the passage below.</b> <p class="tx2"><span class="grey">Recent ad
- stim 1044: <b>Questions 15 and 16 are based on the passage below.</b> <p class="tx2">In the decades leading up 
- stim 1050: <b>Questions 19 and 20 are based on the passage below.</b> <p class="tx2">At the atomic scale, all m

### qcount=3 (5 stimuli)
- stim 1039: <b>Questions 4–6 are based on the passage below.</b> <p class="tx2">The problematic relationship bet
- stim 1042: <b>Questions 3–5 are based on the passage below.</b> <p class="tx2">Although it is an imperfect mode
- stim 1048: <b>Questions 8–10 are based on the passage below.</b> <p class="tx2">Toward the end of the 19th cent
- stim 1052: <b>Questions 15–17 are based on the passage below.</b> <p class="tx2">Kleptoplasty (from the Greek <
- stim 1053: <b>Questions 18–20 are based on the passage below.</b> <p class="tx2">John Finnis developed his theo

### qcount=4 (3 stimuli)
- stim 1043: <b>Questions 7–10 are based on the passage below.</b> <p class="tx2">It has been commonly accepted f
- stim 1049: <b>Questions 15–18 are based on the passage below.</b> <p class="tx2">There is an anthropological th
- stim 1051: <b>Questions 7–10 are based on the passage below.</b> <p class="tx2">The origins of the English lang

## 3. RC orphan questions (stimulus_id IS NULL, status=live)

| subtype | count |
|---------|------:|
| rc_multi | 45 |
| rc_single | 3 |

### Classification

Spot-checking the 48 orphans reveals that none are genuine RC items —
they are all mis-tagged:

- `rc_multi` orphans are quant "select all that apply" questions
  (prompts contain LaTeX like `\(x^2 = y^2\)`, inequalities, etc.)
- `rc_single` orphans are Sentence Equivalence / Text Completion items
  (prompts contain fill-in blanks `_________`)

None reference a passage or author. Fixing their `measure`/`subtype`
is out of scope for this ticket, but flagged for a follow-up subtype
repair job.

## 4. Max Planck passage (user's reported case)

| qid | subtype | status | stim_id | prompt snippet | stim snippet |
|----:|---------|--------|--------:|----------------|--------------|
| 4917 | rc_select_passage | live | 1048 | Select the sentence that best describes the importance of Max Planck’s | <b>Questions 8–10 are based on the passage below.</b> <p class="tx2">T |
| 4918 | rc_single | draft | 1048 | Which of the following would best paraphrase the opening sentence? | <b>Questions 8–10 are based on the passage below.</b> <p class="tx2">T |
| 4919 | rc_single | draft | 1048 | Which of the following best describes the relationship between the hig | <b>Questions 8–10 are based on the passage below.</b> <p class="tx2">T |

**Diagnosis:** stim 1048 has the right sibling count (3) but only qid
4917 is `live` — 4918/4919 are still `draft`. The user sees one
question paired with a passage whose header says "Questions 8–10".
This is **Case C** (solo-effective with a misleading cluster header).
Remediation: strip the header so the passage reads as a clean
standalone; the underlying sibling attachment is already correct and
will re-materialize naturally once the draft siblings are promoted.

## 5. Cluster size histogram (questions per passage stimulus, live)

| qcount | #stimuli |
|-------:|---------:|
| 0 | 332 |
| 1 | 448 |
| 2 | 49 |
| 3 | 27 |
| 4 | 5 |
| 5 | 5 |
| 7 | 4 |

## Summary of remediation plan

- **Case A (duplicate stimuli)**: 10 groups, 15 stimuli to delete,
  15 questions to relink to canonical (oldest) stimulus. All the
  duplicates are linked to retired questions, so the live-exam
  histogram won't change — the dedup is bookkeeping hygiene.
- **Case B (null-stimulus RC orphans)**: 0 actionable. All 48
  orphans are misclassified subtypes; leave subtype repair to a
  separate job.
- **Case C (solo cluster-marker strip)**: 8 stimuli have a "Questions
  N–M" header but live question count < marker span. Strip the
  header. Leave 5 intact clusters alone (qcount already matches
  marker).

---

## Remediation applied — 2026-04-29T16:01:50Z

Ran `scripts/fix_rc_cluster_integrity.py` against `data/gre_user.db`
(pre-run snapshot at `data/gre_user.db.pre-rc-integrity.bak`,
gitignored).

### Case A — dedupe_stimuli
- groups merged: 10
- stimuli deleted: 15
- questions relinked: 15

| canonical | duplicates deleted | questions relinked |
|----------:|--------------------|-------------------:|
| 280 | [281] | 1 |
| 282 | [283] | 1 |
| 284 | [285, 286] | 1 |
| 289 | [290] | 1 |
| 291 | [292, 293, 294, 295] | 4 |
| 296 | [979] | 2 |
| 302 | [1005] | 2 |
| 303 | [1008] | 2 |
| 580 | [600, 605] | 0 |
| 693 | [695] | 1 |

### Case B — relink_orphans
- candidates examined: 48
- relinked: 0 (no deterministic matches available)
- left as genuine-RC orphan: 0
- misclassified (quant/SE/TC mis-tagged as rc_*): 48

These 48 questions need a subtype-repair pass (out of scope for this
ticket). None of them actually present as RC in the current exam
flows, so the user impact is already bounded.

### Case C — strip_cluster_marker
- examined: 13
- stripped: 8
- preserved (intact cluster): 5

Stripped (live qcount < marker span):
- stim 1039 (live=0, span=3)
- stim 1041 (live=0, span=2)
- stim 1043 (live=1, span=4)
- stim 1044 (live=1, span=2)
- stim 1048 (live=1, span=3)  ← **user-reported Max Planck case**
- stim 1049 (live=3, span=4)
- stim 1050 (live=1, span=2)
- stim 1051 (live=2, span=4)

Preserved (marker matches live qcount — intact cluster):
- stim 1042 (live=3, span=3)
- stim 1046 (live=1, span=1)
- stim 1047 (live=1, span=1)
- stim 1052 (live=3, span=3)
- stim 1053 (live=3, span=3)

### Cluster-size histogram before vs after

| qcount | before | after |
|-------:|-------:|------:|
| 0 | 332 | 317 |
| 1 | 448 | 448 |
| 2 | 49 | 49 |
| 3 | 27 | 27 |
| 4 | 5 | 5 |
| 5 | 5 | 5 |
| 7 | 4 | 4 |

Passage stimulus rows: 870 → 855. The qcount=0 bucket dropped by 15
(all 15 deleted duplicates were orphaned from a live-question
perspective — they only had retired or draft children). The live
buckets (qcount=1..7) are unchanged, confirming no user-visible
question disappeared.

### Max Planck-specific verification

```sql
SELECT q.id, q.subtype, q.status, q.stimulus_id
FROM question q JOIN stimulus s ON s.id = q.stimulus_id
WHERE s.content LIKE '%Max Planck%';
```
Result after remediation:

| qid | subtype | status | stim_id |
|----:|---------|--------|--------:|
| 4917 | rc_select_passage | live | 1048 |
| 4918 | rc_single | draft | 1048 |
| 4919 | rc_single | draft | 1048 |

All three questions are now attached to a single canonical stimulus
(1048), and the stimulus content no longer begins with
`<b>Questions 8–10 are based on the passage below.</b>`. When the
user encounters qid 4917 as a standalone item in Verbal 1, the
passage renders as a clean self-contained paragraph. If qids 4918
and 4919 are promoted from draft in a later sweep, they will
automatically form a 3-question cluster on the same stim — no
further schema change needed.

### Idempotency

A second run of the script (dry-run) reports 0 groups merged and 0
markers to strip; the surviving 5 marker-bearing stimuli are all
legitimate intact clusters. Safe to re-run.

### Outstanding items (not fixed by this pass)

- **48 misclassified RC orphans** (45 `rc_multi`, 3 `rc_single`).
  These are quant "select all that apply" and SE/TC fill-in-blank
  questions that somehow received an `rc_*` subtype label. Fixing
  their subtype requires checking the source prompt shape and
  reassigning to `quant`/`se`/`tc` — handle in a separate ticket.
- **Draft siblings** under cluster stimuli 1043, 1044, 1048, 1049,
  1050, 1051. These are legitimate Kaplan RC questions sitting in
  draft; the cluster text (minus the header) is consistent with them.
  A future promotion pass can move them to `live`, which will
  re-form the intended N-question clusters.

