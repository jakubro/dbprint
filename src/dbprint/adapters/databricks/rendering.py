"""Databricks's one reading of a column type into SQL text, for every statement that renders a
value: SPEC 2.2.4's domain rendering and SPEC 2.2.14's canonical sketch bytes.
"""

from __future__ import annotations

from functools import partial
from typing import assert_never

from dbprint.spec.sketch import SketchKind
from ..base import TemporalShape, lookup_operand, lookup_temporal_shape
from ..sql_layout import call


# `timestamp` is session-zone-converted on read; `timestamp_ntz` carries no zone.
_SHAPES: dict[str, TemporalShape] = {
    "date": "date",
    "timestamp": "timestamp_tz",
    "timestamp_ntz": "timestamp",
}


# Types with no operator a statistic needs, measured through a lossless comparable form.
_OPERANDS: dict[str, str] = {"variant": "CAST({} AS STRING)", "object": "CAST({} AS STRING)"}


render_operand = partial(lookup_operand, _OPERANDS)


def render_text(expr: str, sql_type: str) -> str:
    """`expr` as the engine's own text - the string a string-like column's `length` and
    `empty_count` are measured over, and the value it publishes (SPEC 2.2.4).
    """

    del sql_type

    return f"CAST({expr} AS STRING)"


temporal_shape = partial(lookup_temporal_shape, _SHAPES)


def utc_instant(expr: str, sql_type: str) -> str:
    """`expr` as its naive UTC instant when `sql_type` is zone-aware, else unchanged.

    A bare cast to `TIMESTAMP_NTZ` keeps the session-zone wall clock, so this converts first.
    """

    if temporal_shape(sql_type) != "timestamp_tz":
        return expr

    return f"CAST({call('CONVERT_TIMEZONE', 'CURRENT_TIMEZONE()', "'UTC'", expr)} AS TIMESTAMP_NTZ)"


def render_domain(expr: str, sql_type: str, *, already_utc: bool = False) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4.

    `already_utc` marks an expression a subquery converted; a second conversion would shift it.
    """

    shape = temporal_shape(sql_type)

    match shape:
        case "date":
            return call("DATE_FORMAT", expr, "'yyyy-MM-dd'")
        case "timestamp":
            return _instant(expr)
        case "timestamp_tz":
            source = expr if already_utc else utc_instant(expr, sql_type)

            return f"CONCAT({_instant(source)}, 'Z')"
        case "time" | "time_tz" | "year":
            return f"CAST({expr} AS STRING)"
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

    return f"CAST({expr} AS STRING)"


def _instant(expr: str) -> str:
    body = f"DATE_FORMAT({expr}, \"yyyy-MM-dd'T'HH:mm:ss.SSSSSS\")"

    return f"REGEXP_REPLACE({body}, '\\\\.000000$', '')"
