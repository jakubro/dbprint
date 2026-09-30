"""Postgres's one reading of a column type into SQL text, for every statement that renders a value:
SPEC 2.2.4's domain rendering and SPEC 2.2.14's canonical sketch bytes.
"""

from __future__ import annotations

from typing import assert_never

from dbprint.spec.classification import base_type
from dbprint.spec.sketch import SketchKind
from ..base import TemporalShape
from ..sql_layout import indented


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
_OPERANDS = {"money": "NUMERIC", "json": "TEXT"}


def render_operand(expr: str, sql_type: str) -> str:
    """`expr` as every comparing, grouping or aggregating statement reads it."""

    target = _OPERANDS.get(base_type(sql_type))

    return f"CAST({expr} AS {target})" if target else expr


def render_text(expr: str, sql_type: str) -> str:
    """`expr` as the engine's own text - the string a string-like column's `length` and
    `empty_count` are measured over, and the value it publishes (SPEC 2.2.4).
    """

    del sql_type

    return f"CAST({expr} AS TEXT)"


def temporal_shape(sql_type: str) -> TemporalShape | None:
    """What a value of `sql_type` is on Postgres, or None for a non-temporal type."""

    return _SHAPES.get(base_type(sql_type))


def render_domain(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4.

    `AT TIME ZONE 'UTC'` ignores the session TimeZone; `infinity` and the BC era sit outside the picture.
    """

    shape = temporal_shape(sql_type)

    match shape:
        case "date":
            return _calendar(expr, "DATE", f"TO_CHAR({expr}, 'YYYY-MM-DD')")
        case "timestamp":
            return _calendar(expr, "TIMESTAMP", _stripped(f"TO_CHAR({expr}, '{_INSTANT_PICTURE}')"))
        case "timestamp_tz":
            utc = f"{expr} AT TIME ZONE 'UTC'"
            body = _stripped(f"TO_CHAR({utc}, '{_INSTANT_PICTURE}')")

            return _calendar(expr, "TIMESTAMPTZ", f"{body} || 'Z'")
        case "time":
            return _stripped(f"TO_CHAR({expr}::TIME, '{_CLOCK_PICTURE}')")
        case "time_tz":
            utc = f"({expr} AT TIME ZONE 'UTC')::TIME"

            return f"{_stripped(f"TO_CHAR({utc}, '{_CLOCK_PICTURE}')")} || 'Z'"
        case "year":
            return f"{expr}::TEXT"
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def render_canonical(expr: str, sql_type: str, kind: SketchKind) -> str:
    """SPEC 2.2.14's canonical byte form for one value of `sql_type`, as a SQL expression -
    `::TEXT` already matches for every non-temporal kind.
    """

    if kind == "temporal":
        return render_domain(expr, sql_type)

    return f"{expr}::TEXT"


def _calendar(expr: str, cast_type: str, body: str) -> str:
    era = f"CASE WHEN TO_CHAR({expr}, 'BC') = 'BC' THEN ' BC' ELSE '' END"

    return f"""
        CASE
          WHEN {expr} = 'infinity'::{cast_type} THEN 'infinity'
          WHEN {expr} = '-infinity'::{cast_type} THEN '-infinity'
          ELSE
            {indented(body, 12)}
            || {era}
        END
        """


def _stripped(rendered: str) -> str:
    return f"REGEXP_REPLACE({rendered}, '\\.000000$', '')"
