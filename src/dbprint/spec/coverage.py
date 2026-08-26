"""Shared `values_coverage` arithmetic per SPEC 2.2.4.

One definition for both adapters and the conformance validator, so they cannot round apart.
"""

from __future__ import annotations


# Clamps a truncated list below 1.0 so a validator's tolerance can never read it as exhaustive.
TRUNCATED_CLAMP = 0.999999


def coverage_share(listed: int, non_null: int, *, exhaustive: bool) -> float:
    """Share of the non-null rows the emitted value list covers, bounded to [0, 1].

    `exhaustive` short-circuits before the division, so a complete list reads 1.0 even when
    phase A's row count and phase B's grouped scan disagree on `non_null`. A zero `non_null`
    reads 1.0 too, and the bound holds even where listed exceeds non_null.
    """

    if not non_null or exhaustive:
        return 1.0

    rounded = round(listed / non_null, 6)

    if rounded >= 1.0:
        return TRUNCATED_CLAMP

    return rounded


def is_incoherent(listed: int, non_null: int) -> bool:
    """True when the listed counts exceed the non-null rows they are drawn from.

    A numerator/denominator mismatch across reads - callers warn rather than trust the share.
    """

    return non_null > 0 and listed > non_null
