"""KMV key sketch, in-database (SPEC 2.2.14). See ARCHITECTURE.md 2.

`TO_NUMBER(hex_string, 'XXXX...')` reads the MD5 digest's low 64 bits as an unsigned hex
numeral into Snowflake's arbitrary-precision NUMBER, which holds the full 0..2^64-1 range
with no widening trick.
"""

from __future__ import annotations

from dbprint.spec.sketch import SketchKind
from .connection import Cursor, exec_query
from .rendering import render_canonical
from ..identifiers import SOURCE_ALIAS, Identity
from ..sql_layout import indented


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
              {identity.quoted()} {SOURCE_ALIAS}
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
    """Low 64 bits of MD5(`value_expr`), unsigned, as a NUMBER (SPEC 2.2.14).

    Not `MD5_NUMBER_LOWER64`: the hex-substring form reads the digest big-endian, which is
    SPEC 2.2.14's canonical order and what every published test vector encodes.
    """

    return f"TO_NUMBER(SUBSTR(MD5({value_expr}), 17, 16), '{_HEX_FORMAT}')"
