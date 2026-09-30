"""BigQuery's one reading of a column type into SQL text, for every statement that renders a value:
SPEC 2.2.4's domain rendering and SPEC 2.2.14's canonical sketch bytes.
"""

from __future__ import annotations

from typing import assert_never

from dbprint.spec.classification import base_type
from dbprint.spec.sketch import SketchKind
from ..base import TemporalShape


# `timestamp` is the only instant-based temporal type; datetime/date/time are naive.
_SHAPES: dict[str, TemporalShape] = {
    "date": "date",
    "time": "time",
    "datetime": "timestamp",
    "timestamp": "timestamp_tz",
}

_INSTANT_PICTURE = "%Y-%m-%dT%H:%M:%E6S"


# Types with no operator a statistic needs, measured through a lossless comparable form.
_OPERANDS: dict[str, str] = {"json": "TO_JSON_STRING({})"}


def render_operand(expr: str, sql_type: str) -> str:
    """`expr` as every comparing, grouping or aggregating statement reads it."""

    template = _OPERANDS.get(base_type(sql_type))

    return template.format(expr) if template else expr


def render_text(expr: str, sql_type: str) -> str:
    """`expr` as the engine's own text - the string a string-like column's `length` and
    `empty_count` are measured over, and the value it publishes (SPEC 2.2.4).
    """

    del sql_type

    return f"CAST({expr} AS STRING)"


def temporal_shape(sql_type: str) -> TemporalShape | None:
    """What a value of `sql_type` is on BigQuery, or None for a non-temporal type."""

    return _SHAPES.get(base_type(sql_type))


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
            rendered = _stripped(f"FORMAT_TIMESTAMP('{_INSTANT_PICTURE}', {expr})")

            return f"CONCAT({rendered}, 'Z')"
        case "year":
            return f"CAST({expr} AS STRING)"
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def render_canonical(expr: str, sql_type: str, kind: SketchKind) -> str:
    """SPEC 2.2.14's canonical byte form for one value of `sql_type`, as a SQL expression."""

    if kind == "temporal":
        return render_domain(expr, sql_type)

    return f"CAST({expr} AS STRING)"


def _stripped(rendered: str) -> str:
    return f"REGEXP_REPLACE({rendered}, r'\\.000000$', '')"
