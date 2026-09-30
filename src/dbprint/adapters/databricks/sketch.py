"""KMV key sketch, in-database (SPEC 2.2.14) - `conv` returns a STRING that sorts
lexicographically, so the result casts to DECIMAL(20,0), wide enough for the unsigned range.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dbprint.spec.sketch import SketchKind
from .connection import DIALECT, exec_query
from .rendering import render_canonical, render_operand
from ..identifiers import SOURCE_ALIAS, Identity, qualified, quote
from ..sql_layout import indented


if TYPE_CHECKING:
    from .connection import Cursor


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
    quoted_col = qualified(quote(column, DIALECT))
    canonical = render_canonical(render_operand(quoted_col, sql_type), sql_type, kind)
    low64 = _low64_expr("dst.v")

    rows = exec_query(
        cursor,
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
        LIMIT {int(k)}
        """,
    ).fetchall()

    return tuple(int(r[0]) for r in rows)


def _low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), unsigned, as DECIMAL(20,0) (SPEC 2.2.14)."""

    return f"CAST(CONV(SUBSTR(MD5({value_expr}), 17, 16), 16, 10) AS DECIMAL(20,0))"
