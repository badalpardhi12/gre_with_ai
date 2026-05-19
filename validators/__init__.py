"""Structural-integrity validators for live GRE content.

Each module exposes a ``validate(...)`` entry point that returns a list of
:class:`ValidationFinding` instances. Empty list means valid. Validators are
read-only — they REPORT problems, never mutate the DB.

Driver: ``scripts/run_validators.py`` walks live questions and AWA prompts,
dispatches to the right validator by measure, and writes per-rule CSV +
JSON summary into ``data/audits/``.

See ``docs/implementation_plan_2026_05_18.md`` §3.1 for the spec.
"""
from validators.findings import ValidationFinding
from validators.quant import validate as validate_quant
from validators.verbal import validate as validate_verbal
from validators.awa import validate as validate_awa

__all__ = [
    "ValidationFinding",
    "validate_quant",
    "validate_verbal",
    "validate_awa",
]
