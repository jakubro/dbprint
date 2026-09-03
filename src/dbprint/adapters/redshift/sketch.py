"""KMV key sketch, in-database (SPEC 2.2.14) - with no unsigned 64-bit integer and no `bit`
cast, MD5's low 8 bytes read as two `STRTOL` calls recombined in NUMERIC.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dbprint.spec.sketch import SketchKind
from . import stats
from .connection import exec_query
from .introspect import resolve_column


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
    quoted_col = stats._quote_ident(resolve_column(cursor, fqn, column))
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
    """SPEC 2.2.14's canonical byte form for one SQL value, as a SQL expression - `::varchar`
    already matches for every non-temporal kind, and temporal reuses `_render_calendar_bound`.
    """

    if kind == "temporal":
        return stats._render_calendar_bound(quoted_col, sql_type)

    return f"{quoted_col}::varchar"


def _low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), unsigned, as NUMERIC (SPEC 2.2.14) - split into two
    `STRTOL` calls, avoiding both a 60-bit truncation and a signed 64-bit overflow.
    """

    hi = f"STRTOL(SUBSTRING(MD5({value_expr}), 17, 8), 16)::NUMERIC"
    lo = f"STRTOL(SUBSTRING(MD5({value_expr}), 25, 8), 16)::NUMERIC"

    return f"({hi} * 4294967296::NUMERIC + {lo})"
