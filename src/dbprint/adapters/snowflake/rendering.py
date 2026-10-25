"""Snowflake's one reading of a column type into SQL text, for every statement that renders a
value: SPEC 2.2.4's domain rendering and SPEC 2.2.14's canonical sketch bytes.
"""

from __future__ import annotations

from functools import partial
from typing import assert_never

from dbprint.spec.classification import is_floating_type
from dbprint.spec.sketch import SketchKind
from ..base import TemporalShape, lookup_operand, lookup_temporal_shape


# TZ variants convert through CONVERT_TIMEZONE before rendering, immune to the session
# TIMEZONE param unlike TO_VARCHAR's offset token; LTZ and TZ both carry an instant.
_SHAPES: dict[str, TemporalShape] = {
    "date": "date",
    "time": "time",
    "timestamp": "timestamp",
    "timestamp_ntz": "timestamp",
    "datetime": "timestamp",
    "timestamp with time zone": "timestamp_tz",
    "timestamp_ltz": "timestamp_tz",
    "timestamp_tz": "timestamp_tz",
}

_INSTANT_PICTURE = 'YYYY-MM-DD"T"HH24:MI:SS.FF6'


# Types with no operator a statistic needs, measured through a lossless comparable form.
_OPERANDS: dict[str, str] = {}


render_operand = partial(lookup_operand, _OPERANDS)


def finite_value(expr: str, sql_type: str) -> str:
    """`expr` where finite, NULL for NaN and the infinities; unchanged on a type holding neither."""

    if not is_floating_type(sql_type):
        return expr

    finite = f"{expr} NOT IN ('NaN'::FLOAT, 'inf'::FLOAT, '-inf'::FLOAT)"

    return f"CASE WHEN {finite} THEN {expr} END"


def sum_expression(expr: str, sql_type: str) -> str:
    """`SUM` over `expr`; Snowflake already sums into an equivalent or larger type."""

    del sql_type

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

    return f"TO_VARCHAR({expr})"


temporal_shape = partial(lookup_temporal_shape, _SHAPES)


def render_domain(expr: str, sql_type: str, *, already_utc: bool = False) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4, an explicit picture ignoring session formats.

    `already_utc` marks a zone-aware expression already converted to its naive UTC reading.
    """

    shape = temporal_shape(sql_type)

    match shape:
        case "date":
            return f"TO_VARCHAR({expr}, 'YYYY-MM-DD')"
        case "timestamp":
            return _stripped(f"TO_VARCHAR({expr}, '{_INSTANT_PICTURE}')")
        case "timestamp_tz":
            utc = expr if already_utc else f"CONVERT_TIMEZONE('UTC', {expr})"

            return f"{_stripped(f"TO_VARCHAR({utc}, '{_INSTANT_PICTURE}')")} || 'Z'"
        case "time" | "time_tz":
            return _stripped(f"TO_VARCHAR({expr}, 'HH24:MI:SS.FF6')")
        case "year":
            return f"TO_VARCHAR({expr})"
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def render_binary(expr: str) -> str:
    """A binary value as SPEC 2.2.4 spells it: lowercase hex, no prefix."""

    return f"HEX_ENCODE({expr}, 0)"


def render_canonical(expr: str, sql_type: str, kind: SketchKind) -> str:
    """SPEC 2.2.14's canonical byte form for one value of `sql_type`, as a SQL expression -
    `TO_VARCHAR`'s default rendering already matches for every non-temporal kind.
    """

    if kind == "binary":
        return expr

    if kind == "temporal":
        return render_domain(expr, sql_type)

    return f"TO_VARCHAR({expr})"


def _stripped(rendered: str) -> str:
    return f"REGEXP_REPLACE({rendered}, '\\.000000$', '')"


def utc_reading(expr: str, sql_type: str) -> str:
    """A `TIMESTAMP_LTZ`/`TIMESTAMP_TZ` value as its naive UTC wall clock; other types unchanged."""

    if temporal_shape(sql_type) != "timestamp_tz":
        return expr

    return f"CONVERT_TIMEZONE('UTC', {expr})::TIMESTAMP_NTZ"
