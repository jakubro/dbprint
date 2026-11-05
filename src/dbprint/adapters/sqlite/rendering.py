"""SQLite's one reading of a column into SQL text, for every statement that renders a value.

SQLite stores no temporal value, so a date is published as the text it is stored as (SPEC 2.2.4)
and only the sketch spells it ISO; a value is otherwise written by its storage class.
"""

from __future__ import annotations

from functools import partial

from dbprint.spec.classification import is_binary_type, is_boolean_type, is_floating_type
from dbprint.spec.sketch import SketchKind
from ..base import lookup_operand


# The largest finite binary64; NaN is stored as NULL, so only the infinities lie beyond it.
_MAX_FINITE = "1.7976931348623157e308"

# Types with no operator a statistic needs, measured through a lossless comparable form.
_OPERANDS: dict[str, str] = {}


render_operand = partial(lookup_operand, _OPERANDS)


def finite_value(expr: str, sql_type: str) -> str:
    """`expr` where finite, NULL for the infinities; unchanged on a type holding neither."""

    if not is_floating_type(sql_type):
        return expr

    return f"CASE WHEN ABS({expr}) <= {_MAX_FINITE} THEN {expr} END"


def render_text(expr: str, sql_type: str) -> str:
    """`expr` as SQLite's own text - what `length`, `empty_count` and a value list read."""

    del sql_type

    return f"CAST({expr} AS TEXT)"


def render_value(expr: str, sql_type: str) -> str:
    """`expr` as a listed value: a BLOB as lowercase hex, a boolean's `0`/`1` as itself, every
    other storage class as stored (SPEC 3.3).
    """

    if is_binary_type(sql_type):
        return render_binary(expr)

    return f"CASE WHEN TYPEOF({expr}) = 'blob' THEN LOWER(HEX({expr})) ELSE {expr} END"


def render_binary(expr: str) -> str:
    """A binary value as SPEC 2.2.4 spells it: lowercase hex, no prefix."""

    return f"LOWER(HEX({expr}))"


def instant(expr: str) -> str:
    """The point in time a stored date or date-time text names, for ordering it as one."""

    return f"JULIANDAY({expr})"


def render_canonical(expr: str, sql_type: str, kind: SketchKind) -> str:
    """SPEC 2.2.14's canonical byte form for one stored value, as a SQL expression."""

    if kind == "binary":
        return expr

    if kind == "temporal":
        return f"DBPRINT_ISO({expr})"

    if kind == "boolean" or is_boolean_type(sql_type):
        return f"CASE WHEN {expr} THEN 'true' ELSE 'false' END"

    return f"CAST({expr} AS TEXT)"
