"""KMV key sketch, in-database (SPEC 2.2.14) - the low 64 bits of MD5 are assembled bitwise into
a signed INT64 pattern, reinterpreted as unsigned in Python.

Bitwise assembly stays in native 64-bit integers; the NUMERIC-multiply shape `redshift/sketch.py`
uses loses precision above 2**53 on the emulator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dbprint.spec.sketch import SketchKind
from .connection import exec_query
from .rendering import render_canonical, render_operand
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
    """The k smallest low-64-bit MD5 hashes of the column's distinct non-null values.

    `h` is a signed INT64 over the full unsigned pattern, so `ORDER BY (h < 0), h` sorts it as
    unsigned without computing the unsigned value in SQL; `_unsigned` repeats that in Python.
    """

    quoted_table = identity.quoted()
    quoted_col = identity.source_column(column)
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
          (h < 0), h
        LIMIT {int(k)}
        """,
    ).fetchall()

    return tuple(_unsigned(int(r[0])) for r in rows)


def _unsigned(signed_int64: int) -> int:
    """Reinterpret a signed INT64 bit pattern as its unsigned 64-bit value."""

    return signed_int64 + (1 << 64) if signed_int64 < 0 else signed_int64


def _low64_expr(value_expr: str) -> str:
    """The low 64 bits of MD5(`value_expr`) as a signed INT64 pattern (SPEC 2.2.14) - two 32-bit
    halves shifted and OR'd, each safe from a signed-64 overflow on its own.
    """

    hex_digest = f"TO_HEX(MD5({value_expr}))"
    hi = f"CAST(CONCAT('0x', SUBSTR({hex_digest}, 17, 8)) AS INT64)"
    lo = f"CAST(CONCAT('0x', SUBSTR({hex_digest}, 25, 8)) AS INT64)"

    return f"(({hi} << 32) | {lo})"
