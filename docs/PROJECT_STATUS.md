# Project Status — START HERE (2026-06-17)

Single entry point for a fresh session. Read this first, then `CLAUDE.md`.

## What this is
A local, single-user **GRE mock-testing desktop app** (Python 3.9.6 / wxPython
4.2.4 / Peewee + SQLite dual-DB). No cloud at runtime. Run: `venv/bin/python
app.py`. Test: `venv/bin/python -m pytest tests/` (use
`WXSUPPRESS_SIZER_FLAGS_CHECK=1` for wx UI tests on macOS).

## Current state (all on `main`, pushed to origin)
Two large efforts shipped:

### 1. Data/content production-hardening (migrations 039–044)
- **Option grafts** repaired (ai_synthetic_v2 mcq_multi had options grafted from
  neighboring questions) — `scripts/audit_option_graft.py`, migration 039.
- **Phantom figures** fixed: renderer now draws geometry from `render_spec` via
  `services/figures/geometry.py` — `scripts/audit_figure_render.py`, migration 040.
- **In-test repeats**: `question.duplicate_group_id` + assembly group-dedup +
  cross-mock window N=3 — migration 041.
- **GRE faithfulness**: QC options canonicalized — `scripts/audit_faithfulness.py`,
  migration 042.
- **Encoding / Kaplan reclassify** — migration 043.
- **User-reported #42/#43/#44** (q3196, q5379 graft, q5400 retire) — migration 044.
- **Regression gate**: `scripts/run_all_audits.py` (+ `tests/test_production_gate.py`)
  must report 0 for every corruption class. Run it before shipping data changes.

### 2. ETS "Test Preview Tool" UI (the in-test exam UI)
The in-test screens replicate the real GRE Test Preview Tool. **Exam mode is ON
for ALL question-answering sessions, fullscreen.** Study aids (Show Answer / Ask
AI Tutor / explanations) are NOT shown in-test — they live in the post-session
review (`screens/answer_review_dialog.py`).
- Shared chrome: `widgets/exam_chrome.ExamChrome` = charcoal header (`*gre Test
  Preview Tool`) + maroon hairline + top-right tool ribbon
  (Exit·Calc·Mark·Review·Help·Back·Next, owner-drawn `widgets/exam_tool_button`)
  + pink section bar carrying "Section X of Y | Question N of M" + the timer.
- Content: black-bordered white box on a gray page; **no bottom numbered
  navigator** (nav = Back/Next + Review); directions in a top gray band (verbal
  long) + a bottom-center gray pill; blue passage/data title bar.
- Per-type: QC = figure + centered common-info + full-width two-column Quantity
  A/B; data table/chart questions use the **two-pane split** with the figure
  filling the left pane (`_should_split`/`_is_data_presentation` in
  `screens/question_screen.py`); geometry figures stay inline; TC = bordered
  per-blank choice tables; Select-in-Passage = clickable highlightable sentences;
  Numeric Entry = white box / stacked fraction with $/unit.
- Owner-drawn controls (`widgets/exam_choice` ovals/squares, `widgets/exam_button`,
  calculator keys) because native wx controls ignore colors under macOS dark mode.
- Calculator (`widgets/calculator.py`): real ETS layout + MR/MC/M+ verified
  functional + PEMDAS + Transfer Display gating; floating draggable window.
- AWA + section transitions: `screens/awa_screen.py`, `screens/transition_screen.py`.

## Verify the UI visually
`venv/bin/python scripts/ui_screenshot.py <name|all>` → PNGs in `/tmp/ets_ui/`.
Names: qc, mcq_single, mcq_multi, numeric_entry, numeric_entry_fraction, tc,
rc_single, rc_select_passage, data_interp, mcq_table, mcq_chart, calculator.
(macOS WebView can't be grabbed by wx DCs — the harness uses `screencapture -R`.)

## Reference docs / memory
- `docs/PROJECT_STATUS.md` (this file) — start here.
- `docs/production_hardening_2026_06_01.md` — data fixes + the seed→user update
  pipeline (content-signature reconcile, FK-aware tables) + documented follow-ups.
- `docs/gre_ui_spec_2026_06.md` — UI spec; **navy/footer-navigator sections are
  SUPERSEDED** (see its banner); directions/calculator/control-shape substance
  still valid. Current chrome = read the `widgets/exam_*` source.
- Auto-memory: `~/.claude/projects/.../memory/` — `MEMORY.md` (index),
  `production_hardening_2026_06.md`, `ets_exam_ui_2026_06.md`, `dual_db_architecture.md`.

## Known follow-ups (not blocking)
- Taxonomy backfill: 137 live items (mostly Kaplan) — run
  `scripts/llm_judge_taxonomy.py` (needs LLM; internal routing metadata only).
- 16 `unescaped_html` markdown-table DI prompts need a markdown→HTML table pass.
- Pre-existing unrelated test failure:
  `tests/test_minhash_dedup.py::test_held_out_detection_f1_at_persisted_threshold`
  (ML F1 threshold; fails on `main` historically — ignore).
- A user whose local seed was dirtied by the OLD pre-044 runtime seed-writes
  must do a one-time `git checkout -- data/gre_mock.db && git pull`; afterwards
  pulls stay clean (runtime no longer writes the tracked seed).

## Gotchas (will bite a fresh session)
- macOS **dark mode** ignores `SetBackgroundColour`/`SetForegroundColour` on
  native wx.Button/RadioButton/CheckBox/TextCtrl → owner-draw exam controls.
- Never combine `wx.EXPAND` with `wx.ALIGN_*` in one sizer flag (asserts).
- **bleach strips inline `style=`** → put exam HTML styling in CSS classes in
  `widgets/math_view.py`'s template, not inline.
- KaTeX delimiters are `$$`/`\(`/`\[` only — NOT single `$`.
- The shipped seed is **read-only at runtime**; rebuild it with
  `GRE_BUILD_SEED=1 venv/bin/python -c "from models.migrations import apply_pending_migrations; apply_pending_migrations()"` then commit `data/gre_mock.db`.
- Full-suite test count baseline: ~1021 pass, 1 skip, 1 pre-existing fail.
