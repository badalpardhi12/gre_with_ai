# Scoring Mechanism

This document explains how the GRE Mock Testing Platform turns a stream of user
responses into grades, scaled scores, adaptive routing decisions, and
long-term forecasts.

There are **four independent scoring subsystems**. They run side by side and
feed different parts of the UI:

1. [Per-question grading](#1-per-question-grading) — `services/scoring.py`
2. [End-of-section scaled score (130–170)](#2-end-of-section-scaled-score-130170) — `services/scoring.py`
3. [Adaptive routing & forecasting](#3-adaptive-routing--forecasting) — `services/rating_service.py`, `services/score_forecast.py`
4. [AWA (essay) scoring](#4-awa-essay-scoring) — `services/awa_scorer.py`

A short [runtime flow](#runtime-flow) at the end shows how they connect.

---

## 1. Per-question grading

**File:** `services/scoring.py` — `ScoringEngine.check_answer(question_data, user_response)`

This is a pure, deterministic function that returns a single `bool`. It
dispatches by `question_data["subtype"]`:

| Subtype | Rule |
|---|---|
| `mcq_single`, `qc`, `rc_single`, `data_interp` | Exactly one selected label must match the option flagged `is_correct`. |
| `mcq_multi`, `rc_multi` | All-or-nothing set equality between selected labels and correct labels — no partial credit. |
| `se` (sentence equivalence) | Exactly two correct answers required; both must be picked. Logs a warning if the item doesn't have exactly 2 correct options. |
| `tc` (text completion) | Every blank must be right. Options are grouped per-blank by `normalize_tc_options`, which understands three label conventions. |
| `rc_select_passage` | The selected sentence index must equal the correct option's label. |
| `numeric_entry` | Compared in `Fraction` space so `2.5 == 2.50 == 5/2`. |

Anything else returns `False`.

### Text completion label conventions

`normalize_tc_options` is shared by the UI (`screens/question_screen.py`) and
the scorer so they stay in lock-step. It accepts:

1. **Explicit prefix** (`blank1_A`, `blank2_C`) — split on `_`. Authoritative.
2. **Flat 6 / 9 one-letter labels** for multi-blank TC. The authoring
   convention groups consecutive letters into blanks of three:
   `A/B/C → blank1`, `D/E/F → blank2`, `G/H/I → blank3`. The per-blank choice
   letter is renamed to `A/B/C` so each blank shows "A) … B) … C) …" in the
   UI instead of mid-alphabet starts.
3. **Flat labels otherwise** (5-option A–E etc.) — single-blank TC. All
   options go under `blank1` with their original letter.

Earlier versions only handled case 1, which silently marked ~93 single-blank
TC items wrong. A later fix added case 3, and the current code adds case 2
(GitHub #15, Q5257 + 15 other multi-blank flat-label items).

### Numeric entry

- Accepts either `value` (string/decimal/fraction) **or** explicit
  `numerator` / `denominator` from the user.
- Comparison happens in `fractions.Fraction` space to avoid float-precision
  artifacts at tolerance boundaries (e.g. `Fraction('1.05')` as a float is
  `1.0500000000000000444…`).
- If the question carries `tolerance > 0`, the comparison is
  `abs(user − correct) ≤ tolerance` (also in `Fraction`).
- NaN/Inf inputs and malformed numerators/denominators are rejected.
- Defends against malformed DB rows: a question with neither `exact_value`
  nor `numerator`/`denominator` logs a warning and grades as wrong.

---

## 2. End-of-section scaled score (130–170)

**File:** `services/scoring.py` — `ScoringEngine.estimate_scaled_score`,
`compute_session_scores`

A coarse static lookup, used after a mock section completes.

### How the table is built

`_build_score_table()` runs once at import time and produces a table for each
of three S2 difficulty bands — `easy`, `medium`, `hard`. For each raw count
0–27 (S1's 12 questions + S2's 15) it computes a base scaled score:

| Band | Formula | Approx cap |
|---|---|---|
| easy | `130 + pct * 25` | ~155 |
| medium | `135 + pct * 30` | ~165 |
| hard | `140 + pct * 30` | 170 |

where `pct = raw / 27`. The returned band is
`(floor(base) − 2, ceil(base) + 2)`, clamped to `[130, 170]`.

### Public API

- `estimate_scaled_score(raw_correct, difficulty_band="medium") -> (low, high)`
- `compute_session_scores(verbal_raw, verbal_band, quant_raw, quant_band)`
  packages four numbers (verbal/quant raw + low/high) into a dict for the
  results screen.

The module's own docstring is candid that **this is an approximation** — the
real ETS formula isn't public. The IRT path in
[§3.2](#32-irt-theta--ets-concordance--score_forecastpy) is the calibrated
forecast.

---

## 3. Adaptive routing & forecasting

These don't grade individual questions. They convert the stream of pass/fail
bools into a user-ability estimate (theta, on a logit scale) used for
between-section S2 band selection and the long-term score forecast.

### 3.1 Elo item rating — `services/rating_service.py`

Every live question has an `ItemRating` row (`rating`, `n_responses`).
Initial ratings are seeded from `Question.difficulty_target` 1–5:

| difficulty_target | Seed rating (logits) |
|---|---|
| 1 | −1.2 |
| 2 | −0.6 |
| 3 |  0.0 |
| 4 | +0.6 |
| 5 | +1.2 |

After each graded `Response`, `update_on_response` runs a classic Elo update
**against the item only** (user-side rating updates are deferred until the
real IRT tracker lands):

```
E   = 1 / (1 + 10 ** ((item_rating − user_theta) / 0.4))
new = item_rating + K * (E − actual)              # actual ∈ {0, 1}
K   = max(K_MIN, K_INITIAL * K_DECAY_PIVOT / (K_DECAY_PIVOT + n_responses))
```

Tunables (`services/rating_service.py`):

| Constant | Value | Meaning |
|---|---|---|
| `K_INITIAL` | 0.3 | Starting K for an uncalibrated item. |
| `K_MIN` | 0.05 | Floor — items keep moving even after many responses. |
| `K_DECAY_PIVOT` | 40 | At n ≈ 40 responses K is ~half of K_INITIAL. |
| `THETA_SCALE` | 0.4 | One `difficulty_target` band ≈ one logit of expected-score swing. |

#### `get_user_theta(window=40)`

The user's theta is recomputed on demand from the **last 40 graded
responses**. For each response, the "theta signal" is `+rating` if the user
got the item right and `−rating` if they got it wrong; ratings are clipped
to ±3 so a wildly drifted item can't dominate the average. Returns `0.0`
when there's no graded history.

The `user_id` parameter is reserved for a future multi-user rollout — today
the app is single-user and `get_user_theta` simply takes the last `window`
responses regardless of `user_id`.

#### `services.scoring.compute_theta` facade

A thin wrapper around `get_user_theta` used by `models/exam_session` to
drive S2 section-level adaptive item selection. It swallows import failures
and non-finite values, falling back to `0.0` so section CAT routing can
never crash on a fresh clone or rating-service import error.

### 3.2 IRT theta + ETS concordance — `services/score_forecast.py`

The preferred forecast path once enough items have IRT estimates.

#### Step 1 — fit user theta per measure

`_theta_from_irt(measure)` runs a **2-parameter logistic (2PL) MLE** against
all of the user's graded responses on items that have both
`Question.irt_a_estimate` and `Question.irt_b_estimate`.

- Bisects the score equation `Σᵢ aᵢ (yᵢ − pᵢ(θ)) = 0` on `[−6, +6]`. The
  score is monotone-decreasing in θ (derivative is `−Σ aᵢ² · pᵢ(1−pᵢ) ≤ 0`),
  so there is exactly one root.
- Discrimination `a` is clamped to `[0.2, 2.5]` and difficulty `b` to
  `[−4, +4]` to defend against wild Phase-1 girth output on sparse items.
- Edge cases: all-correct → `+3`, all-wrong → `−3` (the score equation has
  no finite root in those cases).

#### Step 2 — map theta to scaled score

`predict_from_theta(theta, measure)` is a piecewise-linear interpolation
between published ETS concordance anchors. Out-of-range theta is clamped to
the endpoints; output is rounded and clamped to `[130, 170]`.

| theta | Quant scaled | Verbal scaled |
|---|---|---|
| −3 | 130 | 130 |
| −2 | 140 | 140 |
| −1 | 150 | 150 |
|  0 | 155 | 155 |
| +1 | 163 | 162 |
| +2 | 167 | 165 |
| +3 | 170 | 169 |

Verbal compresses earlier above θ = +1 because the ETS Verbal concordance
does.

#### Step 3 — choose the path

`predict_composite(user_id="local")` decides which path to use:

1. Pull `rating_service.get_user_theta()`. If it raises, treat as `None`.
2. For each of `quant`, `verbal`, check coverage: the user must have at
   least `_MIN_ATTEMPTS = 10` graded responses **and** `_IRT_COVERAGE_FLOOR
   = 20%` of those responses must be on items with IRT estimates.
3. Use the IRT path **iff** rating-theta is non-`None` and at least one
   measure has good coverage. If a measure has no IRT coverage, the
   rating-service theta is blended in so the forecast still emits a
   number rather than a hole.
4. Otherwise fall back to the legacy logistic path (below).

The result dict always includes a `"method"` key:
`"irt_theta"` | `"legacy_logistic"` | `"insufficient_data"`.

#### Legacy logistic fallback

`predict_scaled_score(measure)` and `measure_accuracy_by_difficulty(measure)`
implement the pre-Phase-2 heuristic that the IRT path replaces. It buckets
responses by `difficulty_target`, weights accuracy by band difficulty
(1→0.6, 2→0.8, 3→1.0, 4→1.3, 5→1.6), maps weighted-% to a fixed center
score (e.g. ≥0.95 → 169, ≥0.85 → 165, …, <0.35 → 138), and adds a
confidence spread that narrows as the attempt count grows (±5 / ±4 / ±3 /
±2 at 0/25/50/100 attempts).

`overall_forecast` and `forecast_history` build on top of these for the
Insights and Today screens.

---

## 4. AWA (essay) scoring

**File:** `services/awa_scorer.py`

Free-response essays don't go through `ScoringEngine`. They're graded by an
LLM via the OpenRouter gateway in `services/llm_service.py` (default model
`anthropic/claude-opus-4`). The pipeline is:

1. **Deterministic prechecks** — word count (`AWA_MIN_WORDS` /
   `AWA_MAX_WORDS` from `config.py`), off-topic detection, repetition.
2. **LLM primary grader** — emits per-dimension subscores anchored to
   ETS-style sample essays (score 6/5/4/3) embedded in the prompt.
3. **Optional self-consistency second pass** — feature-flagged via the
   module-level `USE_SECOND_PASS` flag. May adjust any one dimension by at
   most `MAX_SECOND_PASS_DELTA = ±0.5` to prevent wholesale rewriting of
   the grade.
4. **Prompt-injection mitigation** and **score-confidence reporting**.

The result dict preserves the legacy keys consumed by `main_frame.py`
(`score_estimate`, `score_confidence_low`, `score_confidence_high`,
`dimensions`, `summary`) and adds the calibrated fields (`overall_score`,
`subscores`, `holistic_notes`). The four "subscore" axes are
`analysis`, `structure`, `support`, `conventions`.

---

## Runtime flow

How the four subsystems connect during a mock:

1. User submits an answer →
   `ScoringEngine.check_answer` returns a `bool` →
   a `Response` row is written →
   `rating_service.update_on_response` fires the Elo update on the item.
2. Between S1 and S2 of a mock, `services.scoring.compute_theta()` reads
   `rating_service.get_user_theta()` to drive the S2 difficulty band.
3. After the section completes, `compute_session_scores` produces the
   displayed (low, high) raw-count band shown on the results screen.
4. The Insights and Today screens call `score_forecast.predict_composite`
   for the longer-term forecast (IRT-theta path when coverage is good,
   legacy weighted-accuracy path otherwise).
5. AWA prompts run through `awa_scorer.score_essay` independently of the
   above and contribute their own subscores to the writing report.

---

## File map

| File | Role |
|---|---|
| `services/scoring.py` | Per-question grading, scaled-score lookup, theta facade |
| `services/rating_service.py` | Elo item ratings, user-theta estimator |
| `services/score_forecast.py` | IRT 2PL fit, ETS concordance, composite forecast, legacy fallback |
| `services/awa_scorer.py` | Multi-signal LLM essay grader |
| `services/llm_service.py` | OpenRouter gateway used by the AWA grader |
| `screens/question_screen.py` | UI side of `normalize_tc_options` |
| `models/exam_session` | Calls `compute_theta` for between-section CAT routing |
