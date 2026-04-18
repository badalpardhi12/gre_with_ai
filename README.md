# GRE Mock Test Platform with AI Tutoring

A best-in-class desktop GRE preparation platform: full-length section-adaptive
mock tests, a curated vocabulary curriculum with spaced repetition, AI-generated
lessons and study plans, and per-question tutoring backed by Claude Opus 4.7.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![wxPython](https://img.shields.io/badge/GUI-wxPython%204.2-orange)
![SQLite](https://img.shields.io/badge/database-SQLite-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## Features

### Test taking
- **Full-length mock tests** in the post-September-2023 GRE format
  (1 h 58 min, 5 sections, AWA + V1/V2 + Q1/Q2)
- **Section-level adaptive routing** — Section 2 difficulty is chosen from
  Section 1 performance, mirroring the real ETS engine
- **All 11 question subtypes** — TC (1/2/3 blank), SE, RC single/multi/select,
  QC, MCQ single/multi, Numeric Entry, Data Interpretation
- **Real Data Interpretation charts** — pie/bar/grouped-bar/line/scatter/table,
  rendered with matplotlib (dark theme), embedded as inline base64
- **On-screen calculator** for quant sections, KaTeX math rendering throughout
- **Topic drills** — 10/25/50-question drills per subtopic with smart selection
  (60% never-seen + 30% wrong-before + 10% right-before, skipping last 14 days)

### Adaptive learning
- **Diagnostic test** — 30-question stratified intake produces per-topic
  accuracy, weakness ranking, predicted scaled-score band
- **Per-subtopic mastery tracking** — EWMA over recent attempts at the user's
  ability band; "mastered" at ≥0.80 over 10 attempts
- **AI study plan generator** — Opus 4.7 builds a personalised week-by-week
  plan from diagnostic + live mastery + bank availability + vocab progress;
  daily task list shown on the dashboard
- **Score forecast** — predicted Verbal/Quant scaled-score range, updated
  after every session

### AI tutoring
- **"Why is C wrong?" chat** — opens after a missed question, scope-locked to
  that question; never overrides the deterministic correct answer
- **Mistake-pattern coach** — every 50 questions, Opus 4.7 analyses your error
  log and outputs a 3-bullet diagnosis plus a targeted 10-question drill
- **AWA scoring** — ETS-rubric-aligned essay evaluation with inline feedback

### Content
- **~2,000 live questions** across 48 subtopics (target: 4,000), tagged by
  topic + subtopic + question_type, validated for difficulty + quality
- **3,007 curated vocabulary words** in 3 frequency tiers, with definitions,
  example sentences, synonyms, antonyms, root analysis, mnemonics, theme tags
- **308 Latin/Greek roots** linked to vocabulary words
- **49 subtopic lessons + 8 strategy guides** auto-generated from Kaplan +
  Princeton ebook content
- **136 AWA issue prompts**

### Workflow
- **Dashboard** — score forecast, today's plan, mastery heatmap, recent
  activity, quick-start buttons
- **Topic browser** — taxonomy tree with mastery % and one-click drills
- **Vocabulary flashcards** — FSRS-inspired spaced repetition, 4-button rating
- **DPI-aware UI scaling** — auto 1.0×/1.25×/1.5× based on display
- **Native macOS menu bar** with standard IDs (⌘, ⌘W ESC)

---

## GRE Exam Structure (post-September 2023)

| Section | Time | Questions | Score |
|---------|------|-----------|-------|
| AWA | 30 min | 1 issue essay | 0–6 |
| Verbal 1 | 18 min | 12 | 130–170 combined |
| Verbal 2 | 23 min | 15 | (section-adaptive) |
| Quant 1 | 21 min | 12 | 130–170 combined |
| Quant 2 | 26 min | 15 | (section-adaptive) |

**Total: 5 sections, 55 questions + 1 essay, 118 minutes.**

Section 1 performance steers Section 2 difficulty:
- < 40% correct → Easy S2 (ceiling ~155)
- 40–70% → Medium S2 (ceiling ~165)
- > 70% → Hard S2 (ceiling 170)

---

## Prerequisites

- **Python 3.9+** (uses `Optional[X]` rather than `X | Y` to stay 3.9-compatible)
- **macOS** primary; Linux/Windows supported (wxPython 4.2.4)
- **LLM access** — defaults to LLM gateway (Opus 4.7); can be
  swapped for any OpenAI-compatible API (OpenRouter, direct Anthropic, etc.)

---

## Quick Start

```bash
git clone https://github.com/badalpardhi12/gre_with_ai.git
cd gre_with_ai
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

The database (`data/gre_mock.db`) is auto-created and seeded on first run.

For LLM-backed features (AWA scoring, study plans, AI tutor), configure access
via Settings inside the app, or set environment variables (see
[Configuration](#configuration)).

---

## Project Structure

```
gre_with_ai/
├── app.py                              # Application entry point
├── main_frame.py                       # Window + screen orchestration + menus
├── config.py                           # Exam constants, paths
│
├── models/
│   ├── database.py                     # Peewee ORM (20+ tables)
│   ├── taxonomy.py                     # 48-subtopic taxonomy (single source of truth)
│   └── exam_session.py                 # Section + adaptive state
│
├── services/
│   ├── llm_client.py             # the LLM gateway (Anthropic + Gemini) client
│   ├── llm_service.py                  # Generic LLM router
│   ├── question_bank.py                # Composition-aware selection + smart drill picker
│   ├── scoring.py                      # 11 subtype answer-checkers + scaled scoring
│   ├── awa_scorer.py                   # ETS-rubric AWA scoring
│   ├── srs.py                          # FSRS-inspired vocab spaced repetition
│   ├── diagnostic.py                   # 30Q stratified diagnostic
│   ├── mastery.py                      # EWMA per-subtopic mastery
│   ├── study_plan.py                   # Personalised plan via Opus 4.7
│   ├── mistake_coach.py                # AnswerChat + analyze_mistakes
│   ├── practice_test_assembler.py      # 6 unique full-length tests
│   └── score_forecast.py               # Predicted scaled-score range
│
├── screens/
│   ├── dashboard_screen.py             # Main hub (forecast/plan/mastery)
│   ├── welcome_screen.py
│   ├── instructions_screen.py
│   ├── awa_screen.py                   # Essay editor + rubric scoring
│   ├── question_screen.py              # All 11 subtypes; AI-tutor button
│   ├── review_screen.py
│   ├── results_screen.py
│   ├── progress_screen.py
│   ├── vocab_screen.py                 # Rich-back-card flashcards
│   ├── lesson_screen.py                # Single lesson reader
│   ├── topic_browser_screen.py         # Taxonomy tree + per-subtopic drills
│   ├── answer_chat_screen.py           # Per-question AI tutor dialog
│   └── llm_settings.py
│
├── widgets/
│   ├── math_view.py                    # KaTeX HTML renderer (WebView)
│   ├── calculator.py
│   ├── numeric_entry.py
│   ├── question_nav.py
│   ├── timer.py
│   └── ui_scale.py                     # DPI-aware font scaling
│
├── scripts/
│   ├── seed_data.py
│   ├── extract_kaplan.py               # EPUB → questions (Kaplan 2024)
│   ├── extract_princeton.py            # EPUB → questions (Princeton 1,014)
│   ├── extract_princeton_vision.py     # OCR for image-based math questions
│   ├── retag_questions.py              # LLM re-tagging to subtopic taxonomy
│   ├── generate_questions.py           # AI gen → fill subtopic gaps
│   ├── generate_lessons.py             # AI lesson + strategy generation
│   ├── generate_explanations.py
│   ├── curate_vocab.py                 # Tier 1/2/3 + retire low-value words
│   ├── enrich_vocab.py                 # Examples, synonyms, mnemonics
│   ├── fix_di_charts.py                # Inline-text DI → matplotlib chart
│   ├── reconstruct_di_charts.py        # Back-construct charts for orphan DI
│   ├── embed_chart_images.py           # file:// → base64 data URI
│   ├── generate_di_charts.py
│   ├── rate_difficulty.py              # LLM-rated difficulty 1-5
│   └── cleanup_broken_questions.py
│
├── data/
│   ├── gre_mock.db                     # SQLite (auto-created)
│   ├── images/                         # Rendered DI chart PNGs
│   ├── ebooks/                         # Source Kaplan + Princeton EPUBs
│   ├── extracted/                      # Intermediate extraction JSON
│   └── external/                       # Source CSVs for vocab imports
│
└── resources/
    └── katex/                          # KaTeX library
```

---

## Question Bank

Live: ~2,000 questions across 48 subtopics. Distribution targets defined in
`models/taxonomy.py`; `scripts/generate_questions.py --fill-gaps` brings any
under-represented subtopic up to its target count via Opus 4.7 generation +
second-pass validation.

| Category | Live | Target |
|----------|-----:|-------:|
| Quant total | ~1,250 | ~1,400 |
| Verbal total | ~750 | ~990 |
| AWA prompts | 136 | 100 |
| Vocab words (active) | 3,007 | — |
| Vocab roots | 308 | — |
| Lessons | 49 | 48 |

Each question stores: `topic`, `subtopic`, `question_type`, `difficulty_target`
(1–5), `quality_score` (0–1), `provenance`, `source`. Smart drill selection
uses `Response` history to skip last-14-days repeats and prefer never-seen +
previously-wrong questions.

---

## Configuration

### LLM backend

The app defaults to LLM gateway gateway for Opus 4.7. To use a
different backend, set environment variables before launch:

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_BASE_URL` | Anthropic-compatible base URL |
| `ANTHROPIC_AUTH_TOKEN` | Bearer token (LLM gateway id-token, or Anthropic API key) |
| `OPENROUTER_API_KEY` | Falls back to OpenRouter if LLM gateway is unavailable |

For the LLM gateway, refresh the token before each long-running script:

```bash
ID_TOKEN=$(/usr/local/bin/auth-helper getToken \
  -C hvys3fcwcteqrvw3qzkvtk86viuoqv \
  --token-type=oauth --interactivity-type=none -E prod -G pkce \
  -o openid,dsid,accountname,profile,groups 2>/dev/null \
  | tr -s ' \n' '\n' | tail -1)
ANTHROPIC_BASE_URL="https://llm.gateway.example/api/anthropic" \
ANTHROPIC_AUTH_TOKEN="$ID_TOKEN" \
venv/bin/python <SCRIPT>
```

In-app **Settings** persist runtime LLM config to `data/llm_config.json`.

---

## Architecture

### Two-layer separation
1. **Deterministic core** — section engine, timer, scoring, adaptive routing.
   Never depends on the LLM. The user's score is always computed from the
   answer key, never from a model output.
2. **LLM layer** — AWA scoring, explanations, study-plan generation, the
   tutoring chat, mistake-pattern coach. May be offline; the rest of the app
   degrades gracefully.

### Data flow

```
WelcomeScreen / Dashboard
    │
    ├─► Diagnostic ─────► DiagnosticResult ─┐
    │                                       │
    ├─► Topic drill (smart-pick)            ├─► StudyPlan (Opus 4.7)
    │       │                               │
    │       ▼                               ▼
    │   QuestionScreen ◄── adaptive next ─ Today's tasks (dashboard)
    │       │
    │       ├─► AnswerChat (per-Q tutor)
    │       ├─► MasteryRecord update
    │       └─► Response logged
    │
    ├─► Full mock test
    │       AWA → V1 → V2(adaptive) → Q1 → Q2(adaptive) → Results
    │
    ├─► Vocabulary session (FSRS)
    │       Due cards + N new → 4-button rating → reschedule
    │
    └─► MistakeCoach (every 50 Qs) → diagnosis + targeted drill
```

### Database

SQLite + Peewee. Schema migrations applied via `SqliteMigrator`. Key tables:
`Question`, `QuestionOption`, `NumericAnswer`, `Stimulus`, `Response`,
`ItemStats`, `ExamSession`, `SectionRecord`, `AWAPrompt`, `AWAEssay`,
`VocabWord`, `VocabRoot`, `FlashcardReview`, `MasteryRecord`, `Lesson`,
`StudyPlan`, `DiagnosticResult`.

---

## Data Interpretation Charts

DI questions render real visualisations rather than inline text. Pipeline:

1. **AI generation** (`scripts/generate_questions.py`) — Opus 4.7 emits a
   `chart_spec` JSON object alongside the question
2. **Imported orphans** (`scripts/reconstruct_di_charts.py`) — for questions
   whose original ebook charts were lost during extraction, Opus
   back-constructs a chart spec consistent with the marked-correct answer;
   shared-data groups (e.g. multiple questions referencing the same Springfield
   1992-1998 income chart) deduplicate via a `data_context` key
3. **Rendering** — matplotlib (dark theme) outputs PNG; HTML wraps as inline
   `<img src="data:image/png;base64,...">` so wxPython WebView renders without
   `file://` security issues
4. **Tables** — rendered as styled HTML, not images

Supported chart types: pie, bar, grouped-bar (multi-series), line, stacked-bar,
scatter, table.

---

## Troubleshooting

**wxPython on Linux**: needs GTK + WebKit dev headers
(`sudo apt install libgtk-3-dev libwebkit2gtk-4.0-dev`).

**"No questions available"**: run `venv/bin/python scripts/seed_data.py` then
the import scripts in `scripts/`.

**AWA scoring shows N/A**: configure LLM settings in the app or set the
environment variables above.

**Database reset**: `rm data/gre_mock.db*` then re-launch — the app will
recreate and reseed.

---

## License

MIT
