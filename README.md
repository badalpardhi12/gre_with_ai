# GRE prep with AI

Best-in-class desktop GRE preparation: full-length section-adaptive mock
tests, a curated vocabulary curriculum with spaced repetition, AI-generated
lessons and study plans, and per-question tutoring backed by Claude Opus 4.7.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![wxPython](https://img.shields.io/badge/GUI-wxPython%204.2-orange)
![SQLite](https://img.shields.io/badge/database-SQLite%20+%20LFS-green)
![Tests](https://img.shields.io/badge/tests-45%20passing-brightgreen)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## Quick start

```bash
git clone https://github.com/badalpardhi12/gre_with_ai.git
cd gre_with_ai
chmod +x setup.sh && ./setup.sh
source venv/bin/activate
python app.py
```

`setup.sh` is idempotent: it installs Python (via Homebrew on macOS or apt on
Debian/Ubuntu) and Git LFS if missing, runs `git lfs pull` to fetch the
shipped 8.7 MB question/vocab/lessons database, builds the venv, installs
every dep including `bleach` and `pytest`, applies the on-launch schema
migrations, and runs the test suite as a smoke check. Re-run it after every
`git pull` to refresh deps and DB.

---

## What's inside

### Test taking
- **Full-length mock tests** in the post-September-2023 GRE format
  (1 h 58 min: AWA + V1 + V2 + Q1 + Q2)
- **Section-level adaptive routing** — Section 2 difficulty is chosen from
  Section 1 performance, mirroring the real ETS engine
- **All 11 question subtypes** — TC (1/2/3 blank), SE, RC single/multi/
  select-in-passage, QC, MCQ single/multi, Numeric Entry, Data Interpretation
- **Real Data Interpretation charts** — pie/bar/grouped-bar/line/scatter/
  table, rendered with matplotlib (dark theme), embedded as inline base64
  data URIs (no `file://` exposure to the WebView)
- **On-screen calculator** for quant sections, KaTeX math rendering throughout
- **Crash recovery** — every answer is fsync'd to a journal; killed-mid-test
  state is recoverable on next launch

### Adaptive learning
- **Diagnostic test** — 30-question stratified intake produces per-topic
  accuracy, weakness ranking, and a predicted scaled-score band
- **Per-subtopic mastery tracking** — EWMA over recent attempts, weighted
  by question difficulty; mastered at ≥0.80 over 10 attempts
- **AI study-plan generator** — Claude Opus 4.7 builds a personalized
  week-by-week plan from your diagnostic + live mastery + bank availability
- **Score forecast** — predicted Verbal/Quant scaled-score range with a
  10-session sparkline trend
- **Smart drill picker** — 60% never-seen + 30% wrong-before + 10% right-
  before, skipping the last 14 days

### AI tutoring
- **AnswerChat** — opens after a missed question, scope-locked to that
  question; never overrides the deterministic correct answer
- **Mistake-pattern coach** — every 50 lifetime mistakes, Opus 4.7
  analyzes your error log and outputs a 3-bullet diagnosis with a
  targeted drill recommendation. Manual trigger from the Insights tab.
- **Per-question explanation generation** — falls back to LLM when no
  stored explanation exists, then caches the result
- **AWA scoring** — ETS-rubric-aligned essay evaluation with prompt-injection
  hardening (essay text wrapped in `<essay>` tags, system prompt declares
  user input untrusted)

### Habit & onboarding
- **3-step onboarding wizard** on first launch — Welcome → Goal & test
  date → Diagnostic offer; "Skip" exits at any step
- **Streak tracker** — flame icon + day count in the sidebar; one
  freeze-day forgiveness, +1 freeze every Sunday (cap 3)
- **Daily-goal completion bar** on the Today tab, sourced from real
  per-question time spent

### Content
- **~2,300 live questions** across 48 subtopics, tagged by topic +
  subtopic + question_type, validated for difficulty + quality
- **3,007 curated vocabulary words** in 3 frequency tiers, with
  definitions, example sentences, synonyms, antonyms, root analysis,
  mnemonics, theme tags
- **308 Latin/Greek roots** linked to vocabulary words
- **49 subtopic lessons + 8 strategy guides** auto-generated from
  Kaplan + Princeton ebook content
- **136 AWA issue prompts**

---

## UI architecture

A persistent left sidebar with five purpose-built tabs (each does one thing,
and exactly one thing):

| Tab | Job |
|---|---|
| **Today** (Cmd+1)    | One primary CTA chosen by the app + today's plan checklist + score-forecast range bar + recent activity |
| **Learn** (Cmd+2)    | 48-cell mastery heatmap with filter chips (All / Weak / Mastered / Not started) + integrated subtopic detail with lesson + practice CTA |
| **Practice** (Cmd+3) | Three distinct cards: Quick Drill (smart 10-Q), Section Test (timed verbal or quant), Full Mock Exam (AWA + 4 sections, ~2 h) |
| **Vocab** (Cmd+4)    | Daily SRS flashcards (FSRS-inspired) with rich back-of-card content |
| **Insights** (Cmd+5) | Score-forecast trend, per-measure mastery roll-up, study-plan summary, mistake-coach status, full test history |

Below the tabs sit the streak badge and a Settings cog.

### Visual design
- Single dark theme (no light variant) — every color, font size, and
  spacing token comes from `widgets/theme.py` and `widgets/ui_scale.py`.
  No `wx.Colour(...)` or hardcoded font sizes anywhere in `screens/`.
- Custom-painted reusable widgets: `Sidebar`, `Card`, `PrimaryButton`,
  `SecondaryButton`, `EmptyState`, `RangeBar`, `Sparkline`, `Heatmap`,
  `StreakBadge` — keeps appearance consistent across macOS / Linux /
  Windows where stock wx widgets render inconsistently.

---

## Project structure

```
gre_with_ai/
├── app.py                              # Application entry point
├── main_frame.py                       # Sidebar shell + screen orchestration + menus
├── config.py                           # Exam constants + atomic LLM-config save
│
├── models/
│   ├── database.py                     # Peewee ORM (~22 tables) + UserStats
│   ├── migrations.py                   # On-launch schema migrator (5 migrations)
│   ├── taxonomy.py                     # 48-subtopic taxonomy + display-name lookup
│   └── exam_session.py                 # Section + adaptive state + journal
│
├── services/
│   ├── llm_service.py                  # OpenRouter (httpx.Timeout + wx.CallAfter wrappers)
│   ├── question_bank.py                # Composition-aware selection + smart drill + subtopic_summary
│   ├── scoring.py                      # 11 subtype answer-checkers + scaled scoring (Fraction-space tolerance)
│   ├── awa_scorer.py                   # ETS-rubric AWA scoring (prompt-injection hardened)
│   ├── srs.py                          # FSRS-inspired vocab spaced repetition (NOT EXISTS subquery)
│   ├── diagnostic.py                   # 30Q stratified diagnostic + grade_diagnostic (atomic)
│   ├── mastery.py                      # EWMA per-subtopic mastery (symmetric scoring)
│   ├── study_plan.py                   # Personalized plan via Opus 4.7
│   ├── mistake_coach.py                # AnswerChat + analyze_mistakes (delimiter-wrapped prompts)
│   ├── score_forecast.py               # Predicted scaled-score range + 10-point history
│   ├── streak.py                       # Daily streak + freeze logic + onboarding state
│   └── log.py                          # Centralized rotating-file + stderr logger
│
├── screens/
│   ├── today_screen.py                 # Tab 1 — daily home
│   ├── learn_screen.py                 # Tab 2 — heatmap + subtopic detail
│   ├── practice_screen.py              # Tab 3 — three mode cards
│   ├── vocab_screen.py                 # Tab 4 — flashcards
│   ├── insights_screen.py              # Tab 5 — analytics
│   ├── onboarding/
│   │   └── wizard.py                   # 3-step first-launch flow
│   ├── instructions_screen.py          # Pre-section briefing
│   ├── awa_screen.py                   # Essay editor + 10s autosave to disk
│   ├── question_screen.py              # All 11 subtypes; AI-tutor button
│   ├── review_screen.py                # In-section review grid
│   ├── results_screen.py               # Post-test scores
│   ├── diagnostic_results_screen.py    # Diagnostic deep-dive + Build-Plan CTA
│   ├── answer_chat_screen.py           # Per-question AI tutor dialog
│   ├── llm_settings.py                 # Settings dialog (atomic save, 0o600)
│   └── study_plan_dialog.py            # Plan-creation form
│
├── widgets/
│   ├── theme.py                        # Color tokens + mastery_color()
│   ├── ui_scale.py                     # DPI-aware fonts + semantic tokens (text_xs..display, space)
│   ├── sidebar.py                      # 5-tab nav + streak badge + cog
│   ├── card.py                         # Tokenized titled surface
│   ├── primary_button.py               # Custom-painted accent CTA
│   ├── secondary_button.py             # Custom-painted outlined chip
│   ├── empty_state.py                  # Icon + headline + body + CTA
│   ├── range_bar.py                    # Score-forecast min-max bar
│   ├── sparkline.py                    # 10-point trend line
│   ├── heatmap.py                      # 48-cell mastery grid
│   ├── math_view.py                    # KaTeX HTML renderer (CSP, bleach sanitized)
│   ├── html_sanitizer.py               # bleach allow-list for WebView
│   ├── calculator.py                   # On-screen calc (forbids ** to prevent DoS)
│   ├── numeric_entry.py                # Decimal / fraction input (finite-float guard)
│   ├── question_nav.py                 # In-section question grid
│   └── timer.py                        # Wallclock-anchored countdown
│
├── tests/                              # 45 pytest cases
│   ├── conftest.py                     # tmp_db fixture
│   ├── test_scoring.py                 # 25 scoring engine + estimate cases
│   ├── test_exam_session.py            # 11 adaptive routing + section-state cases
│   └── test_streak.py                  # 9 streak / freeze / onboarding cases
│
├── scripts/                            # Build-time data tools (not invoked at runtime)
│   ├── seed_data.py                    # Initial DB seed
│   ├── extract_kaplan.py               # EPUB → questions
│   ├── extract_princeton.py            # EPUB → questions (1,014)
│   ├── retag_questions.py              # LLM re-tagging to subtopic taxonomy
│   ├── generate_questions.py           # AI gen → fill subtopic gaps (atomic)
│   ├── generate_lessons.py             # AI lesson + strategy generation
│   ├── generate_explanations.py
│   ├── curate_vocab.py                 # Tier 1/2/3 + retire low-value words
│   ├── enrich_vocab.py                 # Examples, synonyms, mnemonics
│   ├── fix_di_charts.py                # Inline-text DI → matplotlib chart
│   ├── reconstruct_di_charts.py        # Back-construct charts for orphan DI
│   ├── embed_chart_images.py           # file:// → base64 data URI
│   ├── rate_difficulty.py              # LLM-rated difficulty 1-5
│   └── cleanup_broken_questions.py
│
├── data/
│   ├── gre_mock.db                     # SQLite, ~8.7 MB, ships via Git LFS
│   ├── images/                         # Rendered DI chart PNGs
│   ├── ebooks/                         # Source Kaplan + Princeton EPUBs (gitignored)
│   ├── extracted/                      # Intermediate extraction JSON (gitignored)
│   ├── external/                       # Source CSVs for vocab imports
│   └── llm_config.json                 # User's API key (0o600, gitignored)
│
└── resources/
    └── katex/                          # KaTeX library (math rendering)
```

---

## Architecture

### Three-layer separation
1. **Deterministic core** — section engine, timer, scoring, adaptive
   routing. Never depends on the LLM. The user's score is always computed
   from the answer key, never from a model output. Lives in
   `services/scoring.py`, `models/exam_session.py`, `widgets/timer.py`.
2. **Runtime LLM layer (OpenRouter)** — AWA scoring, explanations,
   study-plan generation, AI tutor chat, mistake-pattern coach. Calls go
   through `services/llm_service.py` (httpx timeout + wx.CallAfter
   marshalling). May be offline; the rest of the app degrades gracefully
   (drills, mock tests, vocab review all work without an API key).
3. **Build-time data generation** — scripts in `scripts/` were used once
   to build the database that ships in this repo via Git LFS. End users
   never invoke them.

### Data flow

```
First launch
    │
    └─► Onboarding wizard (Welcome → Goal → Diagnostic) ──► Today tab
                                                                │
                                            ┌───────────────────┘
                                            ▼
                                  ┌─────────────────────────┐
                                  │   Today tab (home)      │
                                  │   one primary CTA       │
                                  └────────────┬────────────┘
        ┌──────────────────┬─────────────────┬────────────┬──────────────┐
        ▼                  ▼                 ▼            ▼              ▼
    Learn tab         Practice tab        Vocab tab    Insights      AnswerChat
    Heatmap +         Quick Drill /       FSRS         Forecast,     (per-Q tutor)
    lesson            Section Test /      flashcards   plan,
                      Full Mock                        coach,
                                                       history
        │                  │                 │
        └──────────────────┴───►  QuestionScreen
                                   │
                                   ├─► ScoringEngine.check_answer
                                   ├─► update_mastery (per subtopic)
                                   ├─► record_activity (streak)
                                   ├─► log_event (autosave journal)
                                   └─► Response row persisted
                                        │
                                        └─► every 50 wrong: MistakeCoach
```

### Database

SQLite + Peewee with Git-LFS-shipped content. Schema migrations are applied
on every launch via `models/migrations.py`:

| # | Migration | What it does |
|---|---|---|
| 001 | `numeric_answer_mode` | Add `mode` column; backfill from `numerator IS NOT NULL` |
| 002 | `numeric_answer_default_tolerance` | Bump existing decimal answers from 0 → 0.001 |
| 003 | `flashcard_review_indexes` | Index `next_review_at` + composite `(user_id, next_review_at)` |
| 004 | `user_stats` | Seed singleton `UserStats` row for `user_id="local"` |
| 005 | `onboarding_inferred_complete` | Auto-onboard users with prior `Response` rows |

Key tables: `Question`, `QuestionOption`, `NumericAnswer`, `Stimulus`,
`Response`, `ItemStats`, `Session`, `SectionResult`, `AWAPrompt`,
`AWASubmission`, `AWAResult`, `VocabWord`, `VocabRoot`, `FlashcardReview`,
`MasteryRecord`, `Lesson`, `StudyPlan`, `DiagnosticResult`,
`UserStats`, `SchemaMigration`.

---

## Configuration

### Runtime LLM (OpenRouter)

All in-app AI features go through OpenRouter via `services/llm_service.py`:

| Feature | Where |
|---------|-------|
| AWA essay scoring | `services/awa_scorer.py` |
| Per-question AI tutor (AnswerChat) | `services/mistake_coach.py` |
| Mistake-pattern coach | `services/mistake_coach.py` |
| AI study plan generator | `services/study_plan.py` |
| Question explanations on demand | `services/explanation.py` |

Configure via the in-app **Settings** dialog (saved atomically to
`data/llm_config.json` with `chmod 0o600`) or environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENROUTER_API_KEY` | — | Your OpenRouter key |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | API base URL |
| `LLM_MODEL` | `anthropic/claude-opus-4` | Model id |
| `LLM_MAX_TOKENS` | `4096` | Response cap |

### LLM call hardening
- **Timeout**: connect 10 s, read 180 s, write 10 s, pool 10 s. Long
  generations (study plans can take 90 s) succeed; stuck connections
  surface as a friendly error rather than a hung UI.
- **Threading**: `call_async` and `chat_async` wrap callbacks in
  `wx.CallAfter` so callers can update the GUI directly without their
  own marshalling.
- **Prompt-injection delimiters**: AnswerChat and explanation prompts
  wrap user-untrusted blocks (`<stimulus>`, `<prompt>`, `<options>`,
  `<student_answer>`, `<explanation>`) and the system prompt explicitly
  warns the model not to follow embedded instructions.
- **WebView sanitization**: `widgets/html_sanitizer.py` runs every
  LLM-generated stimulus/prompt/explanation through `bleach` before
  it reaches `wx.html2.WebView.SetPage`. The page also has a strict CSP
  (`default-src 'self' data:`, `connect-src 'none'`).

---

## Testing

```bash
source venv/bin/activate
pytest tests/ -v
```

45 tests cover:
- Every subtype's `ScoringEngine.check_answer` (TC empty-correct,
  numeric crash on bad data, tolerance None, fraction equivalence,
  SE all-or-nothing, etc.)
- `estimate_scaled_score` defensive type-coercion
- Section-adaptive routing at exactly 40% / 70% boundaries
- Section-state navigation, mark, response, tick
- Streak gap / freeze / longest-streak / Sunday top-up logic
- Onboarding state machine

`tests/conftest.py` provides a `temp_db` fixture that swaps `config.DB_PATH`
to a `tmp_path` and re-imports `models.*` + `services.*` so tests never
touch the user's real DB.

---

## Data Interpretation Charts

DI questions render real visualisations rather than inline text. Pipeline:

1. **AI generation** (`scripts/generate_questions.py`) — Opus 4.7 emits a
   `chart_spec` JSON object alongside the question
2. **Imported orphans** (`scripts/reconstruct_di_charts.py`) — for
   questions whose original ebook charts were lost during extraction,
   Opus back-constructs a chart spec consistent with the marked-correct
   answer; shared-data groups deduplicate via a `data_context` key
3. **Rendering** — matplotlib (dark theme) outputs PNG; HTML wraps as
   inline `<img src="data:image/png;base64,...">` so the wxPython WebView
   renders without `file://` security exposure
4. **Tables** — rendered as styled HTML, not images

Supported chart types: pie, bar, grouped-bar, line, stacked-bar, scatter,
table.

---

## Troubleshooting

**wxPython on Linux**
> `pip install wxPython` may need GTK + WebKit dev headers:
> `sudo apt install libgtk-3-dev libwebkit2gtk-4.0-dev`

**Empty dashboard / "no questions"**
> The database ships via Git LFS. Re-run `./setup.sh` (or
> `git lfs install && git lfs pull`) to fetch `data/gre_mock.db`.

**AWA score shows N/A / AI tutor doesn't open**
> Configure your OpenRouter key via the Settings dialog (or
> `OPENROUTER_API_KEY` env var). The Insights tab disables the
> "Run coach now" button when no key is configured.

**Database reset**
> `rm data/gre_mock.db*` then `git lfs pull` to restore the shipped DB,
> or relaunch to start with an empty DB.

**Wizard re-appears every launch**
> If you skipped onboarding but want it gone permanently:
> ```python
> from services.streak import mark_onboarding_complete
> mark_onboarding_complete()
> ```

**Recover from a force-quit mid-test**
> A timestamped `data/autosave_journal.YYYYMMDD_HHMMSS.jsonl.bak` is
> archived on the next launch. Open it to inspect any answers you'd
> committed before the crash.

---

## License

MIT
