"""KMV key sketch, in-database (SPEC 2.2.14) - `SUBSTR(MD5(v), 17, 16)` reads the digest's
low 64 bits, and the `0x` prefix plus `UBIGINT` cast parses them unsigned big-endian.
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
    """Low 64 bits of MD5(`value_expr`), unsigned, as a UBIGINT (SPEC 2.2.14)."""

    return f"(('0x' || SUBSTR(MD5({value_expr}), 17, 16))::UBIGINT)"
