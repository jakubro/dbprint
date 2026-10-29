"""ClickHouse's one reading of a column type into SQL text, for every statement that renders a
value: SPEC 2.2.4's domain rendering and SPEC 2.2.14's canonical sketch bytes.
"""

from __future__ import annotations

import re
from functools import partial
from typing import assert_never

from dbprint.spec.classification import base_type, is_floating_type
from dbprint.spec.sketch import SketchKind
from ..base import TemporalShape, lookup_operand, lookup_temporal_shape
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
_ZONE_RE = re.compile(r"'([^']+)'")
_DECIMAL_SCALE_RE = re.compile(r"decimal\(\s*\d+\s*,\s*(\d+)\s*\)", re.IGNORECASE)
_DECIMAL_RE = re.compile(r"decimal\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.IGNORECASE)


# Types with no operator a statistic needs, measured through a lossless comparable form.
_OPERANDS: dict[str, str] = {}


render_operand = partial(lookup_operand, _OPERANDS)


def finite_value(expr: str, sql_type: str) -> str:
    """`expr` where finite, NULL for NaN and the infinities; unchanged on a type holding neither."""

    if not is_floating_type(sql_type):
        return expr

    finite = f"isFinite({expr})"

    return f"CASE WHEN {finite} THEN {expr} END"


def sum_expression(expr: str, sql_type: str) -> str:
    """`sum` over `expr` in `Int256` or `Decimal256`, ClickHouse wrapping a narrower total silently.

    A 256-bit type has nothing wider, so its sum is published only while no partial sum can
    reach the type's limit, and is NULL otherwise.
    """

    if is_floating_type(sql_type):
        return f"sum({expr})"

    base = base_type(sql_type)
    decimal = _DECIMAL_RE.search(sql_type)

    if base in ("int256", "uint256") or (decimal is not None and int(decimal[1]) > 38):
        scale = f" * 1e{decimal[2]}" if decimal is not None else ""
        cap = "1.15e77" if base == "uint256" else "5.7e76"
        largest = f"greatest(abs(toFloat64(min({expr}))), abs(toFloat64(max({expr}))))"
        bound = f"toFloat64(count({expr})) * {largest}{scale}"

        return f"if({bound} < {cap}, sum({expr}), NULL)"

    if decimal is not None:
        return f"sum(toDecimal256({expr}, {decimal[2]}))"

    return f"sum(toInt256({expr}))"


def mean_expression(expr: str, sql_type: str) -> str:
    """The mean of `expr`: `avg` on a floating type, else the exact sum over the count."""

    if is_floating_type(sql_type):
        return f"avg({expr})"

    return f"toFloat64({sum_expression(expr, sql_type)}) / nullIf(count({expr}), 0)"


def render_text(expr: str, sql_type: str) -> str:
    """`expr` as the engine's own text - the string a string-like column's `length` and
    `empty_count` are measured over, and the value it publishes (SPEC 2.2.4).
    """

    del sql_type

    return f"toString({expr})"


temporal_shape = partial(lookup_temporal_shape, _SHAPES)


def stores_below_microsecond(sql_type: str) -> bool:
    """Whether a `DateTime64(p)` or `Time64(p)` keeps digits a microsecond drops (SPEC 2.2.4)."""

    match = _PRECISION_RE.search(sql_type)

    return (
        base_type(sql_type) in ("datetime64", "time64") and match is not None and int(match[1]) > 6
    )


def render_domain(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4: `T` separator, no trailing `.000000`."""

    shape = temporal_shape(sql_type)

    match shape:
        case "date" | "year":
            return f"toString({expr})"
        case "timestamp":
            return _stripped(
                call("formatDateTime", call("toDateTime64", expr, "6"), "'%Y-%m-%dT%H:%i:%S.%f'"),
            )
        case "timestamp_tz":
            return _utc_instant(expr, sql_type)
        case "time" | "time_tz":
            return _clock(expr, sql_type)
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def day_span(low: str, high: str, sql_type: str) -> str:
    """Whole days from `low` to `high` (SPEC 2.2.4), to the nanosecond where the type stores one."""

    # `dateDiff` counts unit boundaries, so days are microseconds (SPEC 2.2.4); a `Date` stays a
    # date, since one widened to an instant passes through the session zone's daylight saving.
    if temporal_shape(sql_type) == "date":
        return f"dateDiff('day', {low}, {high})"

    if stores_below_microsecond(sql_type):
        # `dateDiff('nanosecond')` wraps across DateTime64(9)'s range, so the difference is Int128.
        ticks, precision = _ticks(high, sql_type), _precision(sql_type)
        nanos = f"(toInt128({ticks}) - {_ticks(low, sql_type)}) * {10 ** (9 - precision)}"

        return f"intDiv({nanos}, 86400000000000)"

    return f"intDiv(dateDiff('microsecond', {low}, {high}), 86400000000)"


def frame_zone(sql_type: str) -> str:
    """The zone literal a `DateTime` is cut into days in: its declared zone, else `'UTC'`."""

    match = _ZONE_RE.search(sql_type)

    return f"'{match[1]}'" if match else "'UTC'"


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
            return _utc_instant(expr, sql_type)
        case "time" | "time_tz":
            return _clock(expr, sql_type)
        case None:
            raise ValueError(f"not a temporal type: {sql_type!r}")
        case _:
            assert_never(shape)


def _utc_instant(expr: str, sql_type: str) -> str:
    if stores_below_microsecond(sql_type):
        return f"concat({_nanosecond(expr, sql_type, '%Y-%m-%dT%H:%i:%S.%f')}, 'Z')"

    utc = call("toTimeZone", call("toDateTime64", expr, "6"), "'UTC'")

    return f"concat({_stripped(f"formatDateTime({utc}, '%Y-%m-%dT%H:%i:%S.%f')")}, 'Z')"


def _clock(expr: str, sql_type: str) -> str:
    if stores_below_microsecond(sql_type):
        return _nanosecond(expr, sql_type, "%H:%i:%S.%f")

    return _stripped(
        call("formatDateTime", call("toDateTime64", expr, "6", "'UTC'"), "'%H:%i:%S.%f'"),
    )


def _nanosecond(expr: str, sql_type: str, picture: str) -> str:
    # The microsecond is floored through the tick count: `toDateTime64(x, 6)` rounds a
    # pre-1970 value toward the epoch, and `%f` prints a scale's digits under a session setting.
    ticks, precision = _ticks(expr, sql_type), _precision(sql_type)
    per_micro = 10 ** (precision - 6)
    below = f"positiveModulo({ticks}, {per_micro})"
    micro = call("fromUnixTimestamp64Micro", f"intDiv({ticks} - {below}, {per_micro})", "'UTC'")
    nanos = f"{below} * {10 ** (9 - precision)}"
    digits = f"if({below} = 0, '', leftPad(toString({nanos}), 3, '0'))"

    return _stripped(f"concat(formatDateTime({micro}, '{picture}'), {digits})")


def _ticks(expr: str, sql_type: str) -> str:
    # `DateTime64(7)`/`(8)` reaches 2299; its own ticks hold that, nanoseconds overflow at 2262.
    if base_type(sql_type) == "time64":
        return call("toUnixTimestamp64Nano", call("toDateTime64", expr, "9", "'UTC'"))

    precision = str(_precision(sql_type))

    return call("reinterpretAsInt64", call("toDateTime64", expr, precision, "'UTC'"))


def _precision(sql_type: str) -> int:
    if base_type(sql_type) == "time64":
        return 9

    match = _PRECISION_RE.search(sql_type)

    return int(match[1]) if match else 3


def _stripped(rendered: str) -> str:
    return f"replaceRegexpOne({rendered}, '\\\\.000000$', '')"
