#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# GRE Mock Test Platform — Environment Setup
# ──────────────────────────────────────────────────────────────
# Usage:  chmod +x setup.sh && ./setup.sh
#
# What this script does:
#   1. Checks Python version (3.9+ required)
#   2. Creates a virtual environment
#   3. Installs dependencies
#   4. Sets up the .env file (prompts for OpenRouter API key)
#   5. Initialises the SQLite database
#   6. Seeds the question bank (vocab, quant, verbal, AWA prompts)
# ──────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   GRE Mock Test Platform — Environment Setup    ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 1. Check Python ──────────────────────────────────────────
info "Checking Python version..."

PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [[ -z "$PYTHON" ]]; then
    fail "Python 3.9+ is required but not found. Install it from https://www.python.org"
fi

PY_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$("$PYTHON" -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$("$PYTHON" -c 'import sys; print(sys.version_info.minor)')

if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 9 ]]; then
    fail "Python 3.9+ is required (found $PY_VERSION). Please upgrade."
fi
ok "Python $PY_VERSION ($PYTHON)"

# ── 2. Create virtual environment ────────────────────────────
VENV_DIR="$PROJECT_DIR/venv"

if [[ -d "$VENV_DIR" ]]; then
    info "Virtual environment already exists at venv/"
else
    info "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Virtual environment created at venv/"
fi

# Activate
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
ok "Activated virtual environment"

# ── 3. Install dependencies ──────────────────────────────────
info "Upgrading pip..."
pip install --quiet --upgrade pip

info "Installing dependencies from requirements.txt..."
pip install --quiet -r requirements.txt
ok "All dependencies installed"

# wxPython may need platform-specific wheels on Linux
if [[ "$(uname)" == "Linux" ]]; then
    if ! python -c "import wx" 2>/dev/null; then
        warn "wxPython failed to import. On Linux you may need system packages:"
        warn "  Ubuntu/Debian: sudo apt install libgtk-3-dev libwebkit2gtk-4.0-dev"
        warn "  Then:  pip install wxPython"
        warn "  Or use a pre-built wheel: https://extras.wxpython.org/wxPython4/extras/linux/"
    fi
fi

# ── 4. Set up .env ───────────────────────────────────────────
if [[ -f "$PROJECT_DIR/.env" ]]; then
    info ".env file already exists — skipping"
else
    info "Setting up environment configuration..."
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"

    echo ""
    echo "  An OpenRouter API key is needed for LLM features (AWA scoring,"
    echo "  explanations). Get one free at: https://openrouter.ai/keys"
    echo ""
    read -rp "  Enter your OpenRouter API key (or press Enter to skip): " API_KEY

    if [[ -n "$API_KEY" ]]; then
        # Use a portable sed approach
        if [[ "$(uname)" == "Darwin" ]]; then
            sed -i '' "s|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=$API_KEY|" "$PROJECT_DIR/.env"
        else
            sed -i "s|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=$API_KEY|" "$PROJECT_DIR/.env"
        fi
        ok "API key saved to .env"
    else
        warn "Skipped — you can add it later by editing .env"
        warn "The app will still work but LLM features will be unavailable"
    fi
fi

# ── 5. Create data directories ───────────────────────────────
mkdir -p "$PROJECT_DIR/data/external"
mkdir -p "$PROJECT_DIR/data/questions"

# ── 6. Initialise database & seed data ───────────────────────
info "Initialising database..."
python scripts/seed_data.py
ok "Database initialised"

info "Importing vocabulary (Pervasive-GRE dictionary)..."
python scripts/import_vocab.py
ok "Vocabulary imported"

info "Importing Barrons vocabulary..."
python scripts/import_barrons_vocab.py
ok "Barrons vocabulary imported"

info "Importing AWA prompts..."
python scripts/import_awa_prompts.py
ok "AWA prompts imported"

info "Importing quantitative questions..."
python scripts/import_external_quant.py
ok "Quant questions imported"

info "Importing verbal / critical reasoning questions..."
python scripts/import_cr_questions.py
ok "Verbal questions imported"

info "Expanding question variations..."
python scripts/expand_questions.py
ok "Question expansion complete"

# ── 7. Show summary ──────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────────────"
python scripts/dataset_summary.py
echo "────────────────────────────────────────────────────"

echo ""
ok "Setup complete!"
echo ""
echo "  To start the application:"
echo ""
echo "    source venv/bin/activate"
echo "    python app.py"
echo ""
echo "  To configure LLM settings (model, API key) from within"
echo "  the app, use the Settings button on the home screen."
echo ""
