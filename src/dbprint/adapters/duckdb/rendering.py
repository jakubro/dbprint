"""duckdb's one reading of a column type into SQL text, for every statement that renders a value:
SPEC 2.2.4's domain rendering and SPEC 2.2.14's canonical sketch bytes.
"""

from __future__ import annotations

from functools import partial
from typing import assert_never

from dbprint.spec.classification import base_type, is_floating_type
from dbprint.spec.sketch import SketchKind
from ..base import TemporalShape, lookup_operand, lookup_temporal_shape


_SHAPES: dict[str, TemporalShape] = {
    "date": "date",
    "time": "time",
    "time_ns": "time",
    "time with time zone": "time_tz",
    "timestamp": "timestamp",
    "timestamp_s": "timestamp",
    "timestamp_ms": "timestamp",
    "timestamp_ns": "timestamp",
    "timestamp with time zone": "timestamp_tz",
    "timestamp_ltz": "timestamp_tz",
    "timestamp_tz": "timestamp_tz",
    "timestamptz_ns": "timestamp_tz",
}
# `STRFTIME` binds these to its TIMESTAMP_NS overload, which overflows past 2262-04-11.
_SUMMED_AS_BIGNUM = ("hugeint", "uhugeint", "int128", "uint128")
_COARSE_TIMESTAMP_TYPES = ("timestamp_s", "timestamp_ms")
_SUB_MICROSECOND_TYPES = ("timestamp_ns", "timestamptz_ns", "time_ns")

_CLOCK_PICTURE = "%H:%M:%S.%f"
_INSTANT_PICTURE = "%Y-%m-%dT%H:%M:%S.%f"
_NANO_CLOCK_PICTURE = "%H:%M:%S.%n"
_NANO_INSTANT_PICTURE = "%Y-%m-%dT%H:%M:%S.%n"


# Types with no operator a statistic needs, measured through a lossless comparable form.
_OPERANDS: dict[str, str] = {}


render_operand = partial(lookup_operand, _OPERANDS)


def finite_value(expr: str, sql_type: str) -> str:
    """`expr` where finite, NULL for NaN and the infinities; unchanged on a type holding neither."""

    if not is_floating_type(sql_type):
        return expr

    finite = f"ISFINITE({expr})"

    return f"CASE WHEN {finite} THEN {expr} END"


def sum_expression(expr: str, sql_type: str) -> str:
    """`SUM` over `expr` in the widest exact type duckdb offers for `sql_type`."""

    if base_type(sql_type) in _SUMMED_AS_BIGNUM:
        return f"SUM(CAST({expr} AS BIGNUM))"

    return f"SUM({expr})"


def mean_expression(expr: str, sql_type: str) -> str:
    """The mean of `expr`: `AVG` on a floating type, else the exact sum over the count."""

    if is_floating_type(sql_type):
        return f"AVG({expr})"

    return f"CAST({sum_expression(expr, sql_type)} AS DOUBLE) / NULLIF(COUNT({expr}), 0)"


def render_text(expr: str, sql_type: str) -> str:
    """`expr` as the engine's own text - the string a string-like column's `length` and
    `empty_count` are measured over, and the value it publishes (SPEC 2.2.4).
    """

    del sql_type

    return f"CAST({expr} AS VARCHAR)"


temporal_shape = partial(lookup_temporal_shape, _SHAPES)


def stores_below_microsecond(sql_type: str) -> bool:
    """Whether `sql_type` keeps digits the microsecond rendering drops (SPEC 2.2.4)."""

    return base_type(sql_type) in _SUB_MICROSECOND_TYPES


def published_instant(expr: str, sql_type: str) -> str:
    """`expr` floored to the microsecond; the date functions would round it through `TIMESTAMP`."""

    shape = temporal_shape(sql_type)

    if shape not in ("timestamp", "timestamp_tz") or not stores_below_microsecond(sql_type):
        return expr

    aware = expr if shape == "timestamp_tz" else f"TIMEZONE('UTC', {expr})"
    epoch = "CAST('1970-01-01 00:00:00+00' AS TIMESTAMPTZ_NS)"
    micros = f"DATEDIFF('microsecond', {epoch}, {aware})"

    # `DATEDIFF` is NULL for an infinity, which reaches `TIMESTAMP` only through its text.
    return (
        f"CASE WHEN {micros} IS NULL THEN CAST(CAST({aware} AS VARCHAR) AS TIMESTAMP) "
        f"ELSE MAKE_TIMESTAMP({micros}) END"
    )


def render_domain(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4 - `STRFTIME` reads a plain `TIMESTAMP`, a
    `TIMESTAMPTZ` argument otherwise pulling in an optional ICU dependency.
    """

    shape = temporal_shape(sql_type)
    fine = stores_below_microsecond(sql_type)

    match shape:
        case "date":
            return f"STRFTIME({expr}, '%Y-%m-%d')"
        case "timestamp" if fine:
            return _nanosecond(f"STRFTIME({expr}, '{_NANO_INSTANT_PICTURE}')")
        case "timestamp":
            coarse = base_type(sql_type) in _COARSE_TIMESTAMP_TYPES
            source = f"CAST(({expr}) AS TIMESTAMP)" if coarse else expr

            return _stripped(f"STRFTIME({source}, '{_INSTANT_PICTURE}')")
        case "timestamp_tz" if fine:
            utc = f"({expr}) AT TIME ZONE 'UTC'"

            return _zoned(_nanosecond(f"STRFTIME({utc}, '{_NANO_INSTANT_PICTURE}')"))
        case "timestamp_tz":
            source = f"CAST(({expr}) AT TIME ZONE 'UTC' AS TIMESTAMP)"

            return _zoned(_stripped(f"STRFTIME({source}, '{_INSTANT_PICTURE}')"))
        case "time" if fine:
            # STRFTIME has no TIME_NS overload: a time of day goes through its nanosecond count.
            source = f"MAKE_TIMESTAMP_NS(EPOCH_NS({expr}))"

            return _nanosecond(f"STRFTIME({source}, '{_NANO_CLOCK_PICTURE}')")
        case "time":
            return _clock(f"CAST(({expr}) AS TIME)")
        case "time_tz":
            return f"{_clock(f"CAST(TIMEZONE('UTC', {expr}) AS TIME)")} || 'Z'"
        case "year":
            return f"CAST({expr} AS VARCHAR)"
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def render_binary(expr: str) -> str:
    """A binary value as SPEC 2.2.4 spells it: lowercase hex, no prefix."""

    return f"LOWER(HEX({expr}))"


def render_canonical(expr: str, sql_type: str, kind: SketchKind) -> str:
    """SPEC 2.2.14's canonical byte form for one value of `sql_type`, as a SQL expression."""

    if kind == "binary":
        return expr

    if kind == "temporal":
        return render_domain(expr, sql_type)

    return f"CAST({expr} AS VARCHAR)"


def _clock(time_expr: str) -> str:
    return _stripped(f"STRFTIME(DATE '1970-01-01' + {time_expr}, '{_CLOCK_PICTURE}')")


def _nanosecond(rendered: str) -> str:
    # Nine digits shed their last three when those are zero, leaving the six a microsecond keeps.
    return _stripped(f"REGEXP_REPLACE({rendered}, '000$', '')")


def _stripped(rendered: str) -> str:
    return f"REGEXP_REPLACE({rendered}, '\\.000000$', '')"


def _zoned(rendered: str) -> str:
    # `STRFTIME` spells an infinity as the word, which takes no zone designator (SPEC 2.2.4).
    return f"REGEXP_REPLACE({rendered}, '(\\d)$', '\\1Z')"
