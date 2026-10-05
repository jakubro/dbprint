"""Redshift's one reading of a column type into SQL text, for every statement that renders a value:
SPEC 2.2.4's domain rendering and SPEC 2.2.14's canonical sketch bytes.
"""

from __future__ import annotations

from functools import partial
from typing import assert_never

from dbprint.spec.sketch import SketchKind
from ..base import TemporalShape, lookup_operand, lookup_temporal_shape


_SHAPES: dict[str, TemporalShape] = {
    "date": "date",
    "time": "time",
    "time without time zone": "time",
    "time with time zone": "time_tz",
    "timetz": "time_tz",
    "timestamp": "timestamp",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamp_tz",
    "timestamptz": "timestamp_tz",
}

_CLOCK_PICTURE = "HH24:MI:SS.US"
_INSTANT_PICTURE = 'YYYY-MM-DD"T"HH24:MI:SS.US'


# Types with no operator a statistic needs, measured through a lossless comparable form.
_OPERANDS: dict[str, str] = {}


render_operand = partial(lookup_operand, _OPERANDS)


def render_text(expr: str, sql_type: str) -> str:
    """`expr` as the engine's own text - the string a string-like column's `length` and
    `empty_count` are measured over, and the value it publishes (SPEC 2.2.4).
    """

    del sql_type

    return f"{expr}::VARCHAR"


temporal_shape = partial(lookup_temporal_shape, _SHAPES)


def render_domain(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4 - a tz-aware type normalizes to UTC first, and
    `TO_CHAR`'s always-six-digit `US` field is stripped.
    """

    shape = temporal_shape(sql_type)

    # NULL propagates through `||` on its own; no CASE guard is needed to keep a NULL bound NULL.
    match shape:
        case "date":
            return f"TO_CHAR({expr}, 'YYYY-MM-DD')"
        case "timestamp":
            return _stripped(f"TO_CHAR({expr}, '{_INSTANT_PICTURE}')")
        case "timestamp_tz":
            rendered = _stripped(f"TO_CHAR(({expr} AT TIME ZONE 'UTC'), '{_INSTANT_PICTURE}')")

            return f"({rendered} || 'Z')"
        case "time":
            # A time picture on a TIME: TO_CHAR would otherwise render today's date, not raise.
            return _stripped(f"TO_CHAR({expr}, '{_CLOCK_PICTURE}')")
        case "time_tz":
            utc = f"(({expr} AT TIME ZONE 'UTC')::TIME)"

            rendered = _stripped(f"TO_CHAR({utc}, '{_CLOCK_PICTURE}')")

            return f"({rendered} || 'Z')"
        case "year":
            return f"{expr}::VARCHAR"
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def render_binary(expr: str) -> str:
    """A binary value as SPEC 2.2.4 spells it: lowercase hex, no prefix."""

    return f"LOWER(TO_HEX({expr}))"


def render_canonical(expr: str, sql_type: str, kind: SketchKind) -> str:
    """SPEC 2.2.14's canonical byte form for one value of `sql_type`, as a SQL expression -
    `::VARCHAR` already matches for every non-temporal kind.
    """

    if kind == "binary":
        return expr

    if kind == "temporal":
        return render_domain(expr, sql_type)

    return f"{expr}::VARCHAR"


def _stripped(rendered: str) -> str:
    return f"REGEXP_REPLACE({rendered}, '\\.000000$', '')"
