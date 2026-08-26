"""Value redaction per SPEC v1, section 2.2.9: substitutes cell-level literals, coarsens the rest.

Privacy here is cell-level: null counts, cardinality, ratios, coverage and distribution stay
untouched. `freshness.max_age_days` and `range.span_days` are derived arithmetic against a
published constant, so they invert back to a withheld bound unless coarsened alongside the
literals. Pure: no I/O; the caller resolves which primitive applies.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal


Primitive = Literal["mask", "drop", "hash"]

# Fixed, not per-value: varying the placeholder would leak the shape `mask` hides.
MASK_PLACEHOLDER = "[redacted]"

_HASH_LENGTH = 16

# 90 is the format's own `dormant` boundary (SPEC 2.2.4), so the coarsened integer
# discloses nothing the freshness bucket does not already carry.
REDACTED_DAY_COUNT_GRANULARITY = 90


def coarsen_day_count(days: int) -> int:
    """Floor a derived day count to `REDACTED_DAY_COUNT_GRANULARITY`.

    Producer and validator both call this: SPEC 2.2.9's marker requires identical arithmetic.
    """

    return (days // REDACTED_DAY_COUNT_GRANULARITY) * REDACTED_DAY_COUNT_GRANULARITY


def redact_value(value: Any, primitive: Primitive, salt: str) -> Any:
    """Return the emitted stand-in for one literal.

    `drop` is the caller's to handle: it removes the field rather than substituting a value.
    """

    if primitive == "hash":
        return _digest(value, salt)

    return MASK_PLACEHOLDER


def _digest(value: Any, salt: str) -> str:
    """Salted digest, stable across runs for a stable salt.

    Salt comes from `bind_redaction_salt` (config/project.py); an unsalted hash provides no
    privacy. Truncated to 64 bits, still far beyond what one column's values need.
    """

    material = f"{salt}\x00{value}".encode()

    return hashlib.blake2b(material, digest_size=_HASH_LENGTH // 2).hexdigest()
