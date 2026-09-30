"""KMV key sketch, in-database (SPEC 2.2.14) - `SUBSTR(MD5(v), 17, 16)` reads the digest's
low 64 bits, and the `0x` prefix plus `UBIGINT` cast parses them unsigned big-endian.
"""

from __future__ import annotations

from dbprint.spec.sketch import SketchKind
from . import stats
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
    quoted_col = stats._qualified(column)
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
    """Low 64 bits of MD5(`value_expr`), unsigned, as a UBIGINT (SPEC 2.2.14)."""

    return f"(('0x' || SUBSTR(MD5({value_expr}), 17, 16))::UBIGINT)"
