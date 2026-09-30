"""KMV key sketch, in-database (SPEC 2.2.14) - with no unsigned 64-bit integer and no `bit`
cast, MD5's low 8 bytes read as two `STRTOL` calls recombined in NUMERIC.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dbprint.spec.sketch import SketchKind
from .connection import exec_query
from .rendering import render_canonical
from ..identifiers import SOURCE_ALIAS, Identity
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
    quoted_col = identity.source_column(column)
    canonical = render_canonical(quoted_col, sql_type, kind)
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
    """Low 64 bits of MD5(`value_expr`), unsigned, as NUMERIC (SPEC 2.2.14) - split into two
    `STRTOL` calls, avoiding both a 60-bit truncation and a signed 64-bit overflow.
    """

    hi = f"STRTOL(SUBSTRING(MD5({value_expr}), 17, 8), 16)::NUMERIC"
    lo = f"STRTOL(SUBSTRING(MD5({value_expr}), 25, 8), 16)::NUMERIC"

    return f"({hi} * 4294967296::NUMERIC + {lo})"
