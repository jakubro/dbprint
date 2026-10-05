"""KMV key sketch, in-database (SPEC 2.2.14). See ARCHITECTURE.md 2.

`statements.key_sketch` issues one statement: canonicalize each distinct non-null value,
hash it, keep the k smallest. Postgres has no unsigned 64-bit integer, so `low64_expr`
recombines MD5's low 8 bytes as two 32-bit halves into a NUMERIC, which holds the full
0..2^64-1 range instead of wrapping negative above 2^63.
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


def low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), unsigned, as NUMERIC (SPEC 2.2.14).

    Split into two 32-bit halves because a signed `bigint` sorts a hash carrying the top
    bit as negative, corrupting "smallest k".
    """

    hi = f"('x' || SUBSTRING(MD5({value_expr}), 17, 8))::BIT(32)::BIGINT::NUMERIC"
    lo = f"('x' || SUBSTRING(MD5({value_expr}), 25, 8))::BIT(32)::BIGINT::NUMERIC"

    return f"({hi} * 4294967296::NUMERIC + {lo})"
