"""Distinct-value sampling for `looks_like` detection - the oversample uses `RANDOM()`,
Redshift having no `TABLESAMPLE`; the distinct set is hash-ordered either way (SPEC 4.1.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import stats
from .connection import exec_query
from .introspect import table_rows_estimate
from .rendering import render_operand, render_text
from ..base import MIN_SAMPLE_DRAW, TableScope
from ..identifiers import Identity
from ..sql_layout import derived, indented


if TYPE_CHECKING:
    from .connection import Cursor


SAMPLE_RATE_MULTIPLIER = 10  # over-sample to compensate for the DISTINCT filter
SMALL_TABLE_FACTOR = 10  # row_count < n * factor -> direct DISTINCT path


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

    cn = identity.source_column(column)
    seed = stats._seed(identity)
    source = stats._source(identity.quoted(), scope, seed)
    estimate = _scoped_estimate(table_rows_estimate(cursor, identity), scope)

    if estimate <= 0 or estimate < n * SMALL_TABLE_FACTOR:
        return _distinct(cursor, source, cn, n, seed, sql_type)

    oversampled = derived(
        f"""
        SELECT
          {cn} AS v
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
        ORDER BY
          RANDOM()
        LIMIT {int(n * SAMPLE_RATE_MULTIPLIER)}
        """,
        "ovs",
    )
    values = _distinct(cursor, oversampled, "ovs.v", n, seed, sql_type)

    if _starved(scope, values, n):
        return _distinct(cursor, source, cn, n, seed, sql_type)

    return values


def _starved(scope: TableScope | None, values: list[Any], n: int) -> bool:
    """Whether the draw came back too thin to infer from, and re-reading would help - only a
    predicate can starve it, since a fraction sizes the draw to the rate it asked for.
    """

    if scope is None or not scope.filter:
        return False

    return len(values) < min(n, MIN_SAMPLE_DRAW)


def _scoped_estimate(estimate: int, scope: TableScope | None) -> float:
    """Rows the scoped read covers: a fraction scales the catalog estimate, a predicate cannot."""

    if scope is None or scope.sample is None:
        return float(estimate)

    return estimate * scope.sample


def _distinct(
    cursor: Cursor,
    source: str,
    quoted_col: str,
    n: int,
    seed: int,
    sql_type: str | None,
) -> list[Any]:
    """Up to n distinct non-null values of the column from one source expression, ordered by a
    hash of the value (SPEC 4.1.2) - a fixed permutation, reproducible under the table's seed.
    """

    selected = (
        render_text(quoted_col, sql_type)
        if sql_type is not None and stats._is_string_like(sql_type)
        else render_operand(quoted_col, sql_type)
        if sql_type is not None
        else quoted_col
    )
    rows = exec_query(
        cursor,
        f"""
        SELECT
          drw.v
        FROM
          (
            SELECT DISTINCT
              {indented(selected, 14)} AS v
            FROM
              {indented(source, 14)}
            WHERE
              {quoted_col} IS NOT NULL
          ) drw
        ORDER BY
          MD5(%s || drw.v::VARCHAR)
        LIMIT %s
        """,
        (str(seed), n),
    ).fetchall()

    return [r[0] for r in rows]
