"""KMV key sketch, in-database (SPEC 2.2.14) - not `halfMD5`, which takes the upper 8 bytes,
and the low-half slice is reversed first since `reinterpretAsUInt64` reads little-endian.
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
    """Low 64 bits of MD5(`value_expr`), as ClickHouse's native UInt64."""

    return f"reinterpretAsUInt64(reverse(substring(MD5({value_expr}), 9, 8)))"
