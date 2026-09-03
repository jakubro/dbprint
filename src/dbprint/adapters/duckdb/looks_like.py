"""Distinct value sampling for `looks_like` detection - `reservoir(...) REPEATABLE (...)`
reproduces exactly, so coherence with the rest of a sampled profile is row-level here.
"""

from __future__ import annotations

from typing import Any

from . import introspect, stats
from .connection import Cursor, exec_query
from ..base import MIN_SAMPLE_DRAW, TableScope, seed_from_fqn


SMALL_TABLE_FACTOR = 10  # row_count < n * factor -> direct DISTINCT path
SAMPLE_RATE_MULTIPLIER = 10  # over-sample to compensate for the DISTINCT filter


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

    database, schema, table = fqn.split(".")
    quoted_col = stats._quote_ident(column)
    seed = seed_from_fqn(fqn, stats.SEED_MODULUS)
    source = stats._source(database, schema, table, scope, seed)
    estimate = _scoped_estimate(introspect.row_count_estimate(cursor, fqn), scope)

    if estimate <= 0 or estimate < n * SMALL_TABLE_FACTOR:
        return _distinct(cursor, source, quoted_col, n, seed)

    # TABLESAMPLE binds to one table reference, so only an unmaterialized narrowing needs
    # wrapping - a materialized scope is a plain table the draw binds to directly.
    wrapped = scope is not None and scope.narrows and scope.materialized is None
    narrowed = f"(SELECT * FROM {source}) s" if wrapped else source
    oversampled = (
        f"(SELECT {quoted_col} AS v FROM {narrowed} "
        f"TABLESAMPLE reservoir({int(n * SAMPLE_RATE_MULTIPLIER)} ROWS) REPEATABLE ({seed}) "
        f"WHERE {quoted_col} IS NOT NULL) sampled"
    )
    values = _distinct(cursor, oversampled, "v", n, seed)

    if _starved(scope, values, n):
        return _distinct(cursor, source, quoted_col, n, seed)

    return values


def _starved(scope: TableScope | None, values: list[Any], n: int) -> bool:
    """Whether the draw came back too thin to infer from, and re-reading would help - only a
    predicate can starve it, since a fraction sizes the draw to the rate it asked for.
    """

    if scope is None or not scope.filter:
        return False

    return len(values) < min(n, MIN_SAMPLE_DRAW)


def _scoped_estimate(estimate: int, scope: TableScope | None) -> float:
    """Rows the scoped read covers: a fraction scales the estimate, a predicate cannot.
    A missing catalog entry (-1) routes to the direct path.
    """

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
        ORDER BY MD5('{seed}' || CAST(v AS VARCHAR))
        LIMIT {int(n)}
        """,
    ).fetchall()

    return [r[0] for r in rows]
