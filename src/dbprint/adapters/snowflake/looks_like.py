"""Distinct value sampling for `looks_like` detection. See ARCHITECTURE.md 2.

Below `n * SMALL_TABLE_FACTOR` scoped rows the DISTINCT scan is cheap; above that the
adapter switches to `SAMPLE`, sized off the catalog row count, never `COUNT(*)`. Two
sample clauses cannot chain on one table reference, so a narrowing scope is wrapped in a
subquery first. The fixed-size draw is unseedable, so coherence with the rest of the
profile is population-level, not row-level; `_distinct`'s hash ordering of the distinct
set (SPEC 4.1.2) is seeded and reproducible.
"""

from __future__ import annotations

from typing import Any

from . import introspect, stats
from .connection import Cursor, exec_query
from .identity import Identity
from ..base import MIN_SAMPLE_DRAW, TableScope, seed_from_fqn


SMALL_TABLE_FACTOR = 10  # row_count < n * factor -> direct DISTINCT path
SAMPLE_RATE_MULTIPLIER = 10  # over-sample to compensate for the DISTINCT filter


def sample_distinct(
    cursor: Cursor,
    identity: Identity,
    column: str,
    n: int,
    scope: TableScope | None = None,
) -> list[Any]:
    """Return up to n distinct non-null sampled values for the column.

    Scoped like every other statistic; a predicate-starved draw is re-taken directly.
    """

    cn = identity.quoted_column(column)
    seed = seed_from_fqn(identity.dotted().lower(), stats.SEED_MODULUS)
    source = stats._source(identity, scope, seed)
    estimate = _scoped_estimate(introspect.row_count_estimate(cursor, identity), scope)

    if estimate <= 0 or estimate < n * SMALL_TABLE_FACTOR:
        return _distinct(cursor, source, cn, n, seed)

    # A materialized scope is a plain table the draw binds to directly; only an
    # unmaterialized narrowing has to be wrapped first.
    wrapped = scope is not None and scope.narrows and scope.materialized is None
    narrowed = f"(SELECT * FROM {source})" if wrapped else source
    # The SAMPLE ROW draw stays row-random and unseedable (SPEC 4.1.2 names the
    # frequency-weighting this costs); only the final `_distinct` step is hash-ordered.
    oversampled = (
        f"(SELECT {cn} AS v FROM {narrowed} SAMPLE ROW ({int(n * SAMPLE_RATE_MULTIPLIER)} ROWS) "
        f"WHERE {cn} IS NOT NULL) sampled"
    )
    values = _distinct(cursor, oversampled, "v", n, seed)

    if _starved(scope, values, n):
        return _distinct(cursor, source, cn, n, seed)

    return values


def _starved(scope: TableScope | None, values: list[Any], n: int) -> bool:
    """Whether the draw came back too thin to infer from, and re-reading would help.

    Only a predicate can starve it - a fraction sizes the draw to the rate it asked for.
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
    """Up to n distinct non-null values of the column from one source expression.

    Ordered by a hash of the value (SPEC 4.1.2): a fixed permutation of the distinct set,
    independent of frequency and storage order, reproducible under the table's own seed.
    The seed and the row limit are interpolated, not bound - both are internally derived
    integers, never user input.
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
