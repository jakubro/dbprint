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
}
# `STRFTIME` binds these to its TIMESTAMP_NS overload, which overflows past 2262-04-11.
_SUMMED_AS_BIGNUM = ("hugeint", "uhugeint", "int128", "uint128")
_COARSE_TIMESTAMP_TYPES = ("timestamp_s", "timestamp_ms")
_SUB_MICROSECOND_TYPES = ("timestamp_ns", "time_ns")

_CLOCK_PICTURE = "%H:%M:%S.%f"
_INSTANT_PICTURE = "%Y-%m-%dT%H:%M:%S.%f"


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


def render_domain(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4 - `STRFTIME` reads a plain `TIMESTAMP`, a
    `TIMESTAMPTZ` argument otherwise pulling in an optional ICU dependency.
    """

    shape = temporal_shape(sql_type)

    match shape:
        case "date":
            return f"STRFTIME({expr}, '%Y-%m-%d')"
        case "timestamp":
            coarse = base_type(sql_type) in _COARSE_TIMESTAMP_TYPES
            source = f"CAST(({expr}) AS TIMESTAMP)" if coarse else expr

            return _stripped(f"STRFTIME({source}, '{_INSTANT_PICTURE}')")
        case "timestamp_tz":
            source = f"CAST(({expr}) AT TIME ZONE 'UTC' AS TIMESTAMP)"

            return f"{_stripped(f"STRFTIME({source}, '{_INSTANT_PICTURE}')")} || 'Z'"
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


def _stripped(rendered: str) -> str:
    return f"REGEXP_REPLACE({rendered}, '\\.000000$', '')"
