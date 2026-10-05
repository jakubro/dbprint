"""KMV key sketch, in-database (SPEC 2.2.14) - `conv` returns a STRING that sorts
lexicographically, so the result casts to DECIMAL(20,0), wide enough for the unsigned range.
"""

from __future__ import annotations

from dbprint.spec.sketch import SketchKind
from .connection import DIALECT
from .rendering import render_canonical, render_operand
from ..identifiers import Identity, qualified, quote


def canonical_value(
    identity: Identity,
    column: str,
    sql_type: str,
    kind: SketchKind,
) -> tuple[str, str]:
    """The column reference a key sketch filters on, and the canonical value it hashes."""

    quoted_col = qualified(quote(column, DIALECT))

    return quoted_col, render_canonical(render_operand(quoted_col, sql_type), sql_type, kind)


def low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), unsigned, as DECIMAL(20,0) (SPEC 2.2.14)."""

    return f"CAST(CONV(SUBSTR(MD5({value_expr}), 17, 16), 16, 10) AS DECIMAL(20,0))"
