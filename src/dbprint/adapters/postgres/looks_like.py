"""TABLESAMPLE-based distinct value sampling for `looks_like` detection. See ARCHITECTURE.md 2.

Below `n * SMALL_TABLE_FACTOR` scoped rows the read is direct; above that it draws its own
TABLESAMPLE sub-sample composed with the scope. Either way the distinct set is ordered by a
hash of the value (SPEC 4.1.2) rather than storage order.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from . import stats
from .connection import DIALECT, exec_query
from .introspect import reltuples_estimate
from .rendering import render_operand, render_text
from .. import statements
from ..base import TableScope, is_string_like
from ..identifiers import Identity, quote


if TYPE_CHECKING:
    import psycopg


def sample_distinct(
    conn: psycopg.Connection,
    identity: Identity,
    column: str,
    n: int,
    scope: TableScope | None = None,
    sql_type: str | None = None,
    *,
    foreign: bool = False,
) -> list[Any]:
    """Return up to n distinct non-null sampled values for the column.

    `column` is the artifact's lowercase map key (SPEC 2.2.1), resolved to its physical
    spelling before quoting, since a Postgres column's catalog case can differ. A
    predicate-starved draw is re-taken over the scoped set directly.
    """

    quoted = identity.quoted()
    cn = identity.source_column(column)
    seed = statements.table_seed(identity)
    scoped = stats.table_source(identity, scope)
    # TABLESAMPLE refuses a foreign table, so its values are always read directly.
    estimate = (
        -1.0 if foreign else statements.scoped_estimate(reltuples_estimate(conn, identity), scope)
    )

    def draw() -> list[Any]:
        fraction = min(1.0, max(0.0001, (n * statements.SAMPLE_RATE_MULTIPLIER) / estimate))
        source, conjunct = _sub_drawn_source(quoted, scope, fraction, seed)

        return _distinct(conn, source, cn, n, seed, conjunct, sql_type=sql_type)

    return statements.sample_distinct(
        scope,
        n,
        estimate,
        direct=lambda: _distinct(conn, scoped, cn, n, seed, sql_type=sql_type),
        draw=draw,
    )


def _sub_drawn_source(
    quoted_fqn: str,
    scope: TableScope | None,
    fraction: float,
    seed: int,
) -> tuple[str, str]:
    """Scoped source carrying this module's own draw, plus any extra conjunct.

    A copy draws at its own rate, a filter by `RANDOM()` conjunct, a sample at one composed rate.
    """

    if scope is not None and scope.materialized is not None:
        drawn = stats.source(
            quote(scope.materialized, DIALECT),
            TableScope(sample=min(1.0, fraction)),
            seed,
        )

        return drawn, ""

    if scope is not None and scope.filter:
        return stats.source(quoted_fqn, scope, seed), f" AND RANDOM() < {fraction}"

    composed = fraction * scope.sample if scope is not None and scope.sample else fraction

    return stats.source(quoted_fqn, TableScope(sample=min(1.0, composed)), seed), ""


def _distinct(
    conn: psycopg.Connection,
    source: str,
    quoted_col: str,
    n: int,
    seed: int,
    conjunct: str = "",
    *,
    sql_type: str | None,
) -> list[Any]:
    selected = (
        render_text(quoted_col, sql_type)
        if sql_type is not None and is_string_like(sql_type, stats._is_unsupported)
        else render_operand(quoted_col, sql_type)
        if sql_type is not None
        else quoted_col
    )

    return statements.distinct_values(
        partial(exec_query, conn),
        DIALECT,
        source,
        quoted_col,
        selected,
        n,
        seed,
        conjunct=conjunct,
    )
