"""Distinct value sampling for `looks_like` detection. See ARCHITECTURE.md 2.

Below `n * SMALL_TABLE_FACTOR` scoped rows the DISTINCT scan is cheap; above that the
adapter switches to `SAMPLE`, sized off the catalog row count, never `COUNT(*)`. Two
sample clauses cannot chain on one table reference, so a narrowing scope is wrapped in a
subquery first. The fixed-size draw is unseedable, so coherence with the rest of the
profile is population-level, not row-level; `_distinct`'s hash ordering of the distinct
set (SPEC 4.1.2) is seeded and reproducible.
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
    """Return up to n distinct non-null sampled values for the column.

    Scoped like every other statistic; a predicate-starved draw is re-taken directly.
    """

    cn = identity.source_column(column)
    seed = statements.table_seed(identity)
    source = stats.table_source(identity, scope)

    # A materialized scope is a plain table the draw binds to directly; only an
    # unmaterialized narrowing has to be wrapped first.
    wrapped = scope is not None and scope.narrows and scope.materialized is None
    narrowed = derived(f"SELECT src.* FROM {source}", SOURCE_ALIAS) if wrapped else source
    # The SAMPLE ROW draw stays row-random and unseedable (SPEC 4.1.2 names the
    # frequency-weighting this costs); only the final `_distinct` step is hash-ordered.
    oversampled = derived(
        f"""
        SELECT
          {cn} AS v
        FROM
          {indented(narrowed, 10)} SAMPLE ROW ({int(n * statements.SAMPLE_RATE_MULTIPLIER)} ROWS)
        WHERE
          {cn} IS NOT NULL
        """,
        "ovs",
    )

    return statements.sample_distinct(
        scope,
        n,
        statements.scoped_estimate(introspect.row_count_estimate(cursor, identity), scope),
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
