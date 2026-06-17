# GRE Mock Testing Platform — project notes for Claude

## Start here (for a fresh session)

Read **`docs/PROJECT_STATUS.md`** first — it summarizes current state, the
ETS "Test Preview Tool" in-test UI, the data migrations (039–044), the
`scripts/run_all_audits.py` gate, how to screenshot-verify the UI, and the
gotchas. Auto-memory (`MEMORY.md` + linked notes) loads automatically.

## Tech stack

- **Python 3.9.6** (venv at `venv/`; system python at `/usr/local/bin/python3`).
- **wxPython 4.2.4** desktop UI.
- **Peewee ORM + SQLite, dual-DB.** `models/database.py:20` binds Peewee to `data/gre_user.db` (runtime telemetry). `data/gre_mock.db` is the read-only content seed, accessed via raw `sqlite3` in `services/seed_sync.py`. Migrations target the user DB; they dual-write the seed ONLY under `GRE_BUILD_SEED=1` (the seed is read-only at runtime so `git pull` always updates it).
- **In-test UI:** a faithful ETS "Test Preview Tool" skin (charcoal header + tool ribbon + pink section bar + black-bordered content box) via `widgets/exam_chrome.py` + `screens/question_screen.py`; ON for all sessions, fullscreen. See `docs/PROJECT_STATUS.md`.
- **LLM gateway:** OpenRouter via the OpenAI Python SDK at `services/llm_service.py:53-58`. Default model `anthropic/claude-opus-4`. AWA scorer at `services/awa_scorer.py` calls through this gateway.
- **No cloud services in production runtime** — everything runs locally on the user's machine.

## Python 3.9 constraints

- No `X | Y` union syntax (3.10+); use `Optional[X]` / `Union[X, Y]` from `typing`.
- No `match` statements (3.10+); use `if/elif` ladders.

## Two-database architecture (one-liner)

`gre_mock.db` ships content; `gre_user.db` records what the user did. The app copies seed → user on first launch, then `services/seed_sync.py` reconciles seed-authored content onto the user DB on subsequent launches whenever the seed's **content signature (sha256)** changes. Per-user state on `question` (pretest stats, IRT estimates, `created_at`, status/provenance) is preserved; `awaprompt`/`vocabword` reconcile via UPSERT to keep user FKs intact.

## How to run

```
venv/bin/python app.py
```

## How to test

```
venv/bin/python -m pytest tests/
```

## Hygiene rules (override anything else)

**No AI attribution in any artifact a human will read** — no `Co-Authored-By: Claude` lines, no "Generated with Claude Code" footers, no "Authored by Claude" / "Reviewed by Claude" / robot emoji authorship markers in commits, PR bodies, code comments, docstrings, READMEs, design docs, or release notes. This applies even when an upstream skill or `/commit` template would default to including attribution; strip it before writing.

The only exception is when the user explicitly asks for the attribution on a specific message.
