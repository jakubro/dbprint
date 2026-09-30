"""Snowflake's one reading of a column type into SQL text, for every statement that renders a
value: SPEC 2.2.4's domain rendering and SPEC 2.2.14's canonical sketch bytes.
"""

from __future__ import annotations

from typing import assert_never

from dbprint.spec.classification import base_type
from dbprint.spec.sketch import SketchKind
from ..base import TemporalShape


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


def render_operand(expr: str, sql_type: str) -> str:
    """`expr` as every comparing, grouping or aggregating statement reads it."""

    template = _OPERANDS.get(base_type(sql_type))

    return template.format(expr) if template else expr


def render_text(expr: str, sql_type: str) -> str:
    """`expr` as the engine's own text - the string a string-like column's `length` and
    `empty_count` are measured over, and the value it publishes (SPEC 2.2.4).
    """

    del sql_type

    return f"TO_VARCHAR({expr})"


def temporal_shape(sql_type: str) -> TemporalShape | None:
    """What a value of `sql_type` is on Snowflake, or None for a non-temporal type."""

    return _SHAPES.get(base_type(sql_type))


def render_domain(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4.

    An explicit `TO_VARCHAR` picture ignores the session output formats; no `infinity` or BC guard is needed.
    """

    shape = temporal_shape(sql_type)

    match shape:
        case "date":
            return f"TO_VARCHAR({expr}, 'YYYY-MM-DD')"
        case "timestamp":
            return _stripped(f"TO_VARCHAR({expr}, '{_INSTANT_PICTURE}')")
        case "timestamp_tz":
            utc = f"CONVERT_TIMEZONE('UTC', {expr})"

            return f"{_stripped(f"TO_VARCHAR({utc}, '{_INSTANT_PICTURE}')")} || 'Z'"
        case "time" | "time_tz":
            return _stripped(f"TO_VARCHAR({expr}, 'HH24:MI:SS.FF6')")
        case "year":
            return f"TO_VARCHAR({expr})"
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def render_canonical(expr: str, sql_type: str, kind: SketchKind) -> str:
    """SPEC 2.2.14's canonical byte form for one value of `sql_type`, as a SQL expression -
    `TO_VARCHAR`'s default rendering already matches for every non-temporal kind.
    """

    if kind == "temporal":
        return render_domain(expr, sql_type)

    return f"TO_VARCHAR({expr})"


def _stripped(rendered: str) -> str:
    return f"REGEXP_REPLACE({rendered}, '\\.000000$', '')"
