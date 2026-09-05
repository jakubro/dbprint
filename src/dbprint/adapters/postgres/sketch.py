"""KMV key sketch, in-database (SPEC 2.2.14). See ARCHITECTURE.md 2.

`compute_key_sketch` issues one statement: canonicalize each distinct non-null value,
hash it, keep the k smallest. Postgres has no unsigned 64-bit integer, so `_low64_expr`
recombines MD5's low 8 bytes as two 32-bit halves into a NUMERIC, which holds the full
0..2^64-1 range instead of wrapping negative above 2^63.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dbprint.spec.sketch import SketchKind
from . import stats
from .connection import exec_query
from .identity import Identity
from .introspect import resolve_column


if TYPE_CHECKING:
    import psycopg


def compute_key_sketch(
    conn: psycopg.Connection,
    identity: Identity,
    column: str,
    sql_type: str,
    kind: SketchKind,
    k: int,
) -> tuple[int, ...]:
    """The k smallest low-64-bit MD5 hashes of the column's distinct non-null values."""

    quoted_table = identity.quoted()
    quoted_col = stats._quote_ident(resolve_column(conn, identity, column))
    canonical = _canonical_expr(quoted_col, kind, sql_type)
    low64 = _low64_expr("v")

    rows = exec_query(
        conn,
        f"""
        SELECT {low64} AS h
        FROM (
            SELECT DISTINCT {canonical} AS v
            FROM {quoted_table}
            WHERE {quoted_col} IS NOT NULL
        ) t
        ORDER BY h
        LIMIT %s
        """,
        (k,),
    ).fetchall()

    return tuple(int(r[0]) for r in rows)


def _canonical_expr(quoted_col: str, kind: SketchKind, sql_type: str) -> str:
    """SPEC 2.2.14's canonical byte form for one SQL value, as a SQL expression.

    Postgres's default `::text` rendering already matches the canonical form for every
    non-temporal kind. Temporal reuses `_render_calendar_bound`: Postgres
    appends `Z` itself, so SPEC 2.2.4 and SPEC 2.2.14 coincide and need no override.
    """

    if kind == "temporal":
        return stats._render_calendar_bound(quoted_col, sql_type)

    return f"{quoted_col}::text"


def _low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), unsigned, as NUMERIC (SPEC 2.2.14).

    Split into two 32-bit halves because a signed `bigint` sorts a hash carrying the top
    bit as negative, corrupting "smallest k".
    """

    hi = f"('x' || substring(md5({value_expr}), 17, 8))::bit(32)::bigint::numeric"
    lo = f"('x' || substring(md5({value_expr}), 25, 8))::bit(32)::bigint::numeric"

    return f"({hi} * 4294967296::numeric + {lo})"
