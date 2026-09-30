"""ClickHouse's one reading of a column type into SQL text, for every statement that renders a
value: SPEC 2.2.4's domain rendering and SPEC 2.2.14's canonical sketch bytes.
"""

from __future__ import annotations

import re
from typing import assert_never

from dbprint.spec.classification import base_type
from dbprint.spec.sketch import SketchKind
from ..base import TemporalShape
from ..sql_layout import call


_SHAPES: dict[str, TemporalShape] = {
    "date": "date",
    "date32": "date",
    "datetime": "timestamp_tz",
    "datetime64": "timestamp_tz",
    "time": "time",
    "time64": "time",
}
_PRECISION_RE = re.compile(r"\(\s*(\d+)")
_DECIMAL_SCALE_RE = re.compile(r"decimal\(\s*\d+\s*,\s*(\d+)\s*\)", re.IGNORECASE)


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

    return f"toString({expr})"


def temporal_shape(sql_type: str) -> TemporalShape | None:
    """What a value of `sql_type` is on ClickHouse, or None for a non-temporal type."""

    return _SHAPES.get(base_type(sql_type))


def stores_below_microsecond(sql_type: str) -> bool:
    """Whether a `DateTime64(p)` keeps digits the microsecond rendering drops (SPEC 2.2.4)."""

    match = _PRECISION_RE.search(sql_type)

    return base_type(sql_type) == "datetime64" and match is not None and int(match[1]) > 6


def render_domain(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4: `T` separator, no trailing `.000000`."""

    shape = temporal_shape(sql_type)

    match shape:
        case "date" | "year":
            return f"toString({expr})"
        case "timestamp" | "timestamp_tz":
            return _stripped(
                call("formatDateTime", call("toDateTime64", expr, "6"), "'%Y-%m-%dT%H:%i:%S.%f'"),
            )
        case "time" | "time_tz":
            return _clock(expr)
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def render_canonical(expr: str, sql_type: str, kind: SketchKind) -> str:
    """SPEC 2.2.14's canonical byte form for one value of `sql_type`, as a SQL expression -
    `toString` drops a decimal's trailing zeros, so the declared scale is rendered explicitly.
    """

    if kind == "boolean":
        return f"(CASE WHEN {expr} THEN 'true' ELSE 'false' END)"

    if kind == "decimal" and (scale := _DECIMAL_SCALE_RE.search(sql_type)):
        return f"toDecimalString({expr}, {int(scale[1])})"

    if kind != "temporal":
        return f"toString({expr})"

    shape = temporal_shape(sql_type)

    match shape:
        case "date" | "year":
            return f"toString({expr})"
        case "timestamp" | "timestamp_tz":
            utc = call("toTimeZone", call("toDateTime64", expr, "6"), "'UTC'")

            rendered = _stripped(f"formatDateTime({utc}, '%Y-%m-%dT%H:%i:%S.%f')")

            return f"concat({rendered}, 'Z')"
        case "time" | "time_tz":
            return _clock(expr)
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def _clock(expr: str) -> str:
    return _stripped(
        call("formatDateTime", call("toDateTime64", expr, "6", "'UTC'"), "'%H:%i:%S.%f'"),
    )


def _stripped(rendered: str) -> str:
    return f"replaceRegexpOne({rendered}, '\\\\.000000$', '')"
