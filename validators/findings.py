"""Shared dataclass for validator output.

A ``ValidationFinding`` is one rule violation against one item. Validators
return ``List[ValidationFinding]`` — empty means valid. The driver flattens
findings across all live items into a single CSV row per finding.
"""
from dataclasses import dataclass, field
from typing import Any, Dict


# severity is a string (not an Enum) so the dataclass stays trivially
# JSON-serializable and the value can be filtered with simple equality
# checks at the SQL/CSV layer. Validators must use one of:
#   - "error":   structural violation that breaks rendering or scoring
#                (e.g. numeric_entry with no answer; QC with non-canonical
#                 options). Items are unfit for live serving.
#   - "warning": deviation worth a human eyeball but not necessarily fatal
#                (e.g. AWA prompt missing the "discuss" keyword in an
#                 alternate-phrasing variant).
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
ALLOWED_SEVERITIES = (SEVERITY_ERROR, SEVERITY_WARNING)


@dataclass(frozen=True)
class ValidationFinding:
    """One validator violation."""

    rule_id: str            # e.g. "QUANT_QC_NON_CANONICAL_OPTIONS"
    severity: str           # "error" | "warning"
    message: str            # human-readable one-liner
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.severity not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"severity must be one of {ALLOWED_SEVERITIES}, got "
                f"{self.severity!r}"
            )
