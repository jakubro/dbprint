"""KMV key sketch, in-database (SPEC 2.2.14). See ARCHITECTURE.md 2.

MySQL has a native `UNSIGNED BIGINT`, so a 16-hex-digit string read through `CONV` and cast
`UNSIGNED` holds the full 0..2^64-1 range directly.
"""

from __future__ import annotations

from dbprint.spec.sketch import SketchKind
from . import stats
from .rendering import render_canonical
from ..identifiers import Identity


def canonical_value(
    identity: Identity,
    column: str,
    sql_type: str,
    kind: SketchKind,
) -> tuple[str, str]:
    """The column reference a key sketch filters on, and the canonical value it hashes."""

    quoted_col = stats._qualified(column)

    return quoted_col, render_canonical(quoted_col, sql_type, kind)


def low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), unsigned, as MySQL's native UNSIGNED BIGINT."""

    return f"CAST(CONV(SUBSTRING(MD5({value_expr}), 17, 16), 16, 10) AS UNSIGNED)"
