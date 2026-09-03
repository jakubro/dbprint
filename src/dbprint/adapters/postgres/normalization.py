"""Normalized distinct count, in-database (SPEC 2.2.3, 2.2.4) - trim then case-fold, on the
same seed Phase A's `cardinality` uses so a sampled scope draws identical rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import stats
from .connection import exec_query
from .introspect import resolve_column
from ..base import seed_from_fqn


if TYPE_CHECKING:
    import psycopg

    from dbprint.adapters.base import TableScope


def compute_normalized_cardinality(
    conn: psycopg.Connection,
    fqn: str,
    column: str,
    scope: TableScope | None = None,
) -> int:
    """The distinct count of `column` once trimmed and case-folded (SPEC 2.2.4)."""

    schema, _, table = fqn.partition(".")
    quoted_table = stats._quote_qualified(schema, table)
    cn = stats._quote_ident(resolve_column(conn, fqn, column))
    normalized = f"LOWER(TRIM(CAST({cn} AS text)))"
    source = stats._source(quoted_table, scope, seed_from_fqn(fqn, stats.SEED_MODULUS))

    row = exec_query(
        conn,
        f"SELECT COUNT(DISTINCT {normalized}) AS n FROM {source} WHERE {cn} IS NOT NULL",
    ).fetchone()

    return int(row[0]) if row and row[0] is not None else 0
