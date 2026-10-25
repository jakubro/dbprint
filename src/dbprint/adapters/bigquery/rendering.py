"""BigQuery's one reading of a column type into SQL text, for every statement that renders a value:
SPEC 2.2.4's domain rendering and SPEC 2.2.14's canonical sketch bytes.
"""

from __future__ import annotations

from functools import partial
from typing import assert_never

from dbprint.spec.classification import base_type, is_floating_type
from dbprint.spec.sketch import SketchKind
from ..base import TemporalShape, lookup_operand, lookup_temporal_shape


# `timestamp` is the only instant-based temporal type; datetime/date/time are naive.
_SHAPES: dict[str, TemporalShape] = {
    "date": "date",
    "time": "time",
    "datetime": "timestamp",
    "timestamp": "timestamp_tz",
}

_INSTANT_PICTURE = "%Y-%m-%dT%H:%M:%E6S"
_SUMMED_AS_BIGNUMERIC = (
    "int64",
    "int",
    "integer",
    "bigint",
    "smallint",
    "tinyint",
    "byteint",
    "numeric",
    "decimal",
)


# Types with no operator a statistic needs, measured through a lossless comparable form.
_OPERANDS: dict[str, str] = {"json": "TO_JSON_STRING({})", "object": "TO_JSON_STRING({})"}


render_operand = partial(lookup_operand, _OPERANDS)


def finite_value(expr: str, sql_type: str) -> str:
    """`expr` where finite, NULL for NaN and the infinities; unchanged on a type holding neither."""

    if not is_floating_type(sql_type):
        return expr

    finite = f"NOT IS_NAN({expr}) AND NOT IS_INF({expr})"

    return f"CASE WHEN {finite} THEN {expr} END"


def sum_expression(expr: str, sql_type: str) -> str:
    """`SUM` over `expr` as `BIGNUMERIC` where BigQuery would sum it in its own, narrower type."""

    if base_type(sql_type) in _SUMMED_AS_BIGNUMERIC:
        return f"SUM(CAST({expr} AS BIGNUMERIC))"

    return f"SUM({expr})"


def mean_expression(expr: str, sql_type: str) -> str:
    """The mean of `expr`: `AVG` on a floating type, else the exact sum over the count."""

    if is_floating_type(sql_type):
        return f"AVG({expr})"

    return f"CAST({sum_expression(expr, sql_type)} AS FLOAT64) / NULLIF(COUNT({expr}), 0)"


def render_text(expr: str, sql_type: str) -> str:
    """`expr` as the engine's own text - the string a string-like column's `length` and
    `empty_count` are measured over, and the value it publishes (SPEC 2.2.4).
    """

    del sql_type

    return f"CAST({expr} AS STRING)"


temporal_shape = partial(lookup_temporal_shape, _SHAPES)


def render_domain(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4 - used only where a STRING form must be computed
    in SQL; the statistics pass otherwise renders a fetched value in Python.
    """

    shape = temporal_shape(sql_type)

    match shape:
        case "date":
            return f"FORMAT_DATE('%Y-%m-%d', {expr})"
        case "time" | "time_tz":
            return _stripped(f"FORMAT_TIME('%H:%M:%E6S', {expr})")
        case "timestamp":
            return _stripped(f"FORMAT_DATETIME('{_INSTANT_PICTURE}', {expr})")
        case "timestamp_tz":
            rendered = _stripped(f"FORMAT_TIMESTAMP('{_INSTANT_PICTURE}', {expr}, 'UTC')")

            return f"CONCAT({rendered}, 'Z')"
        case "year":
            return f"CAST({expr} AS STRING)"
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def render_binary(expr: str) -> str:
    """A binary value as SPEC 2.2.4 spells it: lowercase hex, no prefix."""

    return f"TO_HEX({expr})"


def render_canonical(expr: str, sql_type: str, kind: SketchKind) -> str:
    """SPEC 2.2.14's canonical byte form for one value of `sql_type`, as a SQL expression."""

    if kind == "binary":
        return expr

    if kind == "temporal":
        return render_domain(expr, sql_type)

    return f"CAST({expr} AS STRING)"


def _stripped(rendered: str) -> str:
    return f"REGEXP_REPLACE({rendered}, r'\\.000000$', '')"
