"""Normalized distinct count, in-database (SPEC 2.2.3, 2.2.4) - trim then case-fold, on the
same seed Phase A's `cardinality` uses so a sampled scope draws identical rows.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from . import stats
from .connection import DIALECT, exec_query
from .rendering import render_text
from .. import statements
from ..identifiers import Identity


if TYPE_CHECKING:
    import psycopg

    from dbprint.adapters.base import TableScope


def compute_normalized_cardinality(
    conn: psycopg.Connection,
    identity: Identity,
    column: str,
    sql_type: str,
    scope: TableScope | None = None,
) -> int:
    """The distinct count of `column` once trimmed and case-folded (SPEC 2.2.4)."""

    cn = identity.source_column(column)

    return statements.normalized_cardinality(
        partial(exec_query, conn),
        DIALECT,
        stats.table_source(identity, scope),
        cn,
        render_text(cn, sql_type),
    )
