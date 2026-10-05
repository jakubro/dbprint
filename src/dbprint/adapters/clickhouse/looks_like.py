"""Distinct-value sampling for `looks_like` detection - the oversample uses native `SAMPLE`
and degrades to a direct scan, needing no cross-statement coherence to refuse over.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from . import introspect, stats
from .connection import DIALECT, Cursor, exec_query
from .rendering import render_operand, render_text
from .. import statements
from ..base import TableScope, is_string_like
from ..errors import QueryFailed
from ..identifiers import Identity
from ..sql_layout import derived


def sample_distinct(
    cursor: Cursor,
    identity: Identity,
    column: str,
    n: int,
    scope: TableScope | None = None,
    sql_type: str | None = None,
) -> list[Any]:
    """Return up to n distinct non-null sampled values for the column.

    A narrowed read never draws - `SAMPLE` binds to the bare table - so no predicate can starve it.
    """

    cn = identity.source_column(column)
    source = stats._source(identity, scope)
    seed = statements.table_seed(identity)
    narrows = scope is not None and scope.narrows

    return statements.sample_distinct(
        scope,
        n,
        -1.0 if narrows else introspect.estimate_row_count(cursor, identity),
        direct=lambda: _distinct(cursor, source, cn, n, seed, sql_type),
        draw=lambda: _try_oversample(cursor, source, cn, n, seed, sql_type),
    )


def _try_oversample(
    cursor: Cursor,
    quoted_table: str,
    cn: str,
    n: int,
    seed: int,
    sql_type: str | None,
) -> list[Any] | None:
    """A fixed-size `SAMPLE` draw, or None when the table declares no sampling key."""

    oversampled = derived(
        f"""
        SELECT
          {cn} AS v
        FROM
          {quoted_table} SAMPLE {int(n * statements.SAMPLE_RATE_MULTIPLIER)}
        WHERE
          {cn} IS NOT NULL
        """,
        "ovs",
    )

    try:
        return _distinct(cursor, oversampled, "ovs.v", n, seed, sql_type)
    except QueryFailed:
        return None


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
