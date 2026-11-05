"""Distinct value sampling for `looks_like` detection - the oversample orders the rows by the same
registered draw a sampled scope filters on, so it reproduces exactly from the table's seed.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from . import introspect, stats
from .connection import DIALECT, Cursor, exec_query
from .rendering import render_operand, render_text
from .. import statements
from ..base import TableScope
from ..identifiers import SOURCE_ALIAS, Identity
from ..sql_layout import derived, indented


def sample_distinct(
    cursor: Cursor,
    identity: Identity,
    column: str,
    n: int,
    scope: TableScope | None = None,
    sql_type: str | None = None,
) -> list[Any]:
    """Return up to n distinct non-null sampled values for the column - scoped like every other
    statistic, and a predicate-starved draw is re-taken directly.
    """

    quoted_col = identity.source_column(column)
    seed = statements.table_seed(identity)
    source = stats.table_source(identity, scope)
    oversampled = derived(
        f"""
        SELECT
          {quoted_col} AS v
        FROM
          {indented(source, 10)}
        WHERE
          {quoted_col} IS NOT NULL
        ORDER BY
          DBPRINT_DRAW({seed}, {stats.row_key(identity, SOURCE_ALIAS)})
        LIMIT {int(n * statements.SAMPLE_RATE_MULTIPLIER)}
        """,
        "ovs",
    )

    return statements.sample_distinct(
        scope,
        n,
        statements.scoped_estimate(introspect.row_count_estimate(cursor, identity), scope),
        direct=lambda: _distinct(
            partial(exec_query, cursor),
            source,
            quoted_col,
            n,
            seed,
            sql_type,
        ),
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
