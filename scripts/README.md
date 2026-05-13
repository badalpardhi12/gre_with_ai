# scripts/

Ad-hoc maintenance scripts. Nothing here is run on app launch —
every entry point is manual / scheduled.

## `recalibrate_irt.py` — 2PL IRT recalibration (Phase 4 P1)

Fits a 2PL IRT model on graded `Response` rows and writes the
estimated `irt_a_estimate` / `irt_b_estimate` back onto `Question`.

**Prerequisites:**

```bash
venv/bin/pip install girth   # MIT, scipy-only, ~2 MB
```

**Usage:**

```bash
# Dry-run: fit + log the summary, don't touch the DB.
venv/bin/python scripts/recalibrate_irt.py --dry-run --min-responses 50

# Real run (writes back).
venv/bin/python scripts/recalibrate_irt.py --min-responses 50
```

Items with fewer than `--min-responses` graded responses are skipped —
their Elo rating from `services.rating_service` remains the best
available difficulty estimate. The script is idempotent; re-running
over unchanged data produces the same estimates modulo numerical
noise.

Because the local app is single-user, the "person" axis for the
marginal-likelihood estimator is `Session.id` — each session is
treated as an independent ability draw, which gives girth enough
columns to identify both discrimination (`a`) and difficulty (`b`).

## `audit_data_corruption.py`

Spot-check tool for detecting broken foreign-key chains in a local
DB. See the file header for usage.

## `migrate_to_user_db.sh`

One-shot migration for the v1 → v2 user DB layout; retained for
historical reference.
