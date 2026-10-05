"""KMV key sketch, in-database (SPEC 2.2.14) - the low 64 bits of MD5 are assembled bitwise into
a signed INT64 pattern, reinterpreted as unsigned in Python.

Bitwise assembly stays in native 64-bit integers; the NUMERIC-multiply shape `redshift/sketch.py`
uses loses precision above 2**53 on the emulator.
"""

from __future__ import annotations

from dbprint.spec.sketch import SketchKind
from .rendering import render_canonical, render_operand
from ..identifiers import Identity


def canonical_value(
    identity: Identity,
    column: str,
    sql_type: str,
    kind: SketchKind,
) -> tuple[str, str]:
    """The column reference a key sketch filters on, and the canonical value it hashes."""

    quoted_col = identity.source_column(column)

    return quoted_col, render_canonical(render_operand(quoted_col, sql_type), sql_type, kind)


def unsigned(signed_int64: int) -> int:
    """Reinterpret a signed INT64 bit pattern as its unsigned 64-bit value."""

    return signed_int64 + (1 << 64) if signed_int64 < 0 else signed_int64


def low64_expr(value_expr: str) -> str:
    """The low 64 bits of MD5(`value_expr`) as a signed INT64 pattern (SPEC 2.2.14) - two 32-bit
    halves shifted and OR'd, each safe from a signed-64 overflow on its own.
    """

    hex_digest = f"TO_HEX(MD5({value_expr}))"
    hi = f"CAST(CONCAT('0x', SUBSTR({hex_digest}, 17, 8)) AS INT64)"
    lo = f"CAST(CONCAT('0x', SUBSTR({hex_digest}, 25, 8)) AS INT64)"

    return f"(({hi} << 32) | {lo})"
