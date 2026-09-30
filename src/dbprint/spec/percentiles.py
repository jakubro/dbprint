"""SPEC 2.2.4 percentile coherence across the float64 grid, shared by adapters and the validator.

Past 2**53 a disagreement of a few grid steps is an artifact; anything wider is a measurement.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any


FLOAT64_EXACT_LIMIT = 2**53

# How many float64 steps apart a percentile and its bound or neighbour may sit and still count as
# one value on two grids - the producer repairs within it, the validator tolerates within it.
REPRESENTABLE_STEPS = 4


def beyond_float64_precision(value: Any) -> bool:
    """Whether `value` is a finite real number past the float64 exact-integer range."""

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False

    return math.isfinite(value) and abs(value) > FLOAT64_EXACT_LIMIT


def coherent_percentiles(percentiles: Mapping[str, Any], low: Any, high: Any) -> dict[str, Any]:
    """Clamp float percentiles into their bounds and flatten descents, beyond float64 only.

    Each fix needs its operands past 2**53 and a gap within `REPRESENTABLE_STEPS`; wider is kept.
    """

    out = dict(percentiles)
    previous: Any = None

    for key in sorted(out, key=lambda name: int(name[1:])):
        value = out[key]

        if isinstance(value, float):
            if beyond_float64_precision(low) and value < low and within_steps(value, low):
                value = _nearest_inside(low, low, high, -math.inf)

            if beyond_float64_precision(high) and value > high and within_steps(value, high):
                value = _nearest_inside(high, low, high, math.inf)

            if (
                _is_real(previous)
                and value < previous
                and beyond_float64_precision(previous)
                and beyond_float64_precision(value)
                and within_steps(value, previous)
            ):
                value = float(previous)

            out[key] = value

        previous = out[key]

    return out


def within_steps(a: Any, b: Any) -> bool:
    """Whether `a` and `b` lie within `REPRESENTABLE_STEPS` float64 steps at their magnitude."""

    return abs(Decimal(a) - Decimal(b)) <= Decimal(tolerance(a, b))


def tolerance(a: Any, b: Any) -> float:
    """The gap two values `REPRESENTABLE_STEPS` float64 steps apart span at their magnitude."""

    return REPRESENTABLE_STEPS * math.ulp(max(abs(float(a)), abs(float(b))))


def _nearest_inside(bound: Any, low: Any, high: Any, outward: float) -> float:
    # `float(bound)` can round past the bound itself; one step back lands inside unless no float
    # lies between the bounds at all, where the rounded bound is what a float reader compares.
    candidate = float(bound)

    if low <= candidate <= high:
        return candidate

    stepped = math.nextafter(candidate, -outward)

    return stepped if low <= stepped <= high else candidate


def _is_real(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
