"""Distinct-value sampling for `looks_like` detection - BigQuery's `RAND()` takes no seed, so the
oversample draw orders by the same seeded hash the final distinct list ships under (SPEC 4.1.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import stats
from .connection import exec_query
from .identity import Identity
from .introspect import row_count_hint
from ..base import MIN_SAMPLE_DRAW, TableScope, seed_from_fqn


if TYPE_CHECKING:
    from .connection import Cursor


SAMPLE_RATE_MULTIPLIER = 10  # over-sample to compensate for the DISTINCT filter
SMALL_TABLE_FACTOR = 10  # row_count < n * factor -> direct DISTINCT path


def sample_distinct(
    cursor: Cursor,
    project: str,
    identity: Identity,
    column: str,
    n: int,
    scope: TableScope | None = None,
) -> list[Any]:
    """Return up to n distinct non-null sampled values for the column - scoped like every other
    statistic, and a predicate-starved draw is re-taken directly.
    """

    quoted = identity.quoted()
    cn = identity.quoted_column(column)
    seed = seed_from_fqn(identity.dotted().lower(), stats.SEED_MODULUS)
    source = stats._source(quoted, scope, seed)
    estimate = _scoped_estimate(row_count_hint(cursor, project, identity), scope)

    if estimate <= 0 or estimate < n * SMALL_TABLE_FACTOR:
        return _distinct(cursor, source, cn, n, seed)

    oversampled = (
        f"(SELECT {cn} AS v FROM {source} WHERE {cn} IS NOT NULL "
        f"ORDER BY {_seed_hash_order(cn, seed)} "
        f"LIMIT {int(n * SAMPLE_RATE_MULTIPLIER)}) sampled"
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


def _scoped_estimate(estimate: int | None, scope: TableScope | None) -> float:
    """Rows the scoped read covers: a fraction scales the catalog estimate, a predicate cannot.
    A missing catalog estimate (`None`) routes to the direct path.
    """

    base = float(estimate) if estimate is not None else -1.0

    if scope is None or scope.sample is None:
        return base

    return base * scope.sample


def _seed_hash_order(quoted_col: str, seed: int) -> str:
    """A fixed permutation of `quoted_col`'s values under `seed` (SPEC 4.1.2) - embedded as a
    literal, since a caller nests this in its own query text and `seed` is never external input.
    """

    return f"TO_HEX(MD5(CONCAT('{seed}', CAST({quoted_col} AS STRING))))"


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
        ORDER BY {_seed_hash_order("v", seed)}
        LIMIT %s
        """,
        (n,),
    ).fetchall()

    return [r[0] for r in rows]
