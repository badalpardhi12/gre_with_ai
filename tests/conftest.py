"""
Pytest fixtures for the GRE prep test suite.

Each test gets a fresh, empty SQLite DB at a tmp_path so changes don't
persist into `data/gre_mock.db`. We swap `config.DB_PATH` and rebind
`models.database.db` before importing the model classes; the
ALL_TABLES list is recreated against the new DB.
"""
import importlib
import os
import sys

import pytest


def pytest_configure(config):
    """Register custom markers used by the dedup test suites."""
    config.addinivalue_line(
        "markers",
        "slow: tests that load heavy ML models or require GPU; "
        "skipped by default in fast pre-commit runs (use -m \"not slow\").",
    )
    config.addinivalue_line(
        "markers",
        "timeout(seconds): per-test wall-clock budget (informational only "
        "unless pytest-timeout is installed).",
    )

# Make the project root importable when pytest is run from any cwd.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Provide a clean SQLite database for each test."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr("config.DB_PATH", db_file)
    # Also point the seed path at a nonexistent file so the reconcile
    # step inside ``init_db`` no-ops (``reconcile_if_stale`` returns
    # ``skipped: missing_path`` when the seed isn't on disk). Without
    # this, the reconcile copies ~5,000 questions from the real shipped
    # seed into the test's fresh DB, swamping any hand-built fixture.
    monkeypatch.setattr("config.SEED_DB_PATH", tmp_path / "no_seed.db")

    # Force a fresh import of models so the module-level Database picks up
    # the patched DB_PATH. Also evict any service modules that captured
    # `from models.database import db, …` at import time, otherwise their
    # bindings stay pointing at the previous test's DB.
    def _evict_models_services():
        for prefix in ("models", "services"):
            for mod in [m for m in list(sys.modules) if m.startswith(prefix + ".")
                        or m == prefix]:
                del sys.modules[mod]

    _evict_models_services()

    from models.database import db, init_db, ALL_TABLES  # noqa: F401
    init_db()
    yield db
    if not db.is_closed():
        db.close()
    # Evict again at teardown so the next non-temp_db test (e.g. ones that
    # call init_db() at module scope against the live DB) gets a fresh import
    # that re-binds against the now-restored config.DB_PATH. Without this,
    # the cached module's ``db`` SqliteDatabase object is still pointed at
    # the (now-vanished) tmp_path and downstream tests see "no rows" because
    # they're querying a different SQLite file than they meant to.
    _evict_models_services()


@pytest.fixture
def scoring_engine():
    from services.scoring import ScoringEngine
    return ScoringEngine
