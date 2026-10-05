"""Distinct-value sampling for `looks_like` detection - the oversample uses `ORDER BY
rand(seed) LIMIT`, `TABLESAMPLE (n ROWS)` being documented as a `LIMIT`, not a random draw.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from . import stats
from .connection import DIALECT, exec_query
from .introspect import estimate_row_count
from .rendering import render_operand, render_text
from .. import statements
from ..base import TableScope, is_string_like
from ..identifiers import Identity
from ..sql_layout import derived, indented


if TYPE_CHECKING:
    from .connection import Cursor


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

    quoted = identity.quoted()
    cn = stats._qualified(column)
    seed = statements.table_seed(identity)
    source = stats._source(quoted, scope, seed)

    oversampled = derived(
        f"""
        SELECT
          {cn} AS v
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
        ORDER BY
          RAND({seed})
        LIMIT {int(n * statements.SAMPLE_RATE_MULTIPLIER)}
        """,
        "ovs",
    )

    return statements.sample_distinct(
        scope,
        n,
        statements.scoped_estimate(estimate_row_count(cursor, identity), scope),
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
