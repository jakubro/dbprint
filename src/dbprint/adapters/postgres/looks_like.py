"""TABLESAMPLE-based distinct value sampling for `looks_like` detection. See ARCHITECTURE.md 2.

Below `n * SMALL_TABLE_FACTOR` scoped rows the read is direct; above that it draws its own
TABLESAMPLE sub-sample composed with the scope. Either way the distinct set is ordered by a
hash of the value (SPEC 4.1.2) rather than storage order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import stats
from .connection import exec_query
from .identity import Identity
from .introspect import reltuples_estimate, resolve_column
from ..base import MIN_SAMPLE_DRAW, TableScope


if TYPE_CHECKING:
    import psycopg


SAMPLE_RATE_MULTIPLIER = 10  # over-sample to compensate for the DISTINCT filter
SMALL_TABLE_FACTOR = 10  # row_count < n * factor -> direct DISTINCT path


def sample_distinct(
    conn: psycopg.Connection,
    identity: Identity,
    column: str,
    n: int,
    scope: TableScope | None = None,
) -> list[Any]:
    """Return up to n distinct non-null sampled values for the column.

    `column` is the artifact's lowercase map key (SPEC 2.2.1), resolved to its physical
    spelling before quoting, since a Postgres column's catalog case can differ. A
    predicate-starved draw is re-taken over the scoped set directly.
    """

    quoted = identity.quoted()
    cn = stats._quote_ident(resolve_column(conn, identity, column))
    seed = stats._seed(identity)
    scoped = stats._source(quoted, scope, seed)
    estimate = _scoped_estimate(reltuples_estimate(conn, identity), scope)

    if estimate <= 0 or estimate < n * SMALL_TABLE_FACTOR:
        return _distinct(conn, scoped, cn, n, seed)

    fraction = min(1.0, max(0.0001, (n * SAMPLE_RATE_MULTIPLIER) / estimate))
    source, conjunct = _sub_drawn_source(quoted, scope, fraction, seed)
    values = _distinct(conn, source, cn, n, seed, conjunct)

    if _starved(scope, values, n):
        return _distinct(conn, scoped, cn, n, seed)

    return values


def _scoped_estimate(estimate: float, scope: TableScope | None) -> float:
    """Rows the scoped read covers: a fraction scales the planner estimate, a predicate cannot."""

    if scope is None or scope.sample is None:
        return estimate

    return estimate * scope.sample


def _sub_drawn_source(
    quoted_fqn: str,
    scope: TableScope | None,
    fraction: float,
    seed: int,
) -> tuple[str, str]:
    """Scoped source carrying this module's own draw, plus any extra conjunct.

    A materialized scope already holds the drawn rows, so the draw attaches at its own
    rate; composing against the scope's fraction would apply it twice. A filtering scope
    wraps the table in a subquery, where TABLESAMPLE cannot attach, so the draw becomes a
    `random()` predicate. Otherwise the two fractions collapse into one rate. The seed is
    the table's own, so a smaller composed rate selects a subset of the same rows.
    """

    if scope is not None and scope.materialized is not None:
        drawn = stats._source(
            stats._quote_ident(scope.materialized),
            TableScope(sample=min(1.0, fraction)),
            seed,
        )

        return drawn, ""

    if scope is not None and scope.filter:
        return stats._source(quoted_fqn, scope, seed), f" AND random() < {fraction}"

    composed = fraction * scope.sample if scope is not None and scope.sample else fraction

    return stats._source(quoted_fqn, TableScope(sample=min(1.0, composed)), seed), ""


def _starved(scope: TableScope | None, values: list[Any], n: int) -> bool:
    """Whether the draw came back too thin to infer from, and re-reading would help.

    Only a predicate can starve it - a fraction sizes the draw to the rate it asked for.
    """

    if scope is None or not scope.filter:
        return False

    return len(values) < min(n, MIN_SAMPLE_DRAW)


def _distinct(
    conn: psycopg.Connection,
    source: str,
    quoted_col: str,
    n: int,
    seed: int,
    conjunct: str = "",
) -> list[Any]:
    """Up to n distinct non-null values of the column from one source expression.

    Ordered by a hash of the value (SPEC 4.1.2): a fixed permutation of the distinct set,
    independent of frequency and storage order, reproducible under the table's own seed.
    The subquery keeps the outer level DISTINCT-free, since Postgres requires an ORDER BY
    expression on a `SELECT DISTINCT` to appear in the select list.
    """

    rows = exec_query(
        conn,
        f"""
        SELECT v FROM (
            SELECT DISTINCT {quoted_col} AS v
            FROM {source}
            WHERE {quoted_col} IS NOT NULL{conjunct}
        ) t
        ORDER BY MD5(%s || CAST(v AS VARCHAR))
        LIMIT %s
        """,
        (str(seed), n),
    ).fetchall()

    return [r[0] for r in rows]
