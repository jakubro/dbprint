"""Distinct-value sampling for `looks_like` detection - BigQuery's `RAND()` takes no seed, so the
oversample draw orders by the same seeded hash the final distinct list ships under (SPEC 4.1.2).
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from . import stats
from .connection import DIALECT, exec_query
from .introspect import row_count_hint
from .rendering import render_operand, render_text
from .. import statements
from ..base import TableScope
from ..identifiers import Identity
from ..sql_layout import derived, indented


if TYPE_CHECKING:
    from .connection import Cursor


def sample_distinct(
    cursor: Cursor,
    project: str,
    identity: Identity,
    column: str,
    n: int,
    scope: TableScope | None = None,
    sql_type: str | None = None,
) -> list[Any]:
    """Return up to n distinct non-null sampled values for the column - scoped like every other
    statistic, and a predicate-starved draw is re-taken directly.
    """

    cn = identity.source_column(column)
    seed = statements.table_seed(identity)
    source = stats.table_source(identity, scope)

    oversampled = derived(
        f"""
        SELECT
          {cn} AS v
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
        ORDER BY
          {DIALECT.seed_hash.format(seed=f"'{seed}'", value=cn)}
        LIMIT {int(n * statements.SAMPLE_RATE_MULTIPLIER)}
        """,
        "ovs",
    )

    return statements.sample_distinct(
        scope,
        n,
        statements.scoped_estimate(row_count_hint(cursor, project, identity), scope),
        direct=lambda: _distinct(partial(exec_query, cursor), source, cn, n, seed, sql_type),
        draw=lambda: _distinct(
            partial(exec_query, cursor),
            oversampled,
            "ovs.v",
            n,
            seed,
            sql_type,
        ),
    )


_distinct = partial(
    statements.typed_distinct_values,
    dialect=DIALECT,
    render_text=render_text,
    render_operand=render_operand,
    unsupported=stats._is_unsupported,
)
