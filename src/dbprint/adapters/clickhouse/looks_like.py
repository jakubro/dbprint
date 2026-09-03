"""Distinct-value sampling for `looks_like` detection - the oversample uses native `SAMPLE`
and degrades to a direct scan, needing no cross-statement coherence to refuse over.
"""

from __future__ import annotations

from typing import Any

from . import introspect, stats
from .connection import Cursor, exec_query
from ..base import TableScope, seed_from_fqn
from ..errors import QueryFailed


SMALL_TABLE_FACTOR = 10  # row_count < n * factor -> direct DISTINCT path
SAMPLE_RATE_MULTIPLIER = 10  # over-sample to compensate for the DISTINCT filter


def sample_distinct(
    cursor: Cursor,
    fqn: str,
    column: str,
    n: int,
    scope: TableScope | None = None,
) -> list[Any]:
    """Return up to n distinct non-null sampled values for the column."""

    cn = stats._quote_ident(column)
    source = stats._source(fqn, scope)
    seed = seed_from_fqn(fqn, 2**31)

    if scope is None or not scope.narrows:
        estimate = introspect.estimate_row_count(cursor, fqn)

        if estimate >= n * SMALL_TABLE_FACTOR:
            oversampled = _try_oversample(cursor, source, cn, n, seed)

            if oversampled is not None:
                return oversampled

    return _distinct(cursor, source, cn, n, seed)


def _try_oversample(
    cursor: Cursor,
    quoted_table: str,
    cn: str,
    n: int,
    seed: int,
) -> list[Any] | None:
    """A fixed-size `SAMPLE` draw, or None when the table declares no sampling key."""

    oversampled = (
        f"(SELECT {cn} AS v FROM {quoted_table} SAMPLE {int(n * SAMPLE_RATE_MULTIPLIER)} "
        f"WHERE {cn} IS NOT NULL)"
    )

    try:
        return _distinct(cursor, oversampled, "v", n, seed)
    except QueryFailed:
        return None


def _distinct(cursor: Cursor, source: str, quoted_col: str, n: int, seed: int) -> list[Any]:
    """Up to n distinct non-null values of the column from one source expression, ordered by a
    hash of the seed and the value (SPEC 4.1.2) - a fixed, reproducible permutation.
    """

    rows = exec_query(
        cursor,
        f"""
        SELECT v FROM (
            SELECT DISTINCT {quoted_col} AS v
            FROM {source}
            WHERE {quoted_col} IS NOT NULL
        ) t
        ORDER BY halfMD5(concat(%s, toString(v)))
        LIMIT %s
        """,
        (str(seed), n),
    ).fetchall()

    return [r[0] for r in rows]
