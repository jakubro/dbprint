"""The normalization key `normalized_cardinality` is measured with (SPEC 2.2.4).

One definition for the producer's spelling groups, the value lookup and the validator.
"""

from __future__ import annotations


def fold(text: str) -> str:
    """`LOWER(TRIM(...))` in Python: `lower` and spaces only, which is exactly what SQL's do."""

    return text.strip(" ").lower()
