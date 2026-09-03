"""KMV key sketch, in-database (SPEC 2.2.14) - the low 64 bits of MD5 are assembled bitwise into
a signed INT64 pattern, reinterpreted as unsigned in Python.

Bitwise assembly stays in native 64-bit integers; the NUMERIC-multiply shape `redshift/sketch.py`
uses loses precision above 2**53 on the emulator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dbprint.spec.sketch import SketchKind
from . import stats
from .connection import exec_query
from .identity import Identity


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
    quoted_col = identity.quoted_column(column)
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
        ORDER BY (h < 0), h
        LIMIT {int(k)}
        """,
    ).fetchall()

    return tuple(_unsigned(int(r[0])) for r in rows)


def _unsigned(signed_int64: int) -> int:
    """Reinterpret a signed INT64 bit pattern as its unsigned 64-bit value."""

    return signed_int64 + (1 << 64) if signed_int64 < 0 else signed_int64


def _canonical_expr(quoted_col: str, kind: SketchKind, sql_type: str) -> str:
    """SPEC 2.2.14's canonical byte form for one SQL value, as a SQL expression."""

    if kind == "temporal":
        return stats._render_calendar_bound(quoted_col, sql_type)

    return f"CAST({quoted_col} AS STRING)"


def _low64_expr(value_expr: str) -> str:
    """The low 64 bits of MD5(`value_expr`) as a signed INT64 pattern (SPEC 2.2.14) - two 32-bit
    halves shifted and OR'd, each safe from a signed-64 overflow on its own.
    """

    hex_digest = f"TO_HEX(MD5({value_expr}))"
    hi = f"CAST(CONCAT('0x', SUBSTR({hex_digest}, 17, 8)) AS INT64)"
    lo = f"CAST(CONCAT('0x', SUBSTR({hex_digest}, 25, 8)) AS INT64)"

    return f"(({hi} << 32) | {lo})"
