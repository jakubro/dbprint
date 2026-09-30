"""KMV key sketch, in-database (SPEC 2.2.14) - not `halfMD5`, which takes the upper 8 bytes,
and the low-half slice is reversed first since `reinterpretAsUInt64` reads little-endian.
"""

from __future__ import annotations

from dbprint.spec.sketch import SketchKind
from .connection import Cursor, exec_query
from .rendering import render_canonical
from ..identifiers import SOURCE_ALIAS, Identity
from ..sql_layout import indented


def compute_key_sketch(
    cursor: Cursor,
    identity: Identity,
    column: str,
    sql_type: str,
    kind: SketchKind,
    k: int,
) -> tuple[int, ...]:
    """The k smallest low-64-bit MD5 hashes of the column's distinct non-null values."""

    quoted_table = identity.quoted()
    quoted_col = identity.source_column(column)
    canonical = render_canonical(quoted_col, sql_type, kind)
    low64 = _low64_expr("dst.v")

    rows = exec_query(
        cursor,
        f"""
        SELECT
          {low64} AS h
        FROM
          (
            SELECT DISTINCT
              {indented(canonical, 14)} AS v
            FROM
              {quoted_table} {SOURCE_ALIAS}
            WHERE
              {quoted_col} IS NOT NULL
          ) dst
        ORDER BY
          h
        LIMIT {int(k)}
        """,
    ).fetchall()

    return tuple(int(r[0]) for r in rows)


def _low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), as ClickHouse's native UInt64."""

    return f"reinterpretAsUInt64(reverse(substring(MD5({value_expr}), 9, 8)))"
