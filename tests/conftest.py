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

# Make the project root importable when pytest is run from any cwd.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Provide a clean SQLite database for each test."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr("config.DB_PATH", db_file)

    # Force a fresh import of models so the module-level Database picks up
    # the patched DB_PATH. Also evict any service modules that captured
    # `from models.database import db, …` at import time, otherwise their
    # bindings stay pointing at the previous test's DB.
    for prefix in ("models", "services"):
        for mod in [m for m in list(sys.modules) if m.startswith(prefix + ".")
                    or m == prefix]:
            del sys.modules[mod]

    from models.database import db, init_db, ALL_TABLES  # noqa: F401
    init_db()
    yield db
    if not db.is_closed():
        db.close()


@pytest.fixture
def scoring_engine():
    from services.scoring import ScoringEngine
    return ScoringEngine
