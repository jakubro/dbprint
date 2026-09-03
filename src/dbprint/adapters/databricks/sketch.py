"""KMV key sketch, in-database (SPEC 2.2.14) - `conv` returns a STRING that sorts
lexicographically, so the result casts to DECIMAL(20,0), wide enough for the unsigned range.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dbprint.spec.sketch import SketchKind
from . import stats
from .connection import exec_query


if TYPE_CHECKING:
    from .connection import Cursor


def compute_key_sketch(
    cursor: Cursor,
    fqn: str,
    column: str,
    sql_type: str,
    kind: SketchKind,
    k: int,
) -> tuple[int, ...]:
    """The k smallest low-64-bit MD5 hashes of the column's distinct non-null values."""

    quoted_table = stats._quote_qualified(fqn)
    quoted_col = stats._quote_ident(column)
    canonical = _canonical_expr(quoted_col, kind, sql_type)
    low64 = _low64_expr("v")

    rows = exec_query(
        cursor,
        f"""
        SELECT {low64} AS h
        FROM (
            SELECT DISTINCT {canonical} AS v
            FROM {quoted_table}
            WHERE {quoted_col} IS NOT NULL
        ) t
        ORDER BY h
        LIMIT {int(k)}
        """,
    ).fetchall()

    return tuple(int(r[0]) for r in rows)


def _canonical_expr(quoted_col: str, kind: SketchKind, sql_type: str) -> str:
    """SPEC 2.2.14's canonical byte form for one SQL value, as a SQL expression."""

    if kind == "temporal":
        return stats._render_calendar_bound(quoted_col, sql_type)

    return f"CAST({quoted_col} AS STRING)"


def _low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), unsigned, as DECIMAL(20,0) (SPEC 2.2.14)."""

    return f"CAST(CONV(SUBSTR(MD5({value_expr}), 17, 16), 16, 10) AS DECIMAL(20,0))"
