"""KMV key sketch, in-database (SPEC 2.2.14). See ARCHITECTURE.md 2.

MySQL has a native `UNSIGNED BIGINT`, so a 16-hex-digit string read through `CONV` and cast
`UNSIGNED` holds the full 0..2^64-1 range directly.
"""

from __future__ import annotations

from dbprint.spec.sketch import SketchKind
from . import stats
from .connection import Cursor, exec_query
from .identity import Identity


def compute_key_sketch(
    cursor: Cursor,
    identity: Identity,
    column: str,
    sql_type: str,
    kind: SketchKind,
    k: int,
) -> tuple[int, ...]:
    """The k smallest low-64-bit MD5 hashes of the column's distinct non-null values."""

    quoted_table = identity.quoted()
    quoted_col = stats._quote_ident(column)
    canonical = _canonical_expr(quoted_col, kind, sql_type)
    low64 = _low64_expr("v")

    # k is interpolated, not bound: a temporal canonical expression carries DATE_FORMAT's
    # own literal `%` sequences, which collide with the connector's `%s` substitution.
    rows = exec_query(
        cursor,
        f"""
        SELECT {low64} AS h
        FROM (
            SELECT DISTINCT {canonical} AS v
            FROM {quoted_table}
            WHERE {quoted_col} IS NOT NULL
        ) t
        ORDER BY h
        LIMIT {int(k)}
        """,
    ).fetchall()

    return tuple(int(r[0]) for r in rows)


def _canonical_expr(quoted_col: str, kind: SketchKind, sql_type: str) -> str:
    """SPEC 2.2.14's canonical byte form for one SQL value, as a SQL expression.

    Integer/decimal/text all reduce to `CAST(... AS CHAR)`, which preserves a DECIMAL
    column's own scale (SPEC 2.2.6). `boolean` is unreachable through this
    adapter - MySQL has no native BOOLEAN - but is implemented to honor the ABC contract.
    Temporal does not reuse `_render_calendar_bound`, which may omit the `Z` suffix
    (SPEC 2.2.4) where SPEC 2.2.14 requires it so cross-adapter hashing agrees.
    """

    if kind == "boolean":
        return f"(CASE WHEN {quoted_col} THEN 'true' ELSE 'false' END)"

    if kind == "temporal":
        return _canonical_temporal_expr(quoted_col, sql_type)

    return f"CAST({quoted_col} AS CHAR)"


def _canonical_temporal_expr(quoted_col: str, sql_type: str) -> str:
    """SPEC 2.2.14's temporal canonical form: `Z` iff the type is timezone-aware.

    MySQL's only timezone-aware type is `TIMESTAMP`, stored UTC and converted to the
    session zone on read; the rest carry no zone, so no `Z` and no conversion.
    """

    is_timestamp = stats._matches(sql_type, stats._TZ_TYPES)
    is_date_only = stats._matches(sql_type, stats._DATE_ONLY_TYPES)
    picture = "%Y-%m-%d" if is_date_only else "%Y-%m-%dT%H:%i:%s.%f"
    source_expr = (
        f"CONVERT_TZ({quoted_col}, @@session.time_zone, '+00:00')" if is_timestamp else quoted_col
    )
    body = f"DATE_FORMAT({source_expr}, '{picture}')"

    if not is_date_only:
        body = f"REGEXP_REPLACE({body}, '\\\\.000000$', '')"

    if is_timestamp:
        body = f"CONCAT({body}, 'Z')"

    return body


def _low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), unsigned, as MySQL's native UNSIGNED BIGINT."""

    return f"CAST(CONV(SUBSTRING(MD5({value_expr}), 17, 16), 16, 10) AS UNSIGNED)"
