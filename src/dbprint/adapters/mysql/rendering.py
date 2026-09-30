"""MySQL's one reading of a column type into SQL text, for every statement that renders a value:
SPEC 2.2.4's domain rendering and SPEC 2.2.14's canonical sketch bytes.
"""

from __future__ import annotations

from typing import assert_never

from dbprint.spec.classification import base_type
from dbprint.spec.sketch import SketchKind
from ..base import TemporalShape


# TIMESTAMP is stored UTC and converted to the session `time_zone` on read; DATETIME is naive.
_SHAPES: dict[str, TemporalShape] = {
    "date": "date",
    "time": "time",
    "datetime": "timestamp",
    "timestamp": "timestamp_tz",
    "year": "year",
}

_INSTANT_PICTURE = "%Y-%m-%dT%H:%i:%s.%f"


# Types with no operator a statistic needs, measured through a lossless comparable form.
_OPERANDS: dict[str, str] = {}


def render_operand(expr: str, sql_type: str) -> str:
    """`expr` as every comparing, grouping or aggregating statement reads it."""

    template = _OPERANDS.get(base_type(sql_type))

    return template.format(expr) if template else expr


def render_text(expr: str, sql_type: str) -> str:
    """`expr` as the engine's own text - the string a string-like column's `length` and
    `empty_count` are measured over, and the value it publishes (SPEC 2.2.4).
    """

    # A BIT's driver value is the integer a query compares with; its CHAR cast is raw bytes.
    return expr if base_type(sql_type) == "bit" else f"CAST({expr} AS CHAR)"


def temporal_shape(sql_type: str) -> TemporalShape | None:
    """What a value of `sql_type` is on MySQL, or None for a non-temporal type."""

    return _SHAPES.get(base_type(sql_type))


def render_domain(expr: str, sql_type: str, *, already_utc: bool = False) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4, which lets MySQL omit a UTC value's `Z`.

    `already_utc` skips a second, shifting conversion of an expression already converted.
    """

    shape = temporal_shape(sql_type)

    match shape:
        case "date":
            return f"DATE_FORMAT({expr}, '%Y-%m-%d')"
        case "timestamp":
            return _stripped(f"DATE_FORMAT({expr}, '{_INSTANT_PICTURE}')")
        case "timestamp_tz":
            source = expr if already_utc else f"CONVERT_TZ({expr}, @@session.time_zone, '+00:00')"

            return _stripped(f"DATE_FORMAT({source}, '{_INSTANT_PICTURE}')")
        case "time" | "time_tz":
            return _stripped(f"TIME_FORMAT({expr}, '%H:%i:%s.%f')")
        case "year":
            return f"CAST({expr} AS CHAR)"
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def render_canonical(expr: str, sql_type: str, kind: SketchKind) -> str:
    """SPEC 2.2.14's canonical byte form for one value of `sql_type`, as a SQL expression.

    A TIMESTAMP gains the `Z` its domain rendering may omit, so cross-adapter hashing agrees.
    """

    if kind == "boolean":
        return f"(CASE WHEN {expr} THEN 'true' ELSE 'false' END)"

    if kind != "temporal":
        return f"CAST({expr} AS CHAR)"

    rendered = render_domain(expr, sql_type)

    if temporal_shape(sql_type) != "timestamp_tz":
        return rendered

    return f"CONCAT({rendered}, 'Z')"


def _stripped(rendered: str) -> str:
    return f"REGEXP_REPLACE({rendered}, '\\\\.000000$', '')"
