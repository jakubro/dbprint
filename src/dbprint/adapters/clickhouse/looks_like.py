"""Distinct-value sampling for `looks_like` detection - the oversample uses native `SAMPLE`
and degrades to a direct scan, needing no cross-statement coherence to refuse over.
"""

from __future__ import annotations

from typing import Any

from . import introspect, stats
from .connection import Cursor, exec_query
from .rendering import render_operand, render_text
from ..base import TableScope, seed_from_fqn
from ..errors import QueryFailed
from ..identifiers import Identity
from ..sql_layout import derived, indented


SMALL_TABLE_FACTOR = 10  # row_count < n * factor -> direct DISTINCT path
SAMPLE_RATE_MULTIPLIER = 10  # over-sample to compensate for the DISTINCT filter


def sample_distinct(
    cursor: Cursor,
    identity: Identity,
    column: str,
    n: int,
    scope: TableScope | None = None,
    sql_type: str | None = None,
) -> list[Any]:
    """Return up to n distinct non-null sampled values for the column."""

    cn = identity.source_column(column)
    source = stats._source(identity, scope)
    seed = seed_from_fqn(identity.fqn, 2**31)

    if scope is None or not scope.narrows:
        estimate = introspect.estimate_row_count(cursor, identity)

        if estimate >= n * SMALL_TABLE_FACTOR:
            oversampled = _try_oversample(cursor, source, cn, n, seed, sql_type)

            if oversampled is not None:
                return oversampled

    return _distinct(cursor, source, cn, n, seed, sql_type)


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
          {quoted_table} SAMPLE {int(n * SAMPLE_RATE_MULTIPLIER)}
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
    """Up to n distinct non-null values of the column from one source expression, ordered by a
    hash of the seed and the value (SPEC 4.1.2) - a fixed, reproducible permutation.
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
          halfMD5(concat(%s, toString(drw.v)))
        LIMIT %s
        """,
        (str(seed), n),
    ).fetchall()

    return [r[0] for r in rows]
