"""KMV key sketch, in-database (SPEC 2.2.14) - not `halfMD5`, which takes the upper 8 bytes,
and the low-half slice is reversed first since `reinterpretAsUInt64` reads little-endian.
"""

from __future__ import annotations

from dbprint.spec.sketch import SketchKind
from . import stats
from .connection import Cursor, exec_query


def compute_key_sketch(
    cursor: Cursor,
    fqn: str,
    column: str,
    sql_type: str,
    kind: SketchKind,
    k: int,
) -> tuple[int, ...]:
    """The k smallest low-64-bit MD5 hashes of the column's distinct non-null values."""

    quoted_table = stats._quote_qualified(fqn)
    quoted_col = stats._quote_ident(column)
    canonical = _canonical_expr(quoted_col, kind, sql_type)
    low64 = _low64_expr("v")

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
    """SPEC 2.2.14's canonical byte form for one SQL value, as a SQL expression."""

    if kind == "boolean":
        return f"(CASE WHEN {quoted_col} THEN 'true' ELSE 'false' END)"

    if kind == "temporal":
        return _canonical_temporal_expr(quoted_col, sql_type)

    return f"toString({quoted_col})"


def _canonical_temporal_expr(quoted_col: str, sql_type: str) -> str:
    """SPEC 2.2.14's temporal canonical form: `Z` iff the type carries a timezone - `DateTime`
    is rendered in UTC first, while `Date`/`Date32` carry no time component and no zone.
    """

    is_date_only = stats._matches(sql_type, ("date", "date32"))
    is_timestamp = not is_date_only

    if is_date_only:
        return f"toString({quoted_col})"

    rendered = (
        f"formatDateTime(toTimeZone(toDateTime64({quoted_col}, 6), 'UTC'), '%Y-%m-%dT%H:%i:%S.%f')"
    )
    # A whole-second value renders six trailing zeros; every other adapter's own canonical
    # form strips them (`postgres/sketch.py`, `mysql/sketch.py`), and the hash must agree.
    body = f"replaceRegexpOne({rendered}, '\\\\.000000$', '')"

    return f"concat({body}, 'Z')" if is_timestamp else body


def _low64_expr(value_expr: str) -> str:
    """Low 64 bits of MD5(`value_expr`), as ClickHouse's native UInt64."""

    return f"reinterpretAsUInt64(reverse(substring(MD5({value_expr}), 9, 8)))"
