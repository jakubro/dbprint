"""The published scalar text of a value, SPEC 2.2.4's order over it, and every number a reader sees.

`scalar_text` is the dumper's unquoted scalar; every sort and rendering reads it, so none drift.
"""

from __future__ import annotations

import datetime
import math
import uuid
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

from . import rounding


def scalar_text(value: Any) -> str:
    """The artifact's own spelling of `value`; TypeError for a type the artifact never holds."""

    if isinstance(value, str):
        return value
    elif isinstance(value, bool):
        return "true" if value else "false"
    elif isinstance(value, int):
        return str(value)
    elif isinstance(value, float):
        return rounding.number_text(value) if math.isfinite(value) else _non_finite(value)
    elif isinstance(value, Decimal):
        return rounding.number_text(value) if value.is_finite() else _non_finite(float(value))
    elif isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        text = value.isoformat()

        return text[:-6] + "Z" if text.endswith("+00:00") else text
    elif isinstance(value, datetime.timedelta):
        return _clock(value)
    elif isinstance(value, uuid.UUID):
        return str(value)
    else:
        raise TypeError(f"no artifact spelling for a {type(value).__qualname__} value")


def value_order_key(count: int, text: str) -> tuple[int, str]:
    """SPEC 2.2.4's order: count descending, ties by ascending code point of the published text."""

    return -count, text


def spell_number(value: float | Decimal) -> str:
    """A statistic or count as `statistics.yaml` writes it: digits, positional, never an exponent."""

    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise TypeError(f"not a number: {type(value).__qualname__}")

    return scalar_text(value)


def spell_percent(ratio: float | Decimal, *, signed: bool = False) -> str:
    """`ratio` as a percentage to one decimal, never rounding a partial share onto 0% or 100%.

    Half-even; a result landing on 0 or +-100 for an inexact ratio keeps decimals until it does not.
    """

    if isinstance(ratio, bool) or not isinstance(ratio, int | float | Decimal):
        raise TypeError(f"not a number: {type(ratio).__qualname__}")

    exact = Decimal(repr(ratio)) if isinstance(ratio, float) else Decimal(ratio)
    percent = exact * 100
    exponent = percent.as_tuple().exponent

    if not isinstance(exponent, int):
        return f"{scalar_text(ratio)}%"

    own_digits = max(-exponent, 1)
    places = 1

    with localcontext() as context:
        context.prec = max(context.prec, percent.adjusted() + own_digits + 2)
        shown = percent.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)

        while abs(shown) in (0, 100) and shown != percent and places < own_digits:
            places += 1
            shown = percent.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)

    text = f"{shown:f}".rstrip("0").rstrip(".")
    text = "0" if text == "-0" else text

    return f"+{text}%" if signed and shown > 0 else f"{text}%"


def _non_finite(value: float) -> str:
    if math.isnan(value):
        return ".nan"

    return ".inf" if value > 0 else "-.inf"


def _clock(value: datetime.timedelta) -> str:
    # Integer arithmetic: total_seconds() drops microseconds near MySQL TIME's +/-838:59:59.
    microseconds = (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds
    sign = "-" if microseconds < 0 else ""
    seconds, fraction = divmod(abs(microseconds), 1_000_000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    clock = f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"

    return f"{clock}.{fraction:06d}" if fraction else clock
