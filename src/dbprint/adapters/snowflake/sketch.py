"""KMV key sketch, in-database (SPEC 2.2.14). See ARCHITECTURE.md 2.

`TO_NUMBER(hex_string, 'XXXX...')` reads the MD5 digest's low 64 bits as an unsigned hex
numeral into Snowflake's arbitrary-precision NUMBER, which holds the full 0..2^64-1 range
with no widening trick.
"""

from __future__ import annotations

from dbprint.spec.sketch import SketchKind
from . import stats
from .connection import Cursor, exec_query
from .identity import Identity


_HEX_FORMAT = "X" * 16  # 16 hex digits = 64 bits


def compute_key_sketch(
    cursor: Cursor,
    identity: Identity,
    column: str,
    sql_type: str,
    kind: SketchKind,
    k: int,
) -> tuple[int, ...]:
    """The k smallest low-64-bit MD5 hashes of the column's distinct non-null values."""

    quoted_col = identity.quoted_column(column)
    canonical = _canonical_expr(quoted_col, kind, sql_type)
    low64 = _low64_expr("v")

    rows = exec_query(
        cursor,
        f"""
        SELECT {low64} AS h
        FROM (
            SELECT DISTINCT {canonical} AS v
            FROM {identity.quoted()}
            WHERE {quoted_col} IS NOT NULL
        ) t
        ORDER BY h
        LIMIT {int(k)}
        """,
    ).fetchall()

    return tuple(int(r[0]) for r in rows)


def _canonical_expr(quoted_col: str, kind: SketchKind, sql_type: str) -> str:
    """SPEC 2.2.14's canonical byte form for one SQL value, as a SQL expression.

    Snowflake's default `TO_VARCHAR` rendering already matches the canonical form for
    every non-temporal kind. Temporal reuses `_render_calendar_bound`: Snowflake appends
    `Z` itself, so SPEC 2.2.4 and SPEC 2.2.14 coincide and need no sketch-specific override.
    """

    if kind == "temporal":
        return stats._render_calendar_bound(quoted_col, sql_type)

    return f"TO_VARCHAR({quoted_col})"


def _low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), unsigned, as a NUMBER (SPEC 2.2.14).

    Not `MD5_NUMBER_LOWER64`: the hex-substring form reads the digest big-endian, which is
    SPEC 2.2.14's canonical order and what every published test vector encodes.
    """

    return f"TO_NUMBER(SUBSTR(MD5({value_expr}), 17, 16), '{_HEX_FORMAT}')"
