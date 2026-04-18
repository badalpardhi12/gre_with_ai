#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# GRE prep with AI — one-shot environment setup
# ──────────────────────────────────────────────────────────────────────────
# Usage:
#   chmod +x setup.sh
#   ./setup.sh
#
# What this script does, in order:
#   1. Detects platform (macOS / Linux / WSL); installs missing system
#      dependencies via brew (macOS) or apt (Debian/Ubuntu) where possible.
#   2. Verifies Python 3.9+ is available; installs it via brew on macOS if
#      missing.
#   3. Installs Git LFS and runs `git lfs install` + `git lfs pull` so the
#      shipped question / vocab / lessons database is fetched.
#   4. Creates the venv at ./venv and installs every dependency in
#      requirements.txt (plus pytest for the test suite).
#   5. Runs the test suite as a smoke check.
#   6. Prints clear next-step instructions (how to launch, how to add an
#      OpenRouter API key, how to re-run setup).
#
# Idempotent — safe to re-run after pulling new commits to refresh deps,
# fetch new LFS objects, and re-run tests.
#
# Logging: every line of stdout/stderr is echoed to the terminal AND
# appended to ./setup.log (timestamped) so failed installs leave a forensic
# trail. To skip the log file, run with NO_LOG=1 ./setup.sh.
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# ── tee everything to setup.log ──────────────────────────────────────────
# We do this before anything else so even early-exit failures are captured.
LOG_FILE="$PROJECT_DIR/setup.log"
if [[ "${NO_LOG:-0}" != "1" ]]; then
    # Wrap with `script -q /dev/null` would also work, but a plain `tee` is
    # portable and preserves color codes for the user's terminal while still
    # writing the raw stream to the log.
    if [[ -z "${SETUP_TEEING:-}" ]]; then
        export SETUP_TEEING=1
        # Re-exec ourselves with output piped through tee.
        {
            echo "──────────────────────────────────────────────────────────"
            echo "setup.sh started at $(date)"
            echo "  cwd:  $PROJECT_DIR"
            echo "  user: $(whoami)"
            echo "  shell: $SHELL"
            echo "  args: $*"
            echo "──────────────────────────────────────────────────────────"
        } >> "$LOG_FILE"
        # tee preserves stderr-merged output to BOTH the terminal (with ANSI
        # colour codes intact) and the log file. We don't add per-line
        # timestamps because BSD awk on macOS lacks strftime; the header
        # block above records the start time, which is usually enough to
        # correlate with any timestamps in subprocess output (pip, brew).
        exec > >(tee -a "$LOG_FILE") 2>&1
    fi
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
fail()  { echo -e "${RED}[err]${NC}  $*" >&2; exit 1; }
step()  { echo ""; echo -e "${BOLD}── $* ──${NC}"; }

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║                                                                  ║"
echo "║          GRE prep with AI — environment setup                    ║"
echo "║                                                                  ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
if [[ "${NO_LOG:-0}" != "1" ]]; then
    info "Full log: $LOG_FILE  (set NO_LOG=1 to skip)"
fi

# ── 0. Detect platform ────────────────────────────────────────────────────
step "Detecting platform"

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Darwin)  PLATFORM="macos" ;;
    Linux)   PLATFORM="linux" ;;
    *)       PLATFORM="other" ;;
esac
ok "Platform: $PLATFORM ($ARCH)"
info "uname -a: $(uname -a)"
info "Bash:     ${BASH_VERSION:-unknown}"
info "Git:      $(git --version 2>/dev/null || echo 'NOT INSTALLED')"
info "PWD:      $PROJECT_DIR"

PKG_MGR=""
if [[ "$PLATFORM" == "macos" ]]; then
    if command -v brew &>/dev/null; then
        PKG_MGR="brew"
        ok "Homebrew detected"
    else
        warn "Homebrew not found. Install it from https://brew.sh and re-run."
        warn "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
        fail "Homebrew is required on macOS for this setup script."
    fi
elif [[ "$PLATFORM" == "linux" ]]; then
    if command -v apt-get &>/dev/null; then
        PKG_MGR="apt"
        ok "apt detected"
    elif command -v dnf &>/dev/null; then
        PKG_MGR="dnf"
        ok "dnf detected"
    else
        warn "No supported package manager detected. You may need to install"
        warn "Python 3.9+ and git-lfs manually."
    fi
