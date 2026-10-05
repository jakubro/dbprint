"""Distinct value sampling for `looks_like` detection - `reservoir(...) REPEATABLE (...)`
reproduces exactly, so coherence with the rest of a sampled profile is row-level here.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from . import introspect, stats
from .connection import DIALECT, Cursor, exec_query
from .rendering import render_operand, render_text
from .. import statements
from ..base import TableScope, is_string_like
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

    quoted_col = stats._qualified(column)
    seed = statements.table_seed(identity)
    source = stats._source(identity.quoted(), scope, seed)

    # TABLESAMPLE binds to one table reference, so only an unmaterialized narrowing needs
    # wrapping - a materialized scope is a plain table the draw binds to directly.
    wrapped = scope is not None and scope.narrows and scope.materialized is None
    narrowed = derived(f"SELECT src.* FROM {source}", SOURCE_ALIAS) if wrapped else source
    oversampled = derived(
        f"""
        SELECT
          {quoted_col} AS v
        FROM
          {indented(narrowed, 10)}
          TABLESAMPLE RESERVOIR({int(n * statements.SAMPLE_RATE_MULTIPLIER)} ROWS) REPEATABLE ({seed})
        WHERE
          {quoted_col} IS NOT NULL
        """,
        "ovs",
    )

    return statements.sample_distinct(
        scope,
        n,
        statements.scoped_estimate(introspect.row_count_estimate(cursor, identity), scope),
        direct=lambda: _distinct(cursor, source, quoted_col, n, seed, sql_type),
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
