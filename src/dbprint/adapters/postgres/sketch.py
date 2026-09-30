"""KMV key sketch, in-database (SPEC 2.2.14). See ARCHITECTURE.md 2.

`compute_key_sketch` issues one statement: canonicalize each distinct non-null value,
hash it, keep the k smallest. Postgres has no unsigned 64-bit integer, so `_low64_expr`
recombines MD5's low 8 bytes as two 32-bit halves into a NUMERIC, which holds the full
0..2^64-1 range instead of wrapping negative above 2^63.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dbprint.spec.sketch import SketchKind
from .connection import exec_query
from .rendering import render_canonical, render_operand
from ..identifiers import SOURCE_ALIAS, Identity
from ..sql_layout import indented


if TYPE_CHECKING:
    import psycopg


def compute_key_sketch(
    conn: psycopg.Connection,
    identity: Identity,
    column: str,
    sql_type: str,
    kind: SketchKind,
    k: int,
) -> tuple[int, ...]:
    """The k smallest low-64-bit MD5 hashes of the column's distinct non-null values."""

    quoted_table = identity.quoted()
    quoted_col = identity.source_column(column)
    canonical = render_canonical(render_operand(quoted_col, sql_type), sql_type, kind)
    low64 = _low64_expr("dst.v")

    rows = exec_query(
        conn,
        f"""
        SELECT
          {indented(low64, 10)} AS h
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
        LIMIT %s
        """,
        (k,),
    ).fetchall()

    return tuple(int(r[0]) for r in rows)


def _low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), unsigned, as NUMERIC (SPEC 2.2.14).

    Split into two 32-bit halves because a signed `bigint` sorts a hash carrying the top
    bit as negative, corrupting "smallest k".
    """

    hi = f"('x' || SUBSTRING(MD5({value_expr}), 17, 8))::BIT(32)::BIGINT::NUMERIC"
    lo = f"('x' || SUBSTRING(MD5({value_expr}), 25, 8))::BIT(32)::BIGINT::NUMERIC"

    return f"({hi} * 4294967296::NUMERIC + {lo})"
