"""KMV key sketch, in-database (SPEC 2.2.14) - with no unsigned 64-bit integer and no `bit`
cast, MD5's low 8 bytes read as two `STRTOL` calls recombined in NUMERIC.
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
    """Low 64 bits of MD5(`value_expr`), unsigned, as NUMERIC (SPEC 2.2.14) - split into two
    `STRTOL` calls, avoiding both a 60-bit truncation and a signed 64-bit overflow.
    """

    hi = f"STRTOL(SUBSTRING(MD5({value_expr}), 17, 8), 16)::NUMERIC"
    lo = f"STRTOL(SUBSTRING(MD5({value_expr}), 25, 8), 16)::NUMERIC"

    return f"({hi} * 4294967296::NUMERIC + {lo})"
