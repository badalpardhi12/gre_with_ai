# GRE Prep — Local-First Desktop App

A best-in-class offline GRE preparation platform: section-adaptive mock tests, a 9,600-word spaced-repetition vocab deck, per-subtopic mastery analytics, and an optional LLM tutor that never controls your score. Every millisecond of timing, every answer key, every adaptive routing decision is deterministic and runs locally on your machine. The LLM layer is opt-in — drills, mocks, vocab, and analytics all work with no API key.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![wxPython](https://img.shields.io/badge/GUI-wxPython%204.2-orange)
![SQLite](https://img.shields.io/badge/database-SQLite-green)
![Tests](https://img.shields.io/badge/tests-533%20pass-brightgreen)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

![Today screen](docs/screenshots/today.png)

---

## Quick start

```bash
git clone https://github.com/badalpardhi12/gre_with_ai.git
cd gre_with_ai
chmod +x setup.sh && ./setup.sh        # builds venv, runs migrations, optional API-key prompt
venv/bin/python app.py                 # launches the app
```

`setup.sh` is idempotent. Re-run it after every `git pull`. An OpenRouter key is optional — skip the prompt and the app still runs every offline feature.

---

## Screenshots

| Today — home | Practice — mode picker | Error Log |
|---|---|---|
| ![Today](docs/screenshots/today.png) | ![Practice](docs/screenshots/practice.png) | ![Error Log](docs/screenshots/error_log.png) |

| Practice question (mid-session) | Learn — mastery heatmap | Insights — forecast + plan + history |
|---|---|---|
| ![Question](docs/screenshots/practice_question.png) | ![Learn](docs/screenshots/learn.png) | ![Insights](docs/screenshots/insights.png) |

| Results | Vocab (FSRS) | Onboarding wizard |
|---|---|---|
| ![Results](docs/screenshots/results.png) | ![Vocab](docs/screenshots/vocab.png) | ![Onboarding](docs/screenshots/onboarding_step_1.png) |

---

## Architecture (presentation-grade)

![Architecture diagram](docs/architecture/gre_architecture.png)

Source: [`docs/architecture/gre_architecture.py`](docs/architecture/gre_architecture.py) · PDF: [`docs/architecture/gre_architecture.pdf`](docs/architecture/gre_architecture.pdf)

### Three-layer separation

1. **Deterministic core** (`services/scoring.py`, `models/exam_session.py`, `widgets/timer.py`) — section engine, timer, answer checking, adaptive routing. Never depends on the LLM; your score is always computed from the answer key.
2. **LLM layer** (`services/llm_service.py` + friends) — AWA scoring, per-question tutor, mistake coach, study plans, explanation fallback. Gated by an OpenRouter API key; every UI surface that consumes it degrades gracefully when the key is absent.
3. **Data layer** — SQLite + Peewee, ~25 tables, on-launch idempotent migrator. The shipped seed `data/gre_mock.db` is a tracked ~22 MB blob; per-user state lives in the gitignored `data/gre_user.db` bootstrapped from the seed on first launch.

### Session flow (Mermaid)

High-level user journey from launch to results.

```mermaid
---
title: GRE app — session lifecycle
---
flowchart TB
  L[Launch]:::ui --> O{Onboarded?}:::ui
  O -- no --> W[Onboarding wizard]:::ui
  W --> T[Today tab]:::ui
  O -- yes --> T
  T --> P[Practice / Drill / Mock]:::ui
  P --> QL

  subgraph QL[Per-question loop]
    direction TB
    QS[Question screen]:::ui --> SE[ScoringEngine]:::core
    SE --> RDB[(Response row)]:::db
    SE --> MU[Update mastery EWMA]:::core
    SE --> RU[Update Elo rating]:::core
    RU --> QS
  end

  QL -- end-of-section --> SA[Section-adaptive router]:::core
  SA -- theta / accuracy --> QB[QuestionBank.select_composed]:::core
  QB -- next items --> QL
  QL -- session end --> RES[Results screen]:::ui
  RES --> IL[Error Log + Insights]:::ui

  classDef ui fill:#eef3fb,stroke:#b9c8de,color:#111;
  classDef core fill:#eaf4ec,stroke:#b9d8bd,color:#111;
  classDef db fill:#f4eefb,stroke:#c9b6e0,color:#111;
```

Error-log feedback loop — wrong answers become FSRS-scheduled review items.

```mermaid
---
title: Error log → FSRS review loop
---
flowchart TB
  W[Wrong answer in session]:::ui --> CLS[Classify mistake<br/>careless / conceptual /<br/>timing / vocab-gap]:::core
  CLS --> EL[Error Log entry]:::db
  EL --> EX{User action}:::ui
  EX -- Schedule Redo --> FSRS[FSRS scheduler<br/>services/srs.py]:::core
  EX -- Ask Tutor --> LLM[AnswerChat<br/>scope-locked tutor]:::core
  FSRS --> DUE[(ItemReview<br/>next_due)]:::db
  DUE -- due today --> T[Today tab — review queue]:::ui

  classDef ui fill:#eef3fb,stroke:#b9c8de,color:#111;
  classDef core fill:#eaf4ec,stroke:#b9d8bd,color:#111;
  classDef db fill:#f4eefb,stroke:#c9b6e0,color:#111;
```

---

## Data model (Mermaid ER)

The schema splits into two clusters: **content** (a Question and everything that describes it) and **user state** (sessions, responses, per-item ratings and reviews). Shown as two focused diagrams for readability.

### Content side — Question, Stimulus, Options

```mermaid
---
title: Content — a Question and its components
---
erDiagram
  STIMULUS ||--o{ QUESTION : "referenced by"
  QUESTION ||--o{ QUESTION_OPTION : has
  QUESTION ||--o| NUMERIC_ANSWER : has
  QUESTION ||--o{ QUESTION_FLAG : "user reports"

  QUESTION {
    int id PK
    string subtype
    string measure
    int difficulty_target
    string topic
    string subtopic
    string status "live|draft|retired|candidate"
    string source
  }
  STIMULUS {
    int id PK
    string kind "passage|figure|shared"
    text body
  }
  QUESTION_OPTION {
    int id PK
    int question_id FK
    text text
    bool is_correct
  }
  NUMERIC_ANSWER {
    int question_id PK
    float min
    float max
  }
```

### User-state side — sessions, responses, ratings, reviews

```mermaid
---
title: User state — sessions, responses, per-item rating and review
---
erDiagram
  SESSION ||--o{ SECTION_RESULT : contains
  SESSION ||--o| SCORING_RESULT : scored
  SECTION_RESULT ||--o{ RESPONSE : grades
  QUESTION ||--o{ RESPONSE : "answered by"
  QUESTION ||--o{ SERVED_LOG : exposure
  QUESTION ||--o| ITEM_RATING : elo
  QUESTION ||--o{ ITEM_REVIEW : fsrs
  QUESTION ||--o{ ITEM_STATS : aggregate
  QUESTION ||--o{ MASTERY_RECORD : "per subtopic"
  USER_STATS ||--|| SESSION : tracks
  STUDY_PLAN ||--|| USER_STATS : personal

  RESPONSE {
    int id PK
    int session_id FK
    int section_result_id FK
    int question_id FK
    bool is_correct
    int time_to_answer_ms
    datetime answered_at
  }
  SERVED_LOG {
    int id PK
    int question_id FK
    string session_id
    datetime served_at
  }
  ITEM_RATING {
    int question_id PK
    float rating "Elo, seeded from difficulty"
    int n_responses
  }
  ITEM_REVIEW {
    int user_id
    int question_id FK
    float stability
    float difficulty
    datetime next_due
  }
```

### Vocabulary (FSRS deck, separate from the Question bank)

```mermaid
---
title: Vocab — 9,647 words + roots + FSRS reviews
---
erDiagram
  VOCAB_WORD }o--o{ VOCAB_ROOT : "composed of"
  VOCAB_WORD ||--o{ FLASHCARD_REVIEW : srs

  VOCAB_WORD {
    int id PK
    string word
    string definition
    text examples
    text synonyms
    text antonyms
    string mnemonic
  }
  VOCAB_ROOT {
    int id PK
    string root
    string meaning
  }
  FLASHCARD_REVIEW {
    int id PK
    int word_id FK
    float stability
    float difficulty
    datetime next_due
  }
```

---

## Content pipeline (Mermaid sequence)

Every item in the shipped `data/gre_mock.db` came through the same five-stage extraction / generation pipeline. Each D-task has its own source-tag (`source=` column) so provenance is auditable downstream.

```mermaid
---
title: Content pipeline — from source to gre_mock.db
---
sequenceDiagram
  autonumber
  participant Src as Source<br/>(ebook / PDF / dataset)
  participant Ing as Ingest<br/>(marker-pdf / scraper)
  participant Fmt as Reformat LLM<br/>(OpenRouter)
  participant Slv as Solver<br/>(sympy, quant only)
  participant Jud as Judge<br/>(multi-model vote)
  participant Vis as Vision audit
  participant DB as data/gre_mock.db

  Note over Src,DB: D1 — ETS Official Guide 3rd ed
  Src->>Ing: PDF chapters
  Ing->>Fmt: raw markdown
  Fmt->>Jud: structured question JSON
  Jud->>Vis: figure-bearing items
  Vis-->>Jud: figure OK / retire
  Jud->>DB: upsert (source=ets_og_3rd)

  Note over Src,DB: D2 — ETS Big Book (retired paper exams)
  Src->>Ing: scan PDFs
  Ing->>Fmt: OCR text + reformat
  Fmt->>Jud: structured JSON
  Jud->>DB: upsert (source=ets_bigbook)

  Note over Src,DB: D4 — AGIEval LSAT + Hendrycks MATH
  Src->>Fmt: open-license CSV
  Fmt->>Jud: GRE-style reformat
  Jud->>DB: upsert (source=agieval_lsat / hendrycks_math)

  Note over Src,DB: D5 — NYC Regents scraper
  Src->>Ing: HTML pages
  Ing->>Fmt: reformat
  Fmt->>DB: upsert (source=regents)

  Note over Src,DB: D6 — Quant gen v2 (LLM + solver + dual judge)
  Fmt->>Slv: candidate stem + answer key
  Slv-->>Fmt: verified key
  Fmt->>Jud: multi-judge vote<br/>(Opus + Sonnet + Gemini)
  Jud->>DB: upsert (source=ai_synthetic)

  Note over Src,DB: D7 — RC passage gen from public-domain prose
  Src->>Fmt: Project Gutenberg snippets
  Fmt->>Jud: 3-stage (draft → critique → revise)
  Jud->>DB: upsert (source=ai_generated)
```

---

## Features

| Area | What it does | Source |
|---|---|---|
| **Full mock tests** | Post-Sep-2023 format: AWA + V1·12 + V2·15 + Q1·12 + Q2·15, 1h58m total, section-adaptive between V1→V2 and Q1→Q2 | [`models/exam_session.py`](models/exam_session.py) |
| **All 11 question subtypes** | TC (1/2/3-blank), SE, RC single/multi/select-in-passage, QC, MCQ single/multi, Numeric Entry, Data Interpretation | [`services/scoring.py`](services/scoring.py) |
| **Smart drill picker** | 60% never-seen + 30% wrong-before + 10% right-before, skipping items served in the last 14 days | [`services/question_bank.py`](services/question_bank.py) |
| **Per-subtopic mastery** | EWMA over recent attempts, weighted by difficulty, with forgetting-curve decay; mastered at ≥0.80 over 10 attempts | [`services/mastery.py`](services/mastery.py) |
| **Elo item rating** | Per-item Elo seeded from difficulty label, updated on every response; powers information-theoretic question selection | [`services/rating_service.py`](services/rating_service.py) |
| **Randomesque selection** | Top-5-by-info + shuffle-within-top-5, smooths item exposure over long study horizons | [`services/question_bank.py`](services/question_bank.py) |
| **FSRS item scheduler** | Wrong items can be scheduled for spaced review; reused from the vocab FSRS engine | [`services/srs.py`](services/srs.py) |
| **Cross-session dedup** | Questions served in the current mock are excluded from the next one via `ServedLog` (independent of Response commits) | [`services/question_bank.py`](services/question_bank.py) |
| **Cluster cooldown** | 7-day cooldown on RC passage + DI cluster stimulus IDs, so the same passage can't reappear within a week | [`services/question_bank.py`](services/question_bank.py) |
| **Score forecast** | Logistic Verbal/Quant scaled-score range + 10-session sparkline | [`services/score_forecast.py`](services/score_forecast.py) |
| **Diagnostic (30Q)** | Stratified intake produces per-topic accuracy, weakness ranking, predicted scaled-score band | [`services/diagnostic.py`](services/diagnostic.py) |
| **Error log as UX** | Wrong answers classified (careless / conceptual / timing / vocab-gap), filtered, and actionable (Schedule Redo · Ask Tutor) | [`screens/error_log_screen.py`](screens/error_log_screen.py) |
| **AWA scoring (LLM)** | ETS-rubric-aligned, 4 subscores (analysis / structure / support / conventions) with prompt-injection hardening | [`services/awa_scorer.py`](services/awa_scorer.py) |
| **AnswerChat (LLM)** | Per-question tutor, scope-locked, never overrides the answer key | [`services/mistake_coach.py`](services/mistake_coach.py) |
| **Mistake-pattern coach** | Every 50 lifetime mistakes, Opus analyzes the error log → 3-bullet diagnosis + targeted drill | [`services/mistake_coach.py`](services/mistake_coach.py) |
| **Study plan generator** | Personalized week-by-week plan from diagnostic + mastery + bank availability | [`services/study_plan.py`](services/study_plan.py) |
| **Vocab (9,647 words)** | FSRS flashcards with definition, examples, synonyms/antonyms, root analysis, mnemonic | [`services/srs.py`](services/srs.py) |
| **Contextual vocab** | Generated 120-word passages + inference question per target word | [`services/vocab_context_gen.py`](services/vocab_context_gen.py) |
| **Streak + onboarding** | Daily streak with freeze-day forgiveness; 3-step first-launch wizard | [`services/streak.py`](services/streak.py) |
| **Crash recovery** | Every answer `fsync`'d to a journal; killed-mid-test state recoverable on next launch | [`models/exam_session.py`](models/exam_session.py) |
| **DI charts** | Real matplotlib-rendered charts (pie, bar, line, scatter, table), base64-embedded — no `file://` exposure | [`widgets/math_view.py`](widgets/math_view.py) |

---

## Tech stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.9.6 | Wheel availability for wxPython on macOS; 3.13 is blocked by a numpy/clang mismatch |
| GUI | wxPython 4.2.4 | Native widgets on macOS / Linux / Windows; `html2.WebView` for KaTeX-rendered stems |
| ORM | Peewee | ~22 tables, migrations runnable at launch, lighter than SQLAlchemy for an embedded DB |
| Database | SQLite | Zero-config, single-file, crash-safe with WAL, shippable as a seed blob |
| Math rendering | KaTeX | Bundled under `resources/katex/`; sanitized via `bleach` + strict CSP |
| Charts | matplotlib (dark theme) | Rendered once, embedded as base64 PNG into stimulus HTML |
| LLM layer | OpenRouter | One API surface for Opus / Sonnet / Gemini; easy to swap judges |
| Testing | pytest | 533 tests across 40+ modules; `tmp_db` fixture swaps to a throwaway DB |

---

## Dataset growth scripts

Each of these builds items tagged with a unique `source=` and writes `provenance_json` for audit:

| Script | What it does |
|---|---|
| [`scripts/extract_ets_og.py`](scripts/extract_ets_og.py) | ETS Official Guide 3rd ed. → marker-pdf → LLM reformat → upsert |
| [`scripts/extract_ets_bigbook.py`](scripts/extract_ets_bigbook.py) | ETS Big Book retired-paper exams → OCR → reformat → subtype-filter |
| [`scripts/extract_ets_bigbook_stub.py`](scripts/extract_ets_bigbook_stub.py) | Stub harness for iterating on the Big Book pipeline without the full PDF |
| [`scripts/extract_agieval_math.py`](scripts/extract_agieval_math.py) | AGIEval LSAT-LR/RC + Hendrycks MATH → GRE-style reformat |
| [`scripts/extract_regents.py`](scripts/extract_regents.py) | NYC Regents math exams → quant-reformat with solver verification |
| [`scripts/generate_quant_items.py`](scripts/generate_quant_items.py) | Quant gen v2 — LLM + sympy solver verification + dual-judge vote |
| [`scripts/generate_rc_passages.py`](scripts/generate_rc_passages.py) | RC passage gen from public-domain prose (3-stage draft→critique→revise) |
| [`scripts/recalibrate_irt.py`](scripts/recalibrate_irt.py) | Offline 2PL IRT recalibration with `girth`, priors from Elo rating |
| [`scripts/audit_data_corruption.py`](scripts/audit_data_corruption.py) | Heuristic scan for wrong-explanation / bad-figure rows |

---

## Development

```bash
# Activate the env
source venv/bin/activate

# Run the app
python app.py

# Run the test suite (533 tests, < 60s on an M-series Mac)
venv/bin/python -m pytest tests/ -q

# Run only the repetition-floor benchmark (20-mock simulation)
venv/bin/python -m pytest tests/benchmarks/test_repetition_floor.py -v

# Recalibrate IRT offline (optional, requires response data)
venv/bin/python scripts/recalibrate_irt.py
```

### Adding a feature

1. **Write the test first** under `tests/test_<feature>.py` using the `temp_db` fixture (see `tests/conftest.py`).
2. **Put deterministic logic in `services/`** — never inside a screen. Screens are presentation only.
3. **LLM calls go through `services/llm_service.py`** (never call `httpx` directly). The service wraps callbacks in `wx.CallAfter` and handles timeouts.
4. **Schema changes require a migration**. Add the file under `models/migrations.py`, give it a numeric prefix + date, and make it idempotent (every migration runs at every launch).
5. **UI tokens come from `widgets/theme.py` + `widgets/ui_scale.py`** — no hardcoded `wx.Colour` or font sizes in `screens/`.

---

## Bug reports

Every question screen has a flag icon that opens a pre-filled GitHub issue with qid + source + full JSON + a PNG of the main window. See [`docs/reporting.md`](docs/reporting.md) for the full flow, the screenshot-capture fallback ladder, and what the developer sees on the other end.

---

## Data Interpretation charts

DI questions render real visualisations (not inline text):

- **Rendered charts** (pie, bar, grouped-bar, line, stacked-bar, scatter) ship as base64-encoded PNGs inlined into the stimulus HTML. The `html2.WebView` renders them without `file://` access, eliminating path-traversal surface.
- **Tables** render as styled HTML; markdown pipe-tables in source content are converted to HTML at render time by `widgets/math_view._markdown_tables_to_html`.
- **LaTeX inline math in options** is normalised to readable Unicode (options render as `wx.StaticText`, no KaTeX WebView needed).

A figure audit over the quant corpus is documented in [`docs/figure_audit_2026_05_11.md`](docs/figure_audit_2026_05_11.md) — 1 mismatch out of 36 image-bearing items, already retired.

---

## Roadmap

The full 90-day plan lives in [`docs/implementation_plan_2026_05_12.md`](docs/implementation_plan_2026_05_12.md):

| Phase | Window | Deliverables |
|---|---|---|
| Phase 0 | Week 0 | Repetition-floor benchmark (P0.1) — done |
| Phase 1 | Week 1 | R1–R5 quick wins: DI gate, figure floor, served-log, consecutive-mock exclude, widening fallback — done |
| Phase 2 | Weeks 2–3 | Error-log-as-UX, FSRS items, Elo, randomesque, timing analytics (E1–E5) — done |
| Phase 3 | Weeks 4–8 | Section-level CAT wire-up, contextual vocab, calibrated AWA, forgetting curve (S1–S4) — done |
| Phase 4 | Weeks 9–12 | Offline IRT recalibration, score-forecast calibration, cluster cooldown, dataset Tier 1 (D1–D7) — done |

Diagnosis of the original "repetitiveness" complaint that drove the plan: [`research/gre-repetitiveness-roadmap/report.md`](research/gre-repetitiveness-roadmap/report.md).

---

## Configuration

### Runtime LLM (OpenRouter)

Configure via the in-app **Settings** dialog (atomically saved to `data/llm_config.json`, `chmod 0o600`) or environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Your OpenRouter key |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | API base URL |
| `LLM_MODEL` | `anthropic/claude-opus-4` | Model id |
| `LLM_MAX_TOKENS` | `4096` | Response cap |

### LLM call hardening

- **Timeout**: connect 10 s, read 180 s, write 10 s, pool 10 s. Long generations (study plans can take 90 s) succeed; stuck connections surface as a friendly error rather than a hung UI.
- **Threading**: `call_async` / `chat_async` wrap callbacks in `wx.CallAfter` so GUI updates happen on the main thread.
- **Prompt-injection hardening**: AnswerChat and explanation prompts wrap user-untrusted blocks (`<stimulus>`, `<prompt>`, `<options>`, `<student_answer>`, `<explanation>`) and the system prompt explicitly warns the model not to follow embedded instructions.
- **WebView sanitization**: `widgets/html_sanitizer.py` runs every LLM-generated stimulus/prompt/explanation through `bleach` before `wx.html2.WebView.SetPage`. The page also has a strict CSP (`default-src 'self' data:`, `connect-src 'none'`).

---

## Troubleshooting

**wxPython on Linux** → `sudo apt install libgtk-3-dev libwebkit2gtk-4.0-dev` before `pip install wxPython`.

**numpy / wxPython "metadata-generation-failed" on macOS Python 3.13** → pin to Python 3.12 (`brew install python@3.12 && PYTHON=python3.12 ./setup.sh`).

**Empty dashboard / "no questions"** → the seed DB didn't land during clone. `git checkout HEAD -- data/gre_mock.db`.

**AWA score shows N/A / AI tutor doesn't open** → configure your OpenRouter key via Settings. The Insights tab disables the "Run coach now" button when no key is configured.

**Database reset** → `git checkout HEAD -- data/gre_mock.db` to restore the shipped seed, or `rm data/gre_user.db` to wipe your personal state. The app re-bootstraps `gre_user.db` from the seed on first launch.

**Recover from a force-quit mid-test** → a timestamped `data/autosave_journal.YYYYMMDD_HHMMSS.jsonl.bak` is archived on the next launch.

---

## License

MIT
