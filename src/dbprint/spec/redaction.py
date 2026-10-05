"""Value redaction per SPEC v1, section 2.2.9: a redacted column publishes its count profile.

Each statistic has one role in `FIELD_ROLES`. Pure: the caller resolves which primitive applies.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

from .rounding import number_text


Primitive = Literal["mask", "drop", "hash"]

RedactionRole = Literal[
    "count_profile",
    "literal",
    "withheld",
    "coarsened",
    "detection",
    "structural",
    "shape",
]

# Every property of the statistics schema's `Column`, keyed to what a `redacted` marker does to it.
FIELD_ROLES: Mapping[str, RedactionRole] = MappingProxyType(
    {
        "null_count": "count_profile",
        "null_rate": "count_profile",
        "cardinality": "count_profile",
        "cardinality_ratio": "count_profile",
        "cardinality_method": "count_profile",
        "values_coverage": "count_profile",
        "values_coverage_method": "count_profile",
        "distribution": "count_profile",
        "frequencies": "count_profile",
        "populated": "count_profile",
        "values": "literal",
        "range": "literal",
        "percentiles": "literal",
        "mean": "withheld",
        "sum": "withheld",
        "length": "withheld",
        "zero_count": "withheld",
        "negative_count": "withheld",
        "empty_count": "withheld",
        "quantized_count": "withheld",
        "normalized_cardinality": "withheld",
        "unrepresentable": "withheld",
        "sketch": "withheld",
        "extent": "withheld",
        "dimension": "withheld",
        "norm": "withheld",
        "geometry": "shape",
        "parts_found": "count_profile",
        "size": "count_profile",
        "types": "count_profile",
        "occurrences": "count_profile",
        "parts": "structural",
        "freshness": "coarsened",
        "inferred": "detection",
        "sql_type": "structural",
        "nullable": "structural",
        "classification": "structural",
        "physical_name": "structural",
        "collation": "structural",
        "physical_layout_key": "structural",
        "rows_scanned": "structural",
        "unmeasured": "structural",
        "redacted": "structural",
    },
)

WITHHELD_UNDER_REDACTION = frozenset(f for f, role in FIELD_ROLES.items() if role == "withheld")

# What a diff must never compare across a marker: the literals and everything computed from them.
NOT_COMPARED_UNDER_REDACTION = frozenset(
    f for f, role in FIELD_ROLES.items() if role in {"literal", "withheld"}
)

# Fixed, not per-value: varying the placeholder would leak the shape `mask` hides.
MASK_PLACEHOLDER = "[redacted]"

_HASH_LENGTH = 16

# 90 is the format's own `dormant` boundary (SPEC 2.2.4), so the coarsened integer
# discloses nothing the freshness bucket does not already carry.
REDACTED_DAY_COUNT_GRANULARITY = 90


def is_redacted(column: Mapping[str, Any]) -> bool:
    """Whether a column carries any `redacted` marker (SPEC 2.2.9)."""

    return column.get("redacted") is not None


def apply_redaction_rule(column: dict[str, Any]) -> None:
    """Remove every field a marked column MUST NOT emit (SPEC 2.2.9); unmarked ones are left alone."""

    if is_redacted(column):
        for field in WITHHELD_UNDER_REDACTION:
            column.pop(field, None)


def coarsen_day_count(days: int) -> int:
    """Floor a derived day count to `REDACTED_DAY_COUNT_GRANULARITY`.

    Producer and validator both call this: SPEC 2.2.9's marker requires identical arithmetic.
    """

    return (days // REDACTED_DAY_COUNT_GRANULARITY) * REDACTED_DAY_COUNT_GRANULARITY


def redact_value(value: Any, primitive: Primitive, salt: str | None) -> Any:
    """Return the emitted stand-in for one literal.

    `drop` is the caller's to handle: it removes the field rather than substituting a value.
    """

    if primitive == "hash":
        return _digest(value, _salt_with_material(salt))

    return MASK_PLACEHOLDER


def _salt_with_material(salt: str | None) -> str:
    """The salt a digest may use; an unsalted digest is not readable as one from the artifact."""

    if not salt or not salt.strip():
        raise ValueError("hash redaction requires a redaction_salt carrying a value")

    return salt


def _digest(value: Any, salt: str) -> str:
    """Salted digest, stable across runs for a stable salt.

    Salt comes from `bind_redaction_salt` (config/project.py); an unsalted hash provides no
    privacy. Truncated to 64 bits, still far beyond what one column's values need.
    """

    spelled = number_text(value) if isinstance(value, Decimal) else value
    material = f"{salt}\x00{spelled}".encode()

    return hashlib.blake2b(material, digest_size=_HASH_LENGTH // 2).hexdigest()
