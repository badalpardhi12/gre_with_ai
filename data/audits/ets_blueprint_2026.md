# ETS GRE General Test Blueprint — Authoritative Findings

Research conducted 2026-04-27 for the short-form (post-September 22 2023) GRE
General Test. Primary ETS URLs (ets.org) refuse unauthenticated `WebFetch`
(HTTP 403), so findings are assembled from ETS-quoted snippets surfaced by
Brave search plus several independent test-prep publishers that quote ETS
directly. All numbers below are either ETS-stated or appear in at least two
independent prep sources with no disagreement.

## 1. Overall structure

| Section         | Items | Time   | Notes                                    |
| --------------- | ----: | -----: | ---------------------------------------- |
| AWA (Analyze an Issue) | 1 | 30 min | First section, always. Essay.            |
| Verbal S1       | 12    | 18 min | Moderate mix, determines S2 difficulty.  |
| Verbal S2       | 15    | 23 min | Adaptive to S1 (easy / medium / hard).   |
| Quant S1        | 12    | 21 min | Moderate mix, determines S2 difficulty.  |
| Quant S2        | 15    | 26 min | Adaptive to S1.                          |
| **Total**       | 55    | 1 h 58 min | Verbal & Quant order randomized after AWA. |

Sources: ETS test-structure page (quoted by Brave: ets.org/gre/test-takers/
general-test/prepare/test-structure.html); Kaplan ("21 minutes to work on the
first Quantitative Reasoning section and 26 minutes to work on the second");
Leland 2026 guide ("1 hour and 58 minutes").

## 2. Section-level adaptive algorithm

- Only Verbal and Quant are adaptive (AWA is not).
- Adaptation is **section-level**, not item-level: S1 performance gates the
  **difficulty pool** that S2 is drawn from.
- Performance on S1 → 3 bands for S2: easy / medium / hard.
- The raw score incorporates both correctness and the difficulty of the S2
  section the examinee qualifies into.
- ETS does not publish exact pct-correct thresholds. Community consensus
  (Manhattan Prep, Magoosh, TTP) uses a 3-band cut around ~40% and ~70%,
  which is what the app already encodes (`ADAPT_EASY_THRESHOLD = 0.40`,
  `ADAPT_HARD_THRESHOLD = 0.70`). No authoritative challenge found; leave
  the thresholds as-is.

Sources: ETS FAQ PDF (general-test-enhancement-faqs-for-test-takers.pdf):
"Yes, the Verbal Reasoning and Quantitative Reasoning measures are
section-level adaptive"; BoosterPrep; Leland.

## 3. Verbal section composition (per section)

| Subtype                         | 12-item S1 | 15-item S2 |
| ------------------------------- | ---------: | ---------: |
| Reading Comprehension (total)   | 5-6        | 7          |
| of which long passage (multi-Q) | 1-2        | 1-2        |
| of which short / argument       | 3-4        | 5-6        |
| Text Completion (1/2/3 blank)   | 3-4        | 4          |
| Sentence Equivalence            | 3          | 4          |

Proportions (half RC, quarter TC, quarter SE) are quoted by ETS via Magoosh
("Text completion and sentence equivalence questions each account for about
a quarter of the verbal section, with approximately 7 questions of each
type [across both sections]. Reading comprehension is the most common
question type, making up about half of the verbal reasoning questions.")
and CrackVerbal ("approximately 10 RC questions … one long passage (3-4
questions) and two to three short passages (1-2 questions each)").

**RC clusters are confined to one section.** All sub-questions on a given
passage appear together inside the same section (they share a stimulus the
examinee is reading). They never span a section boundary.

## 4. Quant section composition (per section)

| Subtype                         | 12-item S1 | 15-item S2 |
| ------------------------------- | ---------: | ---------: |
| Quantitative Comparison (QC)    | 3-4        | 4          |
| Multiple Choice, one answer     | 5          | 6-7        |
| Multiple Choice, one or more    | 1          | 1-2        |
| Numeric Entry                   | 1          | 1-2        |
| Data Interpretation (1 set)     | 3          | 3          |

Per TTP ("In the two GRE Quant sections, you can expect to see about 11
Multiple-Choice Single-Answer and 11 Quantitative Comparison questions, and
about 3 Multiple-Choice Multiple-Answer and 3 Numeric Entry questions"),
and "3 or 4 of the 27 Quantitative questions on the GRE to be Data
Interpretation." Manhattan Review corroborates 8 QC + 9 PS + 3 DI per
section typical.

**DI is delivered as a cluster of 3 questions per section**, all sharing a
single chart/table stimulus:
- CrunchPrep: "Each set contains one information source and three
  questions that follow it."
- Experts Global: "a set of three questions, with all required information
  presented through charts, graphs, or tables."
- MyPrepClub: "Data Interpretation for the GRE General Exam consists of
  three questions in both quant sections of the test."

Therefore every Quant section MUST contain exactly one DI stimulus with
exactly 3 DI questions attached. Partial DI clusters are never presented.

## 5. Difficulty mix per section

ETS does not publish a per-section easy / medium / hard ratio in closed
form. What it does publish: S1 is "moderate" / mixed difficulty by design,
and S2's **difficulty band** is adapted by S1 performance.

Operationalization used by this codebase:
- **S1 (Verbal + Quant)**: `difficulty_band = "medium"` — mixed items, but
  the central tendency is medium. No proportion constraint enforced.
- **S2**: whole-band selection — `easy` / `medium` / `hard` based on S1
  correctness. Again, no strict within-band mix.

This matches the observable adaptive behavior (ETS does not claim S2 is
"all hard"; it claims S2 is drawn from a harder pool on average). We
therefore do not attempt to enforce a three-way easy/med/hard histogram
inside a single section.

## 6. Summary of blueprint rules the assembly engine must satisfy

1. Full mock = 5 sections in order: AWA → (V,V,Q,Q) or (Q,Q,V,V).
2. Section counts: AWA=1 item, V1=12, V2=15, Q1=12, Q2=15.
3. Section times: 30, 18, 23, 21, 26 minutes.
4. Verbal section composition targets (per section, not per item):
   - RC total ≈ 50% (rc_single + rc_multi)
   - TC ≈ 25%
   - SE ≈ 25%
5. Quant section composition targets:
   - QC ≈ 33% (≥3 per 12, ≥4 per 15)
   - MCQ single ≈ 40%
   - MCQ multi ≈ 5-10% (≥1 per section)
   - Numeric entry ≈ 5-10% (≥1 per section)
   - DI = **exactly 3 items per section, all sharing one stimulus**
6. RC clusters are atomic: all questions attached to one RC stimulus are in
   the same section, with no partial inclusion.
7. DI clusters are atomic and exactly one set per quant section.
8. S2 questions must not repeat S1 questions (in-exam dedup).
9. Cross-session dedup: questions seen in the last N days should be
   avoided where pool size permits.