fi

# ── 1. Python 3.9+ ────────────────────────────────────────────────────────
step "Checking Python"

ensure_python() {
    for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
        if command -v "$candidate" &>/dev/null; then
            local v_major v_minor
            v_major=$("$candidate" -c 'import sys; print(sys.version_info.major)')
            v_minor=$("$candidate" -c 'import sys; print(sys.version_info.minor)')
            if [[ "$v_major" -ge 3 ]] && [[ "$v_minor" -ge 9 ]]; then
                PYTHON="$candidate"
                return 0
            fi
        fi
    done
    return 1
}

if ! ensure_python; then
    if [[ "$PKG_MGR" == "brew" ]]; then
        info "Python 3.9+ not found — installing python@3.12 via Homebrew (verbose)..."
        brew install --verbose python@3.12 || brew install python@3.12
        if ! ensure_python; then
            fail "Python install completed but Python 3.9+ still not on PATH. "\
"Open a new shell and re-run this script."
        fi
    elif [[ "$PKG_MGR" == "apt" ]]; then
        info "Installing python3 via apt (sudo will prompt)..."
        sudo apt-get update
        sudo apt-get install -y python3 python3-venv python3-pip
        ensure_python || fail "Python install failed."
    else
        fail "Python 3.9+ is required. Install it from https://www.python.org and re-run."
    fi
fi

PY_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')
ok "Python $PY_VERSION ($PYTHON)"

# ── 2. Git LFS ────────────────────────────────────────────────────────────
step "Setting up Git LFS for the shipped database"

ensure_lfs() {
    if command -v git-lfs &>/dev/null || git lfs version &>/dev/null; then
        return 0
    fi
    return 1
}

if ! ensure_lfs; then
    if [[ "$PKG_MGR" == "brew" ]]; then
        info "Installing git-lfs via Homebrew (verbose)..."
        brew install --verbose git-lfs || brew install git-lfs
    elif [[ "$PKG_MGR" == "apt" ]]; then
        info "Installing git-lfs via apt (sudo will prompt; verbose)..."
        sudo apt-get install -y git-lfs
    elif [[ "$PKG_MGR" == "dnf" ]]; then
        info "Installing git-lfs via dnf (verbose)..."
        sudo dnf install -y git-lfs
    else
        fail "git-lfs is required. Install it from https://git-lfs.com and re-run."
    fi
    ensure_lfs || fail "git-lfs install completed but the binary isn't on PATH."
fi
ok "git-lfs $(git lfs version | head -1)"

# Initialise once per machine (idempotent — safe to re-run).
info "Running: git lfs install --local"
git lfs install --local
ok "git lfs install --local"

# Only pull if the local DB is the LFS pointer placeholder rather than the
# real SQLite file. SQLite files start with "SQLite format 3"; LFS pointers
# start with "version https://git-lfs.github.com/spec/v1".
DB_PATH="$PROJECT_DIR/data/gre_mock.db"
if [[ -f "$DB_PATH" ]]; then
    HEAD_BYTES="$(head -c 16 "$DB_PATH" 2>/dev/null || true)"
    if [[ "$HEAD_BYTES" == "SQLite format 3" || "$HEAD_BYTES" == SQLite* ]]; then
        ok "Database already populated ($(du -h "$DB_PATH" | awk '{print $1}'))"
    else
        info "Database is an LFS pointer; pulling real content (verbose)..."
        GIT_TRACE=1 git lfs pull
        ok "Database pulled ($(du -h "$DB_PATH" | awk '{print $1}'))"
    fi
else
    info "No database file yet; pulling from LFS (verbose)..."
    GIT_TRACE=1 git lfs pull
    if [[ -f "$DB_PATH" ]]; then
        ok "Database pulled ($(du -h "$DB_PATH" | awk '{print $1}'))"
    else
        warn "git lfs pull completed but $DB_PATH is missing."
        warn "The app will create an empty DB on first launch; you'll need to"
        warn "run the import scripts in scripts/ to seed it."
    fi
fi

# ── 3. Virtual environment ────────────────────────────────────────────────
step "Creating virtual environment"

VENV_DIR="$PROJECT_DIR/venv"
if [[ -d "$VENV_DIR" ]]; then
    ok "venv/ already exists; reusing"
else
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Created venv/ with $PY_VERSION"
fi

