"""KMV key sketch, in-database (SPEC 2.2.14). See ARCHITECTURE.md 2.

MySQL has a native `UNSIGNED BIGINT`, so a 16-hex-digit string read through `CONV` and cast
`UNSIGNED` holds the full 0..2^64-1 range directly.
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

    # k is interpolated, not bound: a temporal canonical expression carries DATE_FORMAT's
    # own literal `%` sequences, which collide with the connector's `%s` substitution.
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
    """Low 64 bits of MD5(`value_expr`), unsigned, as MySQL's native UNSIGNED BIGINT."""

    return f"CAST(CONV(SUBSTRING(MD5({value_expr}), 17, 16), 16, 10) AS UNSIGNED)"
