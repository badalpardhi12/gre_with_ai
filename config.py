"""
Configuration module — loads .env and defines exam constants.
"""
import os
import shutil
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# ── Database path resolution ─────────────────────────────────────────
# `gre_mock.db` is the SEED — shipped via Git LFS, never written by the
# running app. The actual runtime DB is `gre_user.db` (gitignored), so
# user state (responses, sessions, mastery, flags, streak) lives outside
# version control. This split fixed the bug where every launch left
# `data/gre_mock.db` "modified" in `git status`, blocking pulls.
SEED_DB_PATH = DATA_DIR / "gre_mock.db"
DB_PATH = DATA_DIR / "gre_user.db"


def _bootstrap_user_db():
    """If the user-writable DB is missing, copy the shipped seed into
    place. Idempotent — once `gre_user.db` exists this is a no-op.

    Runs at import time so every module that imports `DB_PATH` sees the
    correct, populated file.
    """
    if DB_PATH.exists():
        return
    if not SEED_DB_PATH.exists():
        # No seed available either — let init_db create an empty DB.
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(SEED_DB_PATH), str(DB_PATH))


_bootstrap_user_db()

QUESTIONS_DIR = DATA_DIR / "questions"
RESOURCES_DIR = BASE_DIR / "resources"
KATEX_DIR = RESOURCES_DIR / "katex"

# API Keys
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# ── GRE Exam Timing (seconds) ─────────────────────────────────────────
# Post-September 22, 2023 format
AWA_TIME = 30 * 60          # 30 minutes
VERBAL_S1_TIME = 18 * 60    # 18 minutes
VERBAL_S2_TIME = 23 * 60    # 23 minutes
QUANT_S1_TIME = 21 * 60     # 21 minutes
QUANT_S2_TIME = 26 * 60     # 26 minutes

# ── GRE Exam Question Counts ──────────────────────────────────────────
VERBAL_S1_COUNT = 12
VERBAL_S2_COUNT = 15
QUANT_S1_COUNT = 12
QUANT_S2_COUNT = 15

# ── Score Ranges ──────────────────────────────────────────────────────
VERBAL_SCORE_MIN = 130
VERBAL_SCORE_MAX = 170
QUANT_SCORE_MIN = 130
QUANT_SCORE_MAX = 170
AWA_SCORE_MIN = 0.0
AWA_SCORE_MAX = 6.0
AWA_SCORE_INCREMENT = 0.5

# ── Section-Level Adaptation Thresholds ───────────────────────────────
# Percentage correct in S1 to determine S2 difficulty
ADAPT_EASY_THRESHOLD = 0.40     # Below 40% → easy S2
ADAPT_HARD_THRESHOLD = 0.70     # Above 70% → hard S2
# Between 40-70% → medium S2

# ── AWA Constraints ──────────────────────────────────────────────────
AWA_MIN_WORDS = 50
AWA_MAX_WORDS = 1000

# ── UI Constants ─────────────────────────────────────────────────────
MIN_WINDOW_WIDTH = 1280
MIN_WINDOW_HEIGHT = 800
TIMER_WARNING_SECONDS = 5 * 60  # 5 minute warning (red timer)

# ── LLM Settings ─────────────────────────────────────────────────────
# Default to Opus 4 via OpenRouter for high-quality tutoring/study planning.
# Any OpenRouter-supported model id works (anthropic/claude-opus-4,
# anthropic/claude-sonnet-4, openai/gpt-4o, etc.). Override via env or Settings.
LLM_MODEL = os.getenv("LLM_MODEL", "anthropic/claude-opus-4")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))

# ── LLM Configuration file (runtime overrides) ───────────────────────
LLM_CONFIG_PATH = DATA_DIR / "llm_config.json"


def load_llm_config():
    """Load runtime LLM config from JSON file, falling back to env/defaults.

    A corrupted (truncated, hand-edited, partially-written) JSON file used to
    crash the app at launch. We now log a warning and fall back to defaults.
    """
    import json
    config = {
        "api_key": OPENROUTER_API_KEY,
        "base_url": OPENROUTER_BASE_URL,
        "model": LLM_MODEL,
        "max_tokens": LLM_MAX_TOKENS,
    }
    if LLM_CONFIG_PATH.exists():
        try:
            with open(LLM_CONFIG_PATH) as f:
                overrides = json.load(f)
            if not isinstance(overrides, dict):
                raise ValueError("llm_config.json is not a JSON object")
            # Drop empty strings/None (legacy hygiene), but preserve falsey
            # booleans so a saved `include_ai_synthetic=False` survives the
            # round-trip. Anything that isn't None or "" wins.
            for k, v in overrides.items():
                if v is None or v == "":
                    continue
                config[k] = v
        except (ValueError, OSError) as exc:
            # Avoid importing services.log here to keep config.py importable
            # before the data dir exists. stderr is acceptable for startup.
            import sys
            print(f"[warn] Could not parse {LLM_CONFIG_PATH}: {exc}; "
                  "falling back to env/defaults.", file=sys.stderr)
    return config


