"""Normalized distinct count, in-database (SPEC 2.2.3, 2.2.4) - trim then case-fold, the one
normalization SPEC 2.2.4 defines, so two producers reading one column agree.
"""

from __future__ import annotations

from functools import partial

from . import stats
from .connection import DIALECT, Cursor, exec_query
from .rendering import render_text
from .. import statements
from ..base import TableScope
from ..identifiers import Identity


def compute_normalized_cardinality(
    cursor: Cursor,
    identity: Identity,
    column: str,
    sql_type: str,
    scope: TableScope | None = None,
) -> int:
    """The distinct count of `column` once trimmed and case-folded (SPEC 2.2.4)."""

    cn = identity.source_column(column)

    return statements.normalized_cardinality(
        partial(exec_query, cursor),
        DIALECT,
        stats.table_source(identity, scope),
        cn,
        render_text(cn, sql_type),
    )
