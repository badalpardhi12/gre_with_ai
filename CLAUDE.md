# GRE Mock Testing Platform — project notes for Claude

## Tech stack

- **Python 3.9.6** (venv at `venv/`; system python at `/usr/local/bin/python3`).
- **wxPython 4.2.4** desktop UI.
- **Peewee ORM + SQLite, dual-DB.** `models/database.py:20` binds Peewee to `data/gre_user.db` (runtime telemetry). `data/gre_mock.db` is the read-only content seed, accessed via raw `sqlite3` in `services/seed_sync.py`. Peewee migrations only target the user DB; the seed regenerates out of band.
- **LLM gateway:** OpenRouter via the OpenAI Python SDK at `services/llm_service.py:53-58`. Default model `anthropic/claude-opus-4`. AWA scorer at `services/awa_scorer.py` calls through this gateway.
- **No cloud services in production runtime** — everything runs locally on the user's machine.

## Python 3.9 constraints

- No `X | Y` union syntax (3.10+); use `Optional[X]` / `Union[X, Y]` from `typing`.
- No `match` statements (3.10+); use `if/elif` ladders.

## Two-database architecture (one-liner)

`gre_mock.db` ships content; `gre_user.db` records what the user did. The app copies seed → user on first launch, then `services/seed_sync.py` reconciles seed-authored content columns onto the user DB on subsequent launches whenever the seed fingerprint changes. Per-user state on `question` (pretest stats, IRT estimates, `created_at`) is preserved.

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
