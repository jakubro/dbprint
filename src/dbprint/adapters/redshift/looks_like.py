"""Distinct-value sampling for `looks_like` detection - the oversample uses `RANDOM()`,
Redshift having no `TABLESAMPLE`; the distinct set is hash-ordered either way (SPEC 4.1.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import stats
from .connection import exec_query
from .introspect import resolve_column, table_rows_estimate
from ..base import MIN_SAMPLE_DRAW, TableScope, seed_from_fqn


if TYPE_CHECKING:
    from .connection import Cursor


SAMPLE_RATE_MULTIPLIER = 10  # over-sample to compensate for the DISTINCT filter
SMALL_TABLE_FACTOR = 10  # row_count < n * factor -> direct DISTINCT path


def sample_distinct(
    cursor: Cursor,
    fqn: str,
    column: str,
    n: int,
    scope: TableScope | None = None,
) -> list[Any]:
    """Return up to n distinct non-null sampled values for the column - scoped like every other
    statistic, and a predicate-starved draw is re-taken directly.
    """

    quoted = stats._quote_qualified(fqn)
    cn = stats._quote_ident(resolve_column(cursor, fqn, column))
    seed = seed_from_fqn(fqn, stats.SEED_MODULUS)
    source = stats._source(quoted, scope, seed)
    estimate = _scoped_estimate(table_rows_estimate(cursor, fqn), scope)

    if estimate <= 0 or estimate < n * SMALL_TABLE_FACTOR:
        return _distinct(cursor, source, cn, n, seed)

    oversampled = (
        f"(SELECT {cn} AS v FROM {source} WHERE {cn} IS NOT NULL "
        f"ORDER BY RANDOM() LIMIT {int(n * SAMPLE_RATE_MULTIPLIER)}) sampled"
    )
    values = _distinct(cursor, oversampled, "v", n, seed)

    if _starved(scope, values, n):
        return _distinct(cursor, source, cn, n, seed)

    return values


def _starved(scope: TableScope | None, values: list[Any], n: int) -> bool:
    """Whether the draw came back too thin to infer from, and re-reading would help - only a
    predicate can starve it, since a fraction sizes the draw to the rate it asked for.
    """

    if scope is None or not scope.filter:
        return False

    return len(values) < min(n, MIN_SAMPLE_DRAW)


def _scoped_estimate(estimate: int, scope: TableScope | None) -> float:
    """Rows the scoped read covers: a fraction scales the catalog estimate, a predicate cannot."""

    if scope is None or scope.sample is None:
        return float(estimate)

    return estimate * scope.sample


def _distinct(cursor: Cursor, source: str, quoted_col: str, n: int, seed: int) -> list[Any]:
    """Up to n distinct non-null values of the column from one source expression, ordered by a
    hash of the value (SPEC 4.1.2) - a fixed permutation, reproducible under the table's seed.
    """

    rows = exec_query(
        cursor,
        f"""
        SELECT v FROM (
            SELECT DISTINCT {quoted_col} AS v
            FROM {source}
            WHERE {quoted_col} IS NOT NULL
        ) t
        ORDER BY MD5(%s || v::varchar)
        LIMIT %s
        """,
        (str(seed), n),
    ).fetchall()

    return [r[0] for r in rows]
