"""KMV key sketch, in-database (SPEC 2.2.14). See ARCHITECTURE.md 2.

`TO_NUMBER(hex_string, 'XXXX...')` reads the MD5 digest's low 64 bits as an unsigned hex
numeral into Snowflake's arbitrary-precision NUMBER, which holds the full 0..2^64-1 range
with no widening trick.
"""

from __future__ import annotations

from dbprint.spec.sketch import SketchKind
from .rendering import render_canonical
from ..identifiers import Identity


_HEX_FORMAT = "X" * 16  # 16 hex digits = 64 bits


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
    """Low 64 bits of MD5(`value_expr`), unsigned, as a NUMBER (SPEC 2.2.14).

    Not `MD5_NUMBER_LOWER64`: the hex-substring form reads the digest big-endian, which is
    SPEC 2.2.14's canonical order and what every published test vector encodes.
    """

    return f"TO_NUMBER(SUBSTR(MD5({value_expr}), 17, 16), '{_HEX_FORMAT}')"