def save_llm_config(api_key=None, base_url=None, model=None, max_tokens=None,
                    include_ai_synthetic=None, font_size_multiplier=None):
    """Persist runtime LLM settings to JSON.

    Writes atomically (temp file + os.replace) so an interrupted save can't
    corrupt the file. Restricts permissions to 0600 so other local users
    can't read the API key.

    `include_ai_synthetic` is a UI-level toggle that controls whether items
    with `Question.source='ai_synthetic'` show up in test assembly. We
    co-locate it in `llm_config.json` rather than introducing a new
    UserPreferences table — this app is single-user, the file already has
    safe atomic writes, and the toggle naturally lives next to the
    settings dialog that exposes it.
    """
    import json
    import os
    LLM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if LLM_CONFIG_PATH.exists():
        try:
            with open(LLM_CONFIG_PATH) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (ValueError, OSError):
            # Treat corrupted input as empty; we're about to overwrite anyway.
            data = {}
    if api_key is not None:
        data["api_key"] = api_key
    if base_url is not None:
        data["base_url"] = base_url
    if model is not None:
        data["model"] = model
    if max_tokens is not None:
        data["max_tokens"] = max_tokens
    if include_ai_synthetic is not None:
        data["include_ai_synthetic"] = bool(include_ai_synthetic)
    if font_size_multiplier is not None:
        data["font_size_multiplier"] = clamp_font_multiplier(font_size_multiplier)

    tmp_path = LLM_CONFIG_PATH.with_suffix(LLM_CONFIG_PATH.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, LLM_CONFIG_PATH)
    try:
        os.chmod(LLM_CONFIG_PATH, 0o600)
    except OSError:
        # Best-effort on platforms (e.g. Windows) where chmod semantics differ.
        pass


# ── User preferences (UI toggles) ────────────────────────────────────
# Keep narrow surface; add new keys here as they appear in settings.
USER_PREF_DEFAULTS = {
    "include_ai_synthetic": True,   # show synthetic items in mock tests
    "font_size_multiplier": 1.0,    # zoom factor for question/explanation text
                                    # (1.0 = baseline; user-adjustable via
                                    # View menu / Cmd-+ / Cmd-- / Cmd-0).
                                    # Clamped to FONT_SIZE_RANGE on save.
}

# Hard bounds on the font multiplier; the View menu enforces these.
FONT_SIZE_MIN = 0.8
FONT_SIZE_MAX = 1.8
FONT_SIZE_STEP = 0.1


def clamp_font_multiplier(value):
    """Coerce a font multiplier into the allowed range, default 1.0 on bad input."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    if v != v:  # NaN
        return 1.0
    return max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, v))


def load_user_prefs():
    """Return UI preference flags, falling back to USER_PREF_DEFAULTS.

    Reads the same `llm_config.json` that backs `load_llm_config` — see
    `save_llm_config` for the rationale on co-locating these. We re-read
    on every call so a Settings-dialog change takes effect without an
    app restart (matches the LLM-config behavior).
    """
    cfg = load_llm_config()
    out = dict(USER_PREF_DEFAULTS)
    for key in USER_PREF_DEFAULTS:
        if key in cfg:
            out[key] = cfg[key]
    # Clamp the font multiplier in case the JSON was hand-edited out of bounds.
    out["font_size_multiplier"] = clamp_font_multiplier(out.get("font_size_multiplier", 1.0))
    return out


def save_user_pref(key, value):
    """Persist a single preference key. Wraps `save_llm_config`."""
    if key not in USER_PREF_DEFAULTS:
        raise KeyError(f"Unknown user pref: {key!r}")
    if key == "include_ai_synthetic":
        save_llm_config(include_ai_synthetic=value)
    elif key == "font_size_multiplier":
        save_llm_config(font_size_multiplier=clamp_font_multiplier(value))
    else:  # pragma: no cover — guarded above
        raise KeyError(f"No save handler for pref: {key!r}")
