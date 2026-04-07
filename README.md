# GRE Mock Test Platform with LLM Supervision

A desktop application for taking realistic GRE practice tests with AI-powered essay scoring. Built with Python, wxPython, and OpenRouter LLM integration.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![wxPython](https://img.shields.io/badge/GUI-wxPython%204.2-orange)
![SQLite](https://img.shields.io/badge/database-SQLite-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## Features

- **Full-length mock tests** following the post-September 2023 GRE format (1 h 58 min, 5 sections)
- **Section-level adaptive difficulty** — second section adjusts based on first-section performance
- **All GRE question types** — Text Completion, Sentence Equivalence, Reading Comprehension, Quantitative Comparison, Multiple Choice, Numeric Entry, Data Interpretation
- **Analytical Writing Assessment (AWA)** with real-time word count and LLM-based scoring
- **On-screen calculator** for quantitative sections
- **KaTeX math rendering** for formulas and equations
- **Question bank** with 600+ questions and 9,600+ vocabulary words
- **Progress dashboard** — track scores, accuracy by topic, and session history
- **Flexible test modes** — full mock, section-only (verbal/quant), learning mode with explanations
- **LLM-generated explanations** for missed questions
- **Configurable LLM backend** — any OpenRouter-supported model (Claude, GPT-4o, Gemini, etc.)

---

## GRE Concepts & Exam Structure

### What is the GRE?

The GRE (Graduate Record Examinations) General Test is a standardised test accepted by thousands of graduate and business schools worldwide. The current format (post-September 2023) is shorter than the legacy version at **1 hour 58 minutes**.

### Exam Sections

| Section | Time | Questions | Score Range |
|---------|------|-----------|-------------|
| **Analytical Writing (AWA)** | 30 min | 1 essay (Issue task) | 0–6 (half-point increments) |
| **Verbal Reasoning — Section 1** | 18 min | 12 questions | 130–170 (combined) |
| **Verbal Reasoning — Section 2** | 23 min | 15 questions | |
| **Quantitative Reasoning — Section 1** | 21 min | 12 questions | 130–170 (combined) |
| **Quantitative Reasoning — Section 2** | 26 min | 15 questions | |

**Total: 5 sections, 55 questions + 1 essay, 118 minutes**

### Section-Level Adaptive Testing

The GRE uses a **section-adaptive** design:

1. Everyone gets the same difficulty level for Section 1 (medium)
2. Your performance on Section 1 determines the difficulty of Section 2:
   - **< 40% correct** → Easy Section 2 (score ceiling ~155)
   - **40–70% correct** → Medium Section 2 (score ceiling ~165)
   - **> 70% correct** → Hard Section 2 (score ceiling 170)

This means doing well on the first section gives you access to higher scores on the second.

### Question Types

**Verbal Reasoning:**
- **Text Completion (TC)** — Fill in 1–3 blanks in a passage with the best word choices
- **Sentence Equivalence (SE)** — Select 2 words that complete a sentence with the same meaning
- **Reading Comprehension (RC)** — Single-answer, multiple-answer, and select-in-passage questions based on reading passages

**Quantitative Reasoning:**
- **Quantitative Comparison (QC)** — Compare two quantities (A vs B)
- **Multiple Choice** — Single-answer and multiple-answer variants
- **Numeric Entry** — Type a decimal or fraction as the answer
- **Data Interpretation** — Questions based on graphs, tables, or charts

**Analytical Writing:**
- **Issue Task** — Write an essay presenting your position on a given topic, supported by reasoning and examples

### Scoring

- **Verbal & Quant**: 130–170 in 1-point increments. Raw scores (number correct) are converted to scaled scores based on the difficulty level of questions answered.
- **AWA**: 0–6 in half-point increments. Evaluated on clarity of position, development of ideas, organisation, supporting evidence, and language control.

---

## Prerequisites

- **Python 3.9 or higher**
- **macOS**, **Linux**, or **Windows**
- **OpenRouter API key** (optional but recommended — needed for AWA essay scoring and question explanations). Get a free key at [openrouter.ai/keys](https://openrouter.ai/keys)

### Platform Notes

| Platform | Status | Notes |
|----------|--------|-------|
| macOS | ✅ Fully supported | Native Cocoa rendering |
| Linux | ✅ Supported | May need `libgtk-3-dev` and `libwebkit2gtk-4.0-dev` for wxPython |
| Windows | ✅ Supported | wxPython wheel installs via pip |

---

## Quick Start

### Automated Setup (Recommended)

```bash
git clone https://github.com/badalpardhi12/gre_with_ai.git
cd gre_with_ai
chmod +x setup.sh
./setup.sh
```

The setup script will:
1. Verify Python 3.9+
2. Create a virtual environment
3. Install all dependencies
4. Prompt for your OpenRouter API key
5. Initialise the database and seed the question bank

Then start the app:

```bash
source venv/bin/activate
python app.py
```

### Manual Setup

```bash
# Clone
git clone https://github.com/badalpardhi12/gre_with_ai.git
cd gre_with_ai

# Virtual environment
python3 -m venv venv
source venv/bin/activate

# Dependencies
pip install -r requirements.txt

# Environment
cp .env.example .env
# Edit .env and add your OPENROUTER_API_KEY

# Database & seed data
python scripts/seed_data.py
python scripts/import_vocab.py
python scripts/import_barrons_vocab.py
python scripts/import_awa_prompts.py
python scripts/import_external_quant.py
python scripts/import_cr_questions.py
python scripts/expand_questions.py

# Run
python app.py
```

---

## Usage

### Home Screen

The home screen offers three options:

- **Start Test** — Choose your test configuration:
  - **Test type**: Full mock, Verbal only, or Quant only
  - **Mode**: Simulation (timed, exam-like) or Learning (untimed, with explanations)
- **View Progress** — See your score history, topic-level accuracy, and trends
- **Settings** — Configure the LLM model, API key, and base URL

### Taking a Test

1. **Select test type and mode**, then click Start
2. **Instructions screen** shows what to expect in the upcoming section
3. **Answer questions** using the provided input controls:
   - Radio buttons for single-select
   - Checkboxes for multi-select
   - Text fields for numeric entry (supports decimals and fractions like `3/4`)
   - Rich text editor for AWA essays
4. **Navigate** with Previous / Next buttons, or the question navigation bar
5. **Mark for Review** to flag questions you want to revisit
6. **Review screen** appears at the end of each section — jump back to any question or submit
7. **Results screen** shows your scaled scores, section breakdown, and per-question details

### Test Modes

| Mode | Timer | Explanations | Scoring |
|------|-------|-------------|---------|
| **Simulation** | ✅ Enforced | After test only | Full adaptive scoring |
| **Learning** | ❌ Untimed | After each question | Section scores only |

### LLM Features

When an OpenRouter API key is configured:

- **AWA Scoring** — Essays are evaluated on a 6-point ETS-aligned rubric with feedback on position clarity, development, organisation, supporting evidence, and language control
- **Explanations** — Get detailed explanations for any question after completing a section
- **Settings** — Switch models at any time from the Settings dialog (Claude, GPT-4o, Gemini, etc.)

Without an API key, the app functions fully for Verbal and Quant sections — only AWA scoring and explanations are unavailable.

---

## Project Structure

```
gre_with_ai/
├── app.py                    # Application entry point
├── main_frame.py             # Main window & screen orchestration
├── config.py                 # Exam constants, paths, LLM config
├── setup.sh                  # Automated environment setup
├── requirements.txt          # Python dependencies
├── .env.example              # Environment variable template
│
├── models/
│   ├── database.py           # Peewee ORM models (14 tables)
│   └── exam_session.py       # Exam state machine & section tracking
│
├── services/
│   ├── question_bank.py      # Question selection & difficulty filtering
│   ├── scoring.py            # Answer checking & scaled score estimation
│   ├── awa_scorer.py         # AWA essay scoring (deterministic + LLM)
│   ├── llm_service.py        # OpenRouter API client
│   ├── explanation.py        # LLM-generated question explanations
│   └── analytics.py          # Session telemetry & topic breakdown
│
├── screens/                  # wxPython UI panels
│   ├── welcome_screen.py     # Home screen
│   ├── instructions_screen.py
│   ├── awa_screen.py         # Essay writing interface
│   ├── question_screen.py    # Question display & answer input
│   ├── review_screen.py      # End-of-section review
│   ├── results_screen.py     # Score cards & question details
│   ├── progress_screen.py    # Historical performance dashboard
│   └── llm_settings.py       # LLM configuration dialog
│
├── widgets/                  # Reusable UI components
│   ├── calculator.py         # On-screen calculator
│   ├── math_view.py          # KaTeX math rendering
│   ├── numeric_entry.py      # Decimal/fraction input
│   ├── question_nav.py       # Question navigation bar
│   └── timer.py              # Countdown timer
│
├── scripts/                  # Database seeding & import tools
│   ├── seed_data.py          # Create tables
│   ├── import_vocab.py       # Pervasive-GRE vocabulary (4,900 words)
│   ├── import_barrons_vocab.py  # Barrons 333 + 800 words
│   ├── import_awa_prompts.py # AWA issue prompts
│   ├── import_external_quant.py # Quantitative questions
│   ├── import_cr_questions.py   # Verbal / critical reasoning
│   ├── expand_questions.py   # Generate question variations
│   └── dataset_summary.py    # Print question bank statistics
│
├── data/
│   ├── gre_mock.db           # SQLite database (auto-created)
│   ├── llm_config.json       # Runtime LLM settings (auto-created)
│   └── external/             # Source CSV files for imports
│
├── resources/
│   └── katex/                # KaTeX library for math rendering
│
└── tests/
```

---

## Question Bank

The platform ships with a seeded question bank:

| Category | Count | Source |
|----------|-------|--------|
| Verbal questions | ~340 | Critical reasoning datasets, curated RC passages |
| Quantitative questions | ~260 | Algebra, geometry, arithmetic, data interpretation |
| AWA prompts | 136 | ETS-style issue prompts |
| Vocabulary words | ~9,600 | Pervasive-GRE, Barrons 333/800, GRE word collections |

Questions span all GRE subtypes and are tagged by topic (algebra, geometry, arithmetic, critical reasoning, etc.) for the progress dashboard's topic-level accuracy tracking.

---

## Configuration

### Environment Variables (`.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENROUTER_API_KEY` | No* | — | OpenRouter API key for LLM features |
| `OPENROUTER_BASE_URL` | No | `https://openrouter.ai/api/v1` | API base URL |
| `LLM_MODEL` | No | `anthropic/claude-sonnet-4-20250514` | Model identifier |
| `LLM_MAX_TOKENS` | No | `4096` | Max response tokens |

*Required for AWA scoring and explanations. The rest of the app works without it.

### Runtime Settings

LLM settings can also be changed from within the app via **Settings** on the home screen. Runtime changes are saved to `data/llm_config.json` and persist across sessions.

---

## Architecture

### Exam Flow

```
WelcomeScreen
    │ (select test type & mode)
    ▼
InstructionsScreen ◄─── (shown before each section)
    │
    ├──► AWAScreen (30 min essay)
    │       │ (submit → async LLM scoring)
    │       ▼
    ├──► QuestionScreen (V1/V2/Q1/Q2)
    │       │ (prev/next, mark for review)
    │       ▼
    │    ReviewScreen (jump to flagged Qs)
    │       │ (end section)
    │       ▼
    │    ── adaptive routing ──
    │       S1 perf < 40% → Easy S2
    │       S1 perf 40-70% → Medium S2
    │       S1 perf > 70% → Hard S2
    │       ▼
    │    (repeat for next section)
    ▼
ResultsScreen (scaled scores, breakdown, Q details)
```

### Key Components

- **ExamSession** (`models/exam_session.py`) — State machine tracking section order, current question, responses, timing, and marked questions
- **QuestionBank** (`services/question_bank.py`) — Selects questions by measure, difficulty band, and topic while avoiding repeats
- **ScoringEngine** (`services/scoring.py`) — Deterministic answer checking for all question types + scaled score estimation via lookup tables
- **AWAScorer** (`services/awa_scorer.py`) — Multi-signal essay evaluation: deterministic prechecks (word count, empty/off-topic detection) followed by LLM-based rubric scoring
- **LLMService** (`services/llm_service.py`) — OpenRouter client with sync and async (threaded) modes; any OpenAI-compatible API is supported

### Database

SQLite with WAL mode via Peewee ORM. 14 tables covering questions, sessions, responses, scores, vocabulary, and analytics. The database is auto-created on first run.

---

## Troubleshooting

### wxPython won't install on Linux

wxPython requires GTK development headers:

```bash
# Ubuntu / Debian
sudo apt install libgtk-3-dev libwebkit2gtk-4.0-dev

# Fedora
sudo dnf install wxGTK3-devel webkit2gtk3-devel

# Then install wxPython
pip install wxPython
```

If building from source is slow, use a pre-built wheel from [extras.wxpython.org](https://extras.wxpython.org/wxPython4/extras/linux/).

### "No questions available" error

Run the seed scripts to populate the question bank:

```bash
source venv/bin/activate
python scripts/seed_data.py
python scripts/import_vocab.py
python scripts/import_barrons_vocab.py
python scripts/import_awa_prompts.py
python scripts/import_external_quant.py
python scripts/import_cr_questions.py
python scripts/expand_questions.py
```

Or re-run `./setup.sh` to do this automatically.

### AWA scoring shows "N/A"

Ensure your OpenRouter API key is set:

1. Check `.env` has a valid `OPENROUTER_API_KEY`
2. Or configure it from the app: Home → Settings → enter your key
3. Verify it works by clicking "Test Connection" in Settings

### Database reset

To start fresh, delete the database and re-seed:

```bash
rm data/gre_mock.db*
./setup.sh
```

---

## License

MIT