# Activate in this shell (does not persist after the script exits).
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── 4. Dependencies ───────────────────────────────────────────────────────
step "Installing Python dependencies"

info "Upgrading pip (verbose)..."
pip install --upgrade pip
ok "pip upgraded to $(pip --version | awk '{print $2}')"

info "Installing packages from requirements.txt (verbose)..."
info "  $(wc -l < requirements.txt | tr -d ' ') packages pinned"
pip install -r requirements.txt
ok "Installed packages from requirements.txt"

# pytest is needed for the test suite but not for runtime.
if ! python -c 'import pytest' 2>/dev/null; then
    info "Installing pytest (test runner)..."
    pip install pytest
fi
ok "pytest available — $(python -c 'import pytest; print(pytest.__version__)')"

info "Installed packages snapshot:"
pip list --format=columns | sed 's/^/    /'

# Linux-specific wxPython sanity check — wheels for Linux are usually missing
# and require a system rebuild.
if [[ "$PLATFORM" == "linux" ]]; then
    if ! python -c 'import wx' 2>/dev/null; then
        warn "wxPython failed to import. On Debian/Ubuntu install:"
        warn "  sudo apt install libgtk-3-dev libwebkit2gtk-4.0-dev"
        warn "Then rerun:  pip install -r requirements.txt"
    fi
fi

# ── 5. Database init + smoke test ─────────────────────────────────────────
step "Initialising database (creates tables + applies migrations)"
python -c "from models.database import init_db; init_db(); print('  database ready')"
ok "Schema migrations applied"

step "Running the test suite (verbose)"
if python -m pytest tests/ -v --tb=short; then
    ok "Tests passed"
else
    warn "Some tests failed. The app may still launch; investigate before relying on results."
fi

# ── 6. Optional: prompt for OpenRouter key ────────────────────────────────
step "OpenRouter API key (optional — needed for AI tutor / AWA scoring)"

if [[ -f "$PROJECT_DIR/.env" ]] && grep -q '^OPENROUTER_API_KEY=sk-' "$PROJECT_DIR/.env"; then
    ok ".env already has an OpenRouter key"
elif [[ -f "$PROJECT_DIR/data/llm_config.json" ]] && grep -q '"api_key": "sk-' "$PROJECT_DIR/data/llm_config.json"; then
    ok "data/llm_config.json already has an OpenRouter key"
else
    if [[ ! -f "$PROJECT_DIR/.env" ]] && [[ -f "$PROJECT_DIR/.env.example" ]]; then
        cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    fi
    echo ""
    echo "  Get a free key at: ${BOLD}https://openrouter.ai/keys${NC}"
    echo "  (You can also add it later from the in-app Settings dialog.)"
    echo ""
    read -rp "  Paste your OpenRouter key here, or press Enter to skip: " API_KEY || true
    if [[ -n "${API_KEY:-}" ]]; then
        if [[ "$PLATFORM" == "macos" ]]; then
            sed -i '' "s|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=$API_KEY|" "$PROJECT_DIR/.env"
        else
            sed -i "s|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=$API_KEY|" "$PROJECT_DIR/.env"
        fi
        ok "Saved key to .env"
    else
        warn "Skipped — drills, mock tests, and vocab will still work without a key."
        warn "AI features (AWA scoring, per-question tutor, study plan) will be"
        warn "disabled until you set one via the in-app Settings dialog."
    fi
fi

# ── 7. Done ───────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo -e "║  ${GREEN}Setup complete${NC}                                                  ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "  Launch the app:"
echo ""
echo -e "    ${BOLD}source venv/bin/activate${NC}"
echo -e "    ${BOLD}python app.py${NC}"
echo ""
echo "  First launch shows a 3-step onboarding wizard (you can skip)."
echo "  After that the home is the ${BOLD}Today${NC} tab — one big CTA per day."
echo ""
echo "  Five sidebar tabs (Cmd+1..5):"
echo "    • Today     — one primary action + plan + forecast"
echo "    • Learn     — mastery heatmap + lesson per subtopic"
echo "    • Practice  — Quick Drill / Section Test / Full Mock"
echo "    • Vocab     — daily SRS flashcards"
echo "    • Insights  — forecast trend + history + study plan"
echo ""
echo "  Re-run this script anytime — it's idempotent and updates deps."
echo ""
