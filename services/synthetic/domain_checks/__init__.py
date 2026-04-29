"""
Type-specific lints — stage (f).

A registry of `subtype -> [checker]` callables. Each checker takes a
DraftItem-shaped payload (dict, not the dataclass — easier in tests)
and returns a `(passed: bool, reason: str)` tuple. All must pass for
the item to survive.

Checkers are intentionally cheap and local (no LLM calls). Anything
that needs an LLM judgement belongs in stage (e).
"""
from services.synthetic.domain_checks.registry import (
    DEFAULT_REGISTRY,
    DomainCheckRegistry,
    run_checks,
)

__all__ = ["DEFAULT_REGISTRY", "DomainCheckRegistry", "run_checks"]
