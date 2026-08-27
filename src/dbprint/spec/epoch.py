"""Epoch-integer detection per SPEC v1, section 4.5.

Two rules feed `inferred.epoch_unit`: `bounds_epoch_unit` over `range` bounds for `numeric`
columns (never sampled), `sample_epoch_unit` over the values SPEC 4.1.5 draws. Both windows
are a plausibility check, not proof (SPEC 4.5): an ordinary large integer can land inside
by chance. Pure: no I/O, no state.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Literal

from dbprint.spec.looks_like import MATCH_THRESHOLD


EpochUnit = Literal["seconds", "milliseconds"]

# The seconds window spans 2001-09-09 to 2033-05-18; the millisecond window is the same, x1000.
_SECONDS_WINDOW = (1_000_000_000, 2_000_000_000)
_MILLISECONDS_WINDOW = (1_000_000_000_000, 2_000_000_000_000)


def bounds_epoch_unit(minimum: object, maximum: object) -> EpochUnit | None:
    """Return the unit both bounds agree on, or None.

    Both `range.min` and `range.max` must be integral and inside the same window; the window
    floor is what rejects an id sequence or a byte count, with no sample needed.
    """

    lo = _as_integral(minimum)
    hi = _as_integral(maximum)

    if lo is None or hi is None:
        return None

    return _window_of(lo, hi)


def sample_epoch_unit(values: Iterable[object]) -> EpochUnit | None:
    """Return the unit at least `looks_like`'s `MATCH_THRESHOLD` of sampled values agree on."""

    sample = list(values)

    if not sample:
        return None

    integral = [v for v in (_as_integral(s) for s in sample) if v is not None]

    if _share(integral, _SECONDS_WINDOW, len(sample)) >= MATCH_THRESHOLD:
        return "seconds"

    if _share(integral, _MILLISECONDS_WINDOW, len(sample)) >= MATCH_THRESHOLD:
        return "milliseconds"

    return None


def _window_of(lo: int, hi: int) -> EpochUnit | None:
    if _SECONDS_WINDOW[0] <= lo and hi <= _SECONDS_WINDOW[1]:
        return "seconds"

    if _MILLISECONDS_WINDOW[0] <= lo and hi <= _MILLISECONDS_WINDOW[1]:
        return "milliseconds"

    return None


def _share(integral: list[int], window: tuple[int, int], denominator: int) -> float:
    """Fraction of the whole sample inside `window`; a non-integral value scores 0."""

    matches = sum(1 for v in integral if window[0] <= v <= window[1])

    return matches / denominator


def _as_integral(value: object) -> int | None:
    """The value as an `int`, or None when it is not a whole number.

    A `str` is parsed as a signed integer literal only (SPEC 4.1.1). `bool` is excluded
    despite being an `int` subclass: a boolean column never classifies `numeric` (SPEC 3.2).
    """

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, (float, Decimal)):
        return int(value) if value == int(value) else None

    if isinstance(value, str):
        s = value.removeprefix("-")

        # isdecimal(), not isdigit(): a superscript/subscript digit passes isdigit() and
        # raises ValueError out of int() - the guard must describe what int() accepts.
        return int(value) if s.isdecimal() else None

    return None
