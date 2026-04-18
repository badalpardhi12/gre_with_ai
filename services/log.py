"""
Centralized logging helper.

Returns a stdlib logger configured to write to both `data/gre_app.log`
(rotating) and stderr at INFO level. Used by services and screens that the
audit flagged for silently swallowing exceptions; scripts continue to use
bare `print` for human-readable progress output.
"""
import logging
from logging.handlers import RotatingFileHandler

from config import DATA_DIR


_LOG_PATH = DATA_DIR / "gre_app.log"
_CONFIGURED = False


def _ensure_configured():
    global _CONFIGURED
    if _CONFIGURED:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_h = RotatingFileHandler(
        _LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_h.setLevel(logging.INFO)
    file_h.setFormatter(fmt)

    stderr_h = logging.StreamHandler()
    stderr_h.setLevel(logging.WARNING)
    stderr_h.setFormatter(fmt)

    root = logging.getLogger("gre_app")
    root.setLevel(logging.INFO)
    root.addHandler(file_h)
    root.addHandler(stderr_h)
    root.propagate = False

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the `gre_app` namespace."""
    _ensure_configured()
    return logging.getLogger(f"gre_app.{name}")
