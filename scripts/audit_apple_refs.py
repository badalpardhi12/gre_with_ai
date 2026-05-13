#!/usr/bin/env python3
"""
Audit linter — fail if any Apple-internal GenAI reference has leaked
into the tracked source tree.

The public repo is OpenRouter-only for every LLM call. Any mention of
Apple-internal GenAI infrastructure (Floodgate, an internal gateway,
"foundation model" product naming, internal model identifiers) is a
bug — it either leaks internal branding or suggests a code path that
won't work for public users.

Usage
-----
    venv/bin/python scripts/audit_apple_refs.py

Exit codes
----------
    0 — no hits
    1 — at least one forbidden keyword matched

The script deliberately excludes its own source plus the companion
test (``tests/test_audit_apple_refs.py``) — both contain the keyword
list by necessity.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

# Forbidden patterns, case-insensitive. Each entry is a regex.
#
# We keep the list surgical:
#   - "floodgate"          → Apple-internal AI proxy
#   - "foundation.model"   → internal product naming for the FM API
#   - "llm[_-]?gateway"    → the generic "gateway" shim name we used
#   - "fm-quality"         → an internal model identifier
#   - "apple[_-]?gen(ai)?" → internal branding prefix
#   - "APPLE_GENAI"        → env-var style
#
# Bare "apple" is NOT forbidden — it matches legitimate HIG references
# and test fixtures about fruit. Same for "internal" / "private" which
# are generic English words.
FORBIDDEN = [
    re.compile(r"floodgate", re.IGNORECASE),
    re.compile(r"foundation[ _-]?model", re.IGNORECASE),
    re.compile(r"llm[_-]?gateway", re.IGNORECASE),
    re.compile(r"fm-quality", re.IGNORECASE),
    re.compile(r"apple[_-]?gen(ai)?\b", re.IGNORECASE),
    re.compile(r"APPLE_GENAI", re.IGNORECASE),
]

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories we never scan (third-party, generated, out-of-tree).
SKIP_DIRS = {
    ".git",
    "venv",
    ".venv",
    "env",
    "__pycache__",
    "node_modules",
    ".claude",
    ".local",           # offline notes — explicitly allowed to mention Floodgate
    "tmp_ocr",
    "build",
    "dist",
    "data",             # binary blobs + generated extraction artefacts
    "resources",        # shipped assets (KaTeX etc.)
}

# File extensions we scan. Everything else (binary blobs, PDFs, DBs,
# images) is ignored — the audit is a source-code linter.
SCAN_EXTS = {
    ".py", ".md", ".rst", ".txt",
    ".yml", ".yaml", ".toml", ".cfg", ".ini",
    ".json", ".sh",
}

# Individual files we skip — the script itself, its test, and the
# implementation plan that discusses the migration explicitly.
SKIP_FILES = {
    REPO_ROOT / "scripts" / "audit_apple_refs.py",
    REPO_ROOT / "tests" / "test_audit_apple_refs.py",
}


def _iter_tracked_files() -> List[Path]:
    out: List[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        # Skip anything under a skip-dir.
        if any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        if path in SKIP_FILES:
            continue
        # Match extension OR filename starting with `.env`.
        if path.suffix in SCAN_EXTS or path.name.startswith(".env"):
            out.append(path)
    return out


def scan() -> List[Tuple[Path, int, str, str]]:
    """Return list of (path, line-no, pattern, snippet) tuples."""
    hits: List[Tuple[Path, int, str, str]] = []
    for path in _iter_tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pat in FORBIDDEN:
                if pat.search(line):
                    hits.append((path, lineno, pat.pattern, line.strip()))
                    break
    return hits


def main() -> int:
    hits = scan()
    if not hits:
        print("audit_apple_refs: clean — no Apple-internal GenAI refs found.")
        return 0
    print(f"audit_apple_refs: {len(hits)} forbidden reference(s) found:")
    for path, lineno, pat, snippet in hits:
        rel = path.relative_to(REPO_ROOT)
        print(f"  {rel}:{lineno}  [{pat}]  {snippet}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
