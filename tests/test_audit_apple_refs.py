"""
Guardrail test — ensures no Apple-internal GenAI references leak into
the tracked source tree. Runs scripts/audit_apple_refs.py as a
subprocess and asserts exit 0 + no stderr noise.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "audit_apple_refs.py"


def test_audit_script_exists():
    assert AUDIT_SCRIPT.is_file(), (
        f"expected audit linter at {AUDIT_SCRIPT}"
    )


def test_audit_clean():
    """The checked-in source tree must pass the Apple-refs audit."""
    result = subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"audit_apple_refs.py failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_audit_detects_hit(tmp_path, monkeypatch):
    """Smoke-test the linter itself — drop a fake file containing a
    forbidden keyword into a fresh tree and confirm the scanner finds
    it. We import the module and call its ``scan`` function directly
    against a monkeypatched repo root so we don't have to copy the
    whole tree."""
    # Seed a throwaway "repo" with one offending file.
    bad = tmp_path / "offender.py"
    bad.write_text("# this references Floodgate which is forbidden\n")

    # Import and rebind REPO_ROOT + SKIP_DIRS so scan() sees our tmp.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "audit_apple_refs", AUDIT_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "SKIP_FILES", set())
    hits = module.scan()
    assert hits, "linter failed to detect a seeded Floodgate reference"
    assert any("floodgate" in h[2].lower() for h in hits)
