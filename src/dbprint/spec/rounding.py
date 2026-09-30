"""SPEC 2.2.6 numerical precision.

One definition for every adapter and the conformance validator, so they cannot round apart.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

from . import value_text


DECIMAL_PLACES = 6

# Without this floor a column of measurements below half of one decimal publishes as zeros.
FLOOR_SIGNIFICANT_FIGURES = 6

_SPELLED_TYPES = (
    datetime.datetime,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    uuid.UUID,
)


class UnrepresentableValue(TypeError):
    """A value the artifact has no spelling for - a producer defect, never a database fault."""

    def __init__(
        self,
        value: Any,
        *,
        table: str | None = None,
        column: str | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(value)
        kind = type(value)
        self.type_name = (
            kind.__qualname__
            if kind.__module__ == "builtins"
            else f"{kind.__module__}.{kind.__qualname__}"
        )
        self.table = table
        self.column = column
        self.field = field

    def __str__(self) -> str:
        where = [f"table {self.table!r}"] if self.table else []
        where += [f"column {self.column!r}"] if self.column else []
        prefix = f"{', '.join(where)}: " if where else ""

        return (
            f"{prefix}{self.field or 'a value'} is {self.type_name}, which has no artifact "
            "spelling (producer defect; please report)"
        )


def measured_value(
    value: Any,
    field: str | None = None,
) -> None | bool | int | float | Decimal | str:
    """One published cell as SPEC 2.2.4/2.2.6 allow it: a scalar, an exact number exactly.

    Dispatches on the exact type, so a driver's subclass is refused like any unknown type.
    """

    kind = type(value)

    if value is None or kind in (bool, int, float, str):
        return value

    if kind is Decimal:
        if not value.is_finite():
            return float(value)

        return int(value) if value == value.to_integral_value() else _stripped(value)

    if kind in _SPELLED_TYPES:
        return value_text.scalar_text(value)

    raise UnrepresentableValue(value, field=field)


def measured_text(value: Any, field: str) -> str:
    """A temporal bound or bucket key as published, which SPEC 2.2.16 and 2.2.4 spell as text."""

    measured = measured_value(value, field)

    if not isinstance(measured, str):
        raise UnrepresentableValue(value, field=field)

    return measured


def number_text(value: Decimal | float) -> str:
    """The positional spelling the artifact writes for a non-integer number - no exponent, and
    always a decimal point.
    """

    exact = value if isinstance(value, Decimal) else Decimal(repr(value))
    text = f"{_stripped(exact):f}" if isinstance(value, Decimal) else f"{exact:f}"

    return text if "." in text else f"{text}.0"


def round_statistic(value: Any, *, exact_int: bool = False) -> Any:
    """`value` as SPEC 2.2.6 emits it: six decimals, floored at six significant figures.

    `exact_int` is for count-like fields only; a finite `Decimal` rounds in decimal arithmetic.
    """

    if value is None:
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, Decimal) and value.is_finite():
        if exact_int and value == value.to_integral_value():
            return int(value)

        return _round_decimal(value)

    try:
        number = float(value)
    except (TypeError, ValueError):
        return value

    rounded = round(number, DECIMAL_PLACES)

    if rounded == 0.0 and number != 0.0:
        return float(f"{number:.{FLOOR_SIGNIFICANT_FIGURES}g}")

    return rounded


def is_rounded(value: float) -> bool:
    """Whether `value` is already what `round_statistic` would have emitted for it."""

    return round_statistic(value) == value


def _round_decimal(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(context.prec, len(value.as_tuple().digits) + DECIMAL_PLACES)
        rounded = value.quantize(Decimal(1).scaleb(-DECIMAL_PLACES), rounding=ROUND_HALF_EVEN)

        if rounded == 0 and value != 0:
            exponent = value.adjusted() - FLOOR_SIGNIFICANT_FIGURES + 1
            rounded = value.quantize(Decimal(1).scaleb(exponent), rounding=ROUND_HALF_EVEN)

    return rounded


def _stripped(value: Decimal) -> Decimal:
    """`value` without trailing zeros, at any width - `normalize()` rounds to context precision."""

    sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)

    while exponent < 0 and len(digits) > 1 and digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1

    return Decimal((sign, digits, exponent))
