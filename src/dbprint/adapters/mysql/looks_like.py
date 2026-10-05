"""Distinct-value sampling for `looks_like` detection. See ARCHITECTURE.md 2.

Below `n * SMALL_TABLE_FACTOR` scoped rows it reads directly; above that it over-samples
via `ORDER BY RAND()` then dedupes, MySQL having no TABLESAMPLE. Either way `_distinct`
orders the distinct set by a hash of the value (SPEC 4.1.2) rather than storage order.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from . import stats
from .connection import DIALECT, Cursor, exec_query
from .introspect import table_rows_estimate
from .rendering import render_operand, render_text
from .. import statements
from ..base import TableScope, is_string_like
from ..identifiers import Identity
from ..sql_layout import derived, indented


def sample_distinct(
    cursor: Cursor,
    identity: Identity,
    column: str,
    n: int,
    scope: TableScope | None = None,
    sql_type: str | None = None,
) -> list[Any]:
    """Return up to n distinct non-null sampled values for the column.

    Scoped like every other statistic; a predicate-starved draw is re-taken directly.
    """

    cn = stats._qualified(column)
    seed = statements.table_seed(identity)
    source = stats._source(identity.quoted(), scope, seed)

    # The over-sample stays row-random (SPEC 4.1.2 names the frequency-weighting
    # this costs); only the final `_distinct` step is hash-ordered.
    oversampled = derived(
        f"""
        SELECT
          {cn} AS v
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
        ORDER BY
          RAND()
        LIMIT {int(n * statements.SAMPLE_RATE_MULTIPLIER)}
        """,
        "ovs",
    )

    return statements.sample_distinct(
        scope,
        n,
        statements.scoped_estimate(table_rows_estimate(cursor, identity), scope),
        direct=lambda: _distinct(cursor, source, cn, n, seed, sql_type),
        draw=lambda: _distinct(cursor, oversampled, "ovs.v", n, seed, sql_type),
    )


def _distinct(
    cursor: Cursor,
    source: str,
    quoted_col: str,
    n: int,
    seed: int,
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
        partial(exec_query, cursor),
        DIALECT,
        source,
        quoted_col,
        selected,
        n,
        seed,
    )
