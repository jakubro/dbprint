"""KMV key sketch, in-database (SPEC 2.2.14) - SQLite has no MD5, so a registered function hashes
each canonical value, and its 64-bit integers are signed: the adapter sorts and reads them unsigned.
"""

from __future__ import annotations

from dbprint.spec.sketch import SketchKind
from .rendering import render_canonical
from ..identifiers import Identity


def canonical_value(
    identity: Identity,
    column: str,
    sql_type: str,
    kind: SketchKind,
) -> tuple[str, str]:
    """The column reference a key sketch filters on, and the canonical value it hashes."""

    quoted_col = identity.source_column(column)

    return quoted_col, render_canonical(quoted_col, sql_type, kind)


def low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`) as a signed integer (SPEC 2.2.14)."""

    return f"DBPRINT_LOW64({value_expr})"


def unsigned(signed_int64: int) -> int:
    """Reinterpret a signed 64-bit integer's bit pattern as its unsigned value."""

    return signed_int64 + (1 << 64) if signed_int64 < 0 else signed_int64
