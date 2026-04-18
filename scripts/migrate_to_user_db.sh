#!/usr/bin/env bash
# Migrate from the legacy single-DB layout (data/gre_mock.db is the
# user-writable runtime DB AND the tracked LFS seed) to the new split
# layout (data/gre_mock.db = read-only seed, data/gre_user.db =
# gitignored runtime DB).
#
# Without this script, a `git pull` after the split would refuse to
# overwrite the user's locally-modified gre_mock.db. Run this BEFORE
# pulling:
#
#     bash scripts/migrate_to_user_db.sh
#     git pull
#
# Idempotent: safe to re-run. Preserves your sessions/responses/mastery
# by copying your current gre_mock.db to gre_user.db before resetting
# the tracked file.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f data/gre_mock.db ]]; then
    echo "data/gre_mock.db not found — nothing to migrate."
    exit 0
fi

# Detect locally-modified seed file. `git status --porcelain` prints
# `_M data/gre_mock.db` when the working tree differs from the index.
if git status --porcelain data/gre_mock.db | grep -q '^.M'; then
    echo "Detected local modifications to data/gre_mock.db (legacy layout)."
    if [[ ! -f data/gre_user.db ]]; then
        echo "→ Backing up your data to data/gre_user.db (preserves your"
        echo "   sessions, responses, mastery, flag reports, streak)."
        cp data/gre_mock.db data/gre_user.db
    else
        echo "→ data/gre_user.db already exists — keeping it as-is."
    fi
    echo "→ Resetting data/gre_mock.db to the tracked version so"
    echo "   future pulls aren't blocked."
    git checkout -- data/gre_mock.db
    echo ""
    echo "Done. Your runtime data lives in data/gre_user.db (gitignored)."
    echo "You can now safely run: git pull"
else
    echo "data/gre_mock.db is already clean — no migration needed."
    if [[ ! -f data/gre_user.db ]]; then
        echo "On next app launch the bootstrap will copy the seed into"
        echo "data/gre_user.db automatically."
    fi
fi
