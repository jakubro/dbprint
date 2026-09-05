"""Normalized distinct count, in-database (SPEC 2.2.3, 2.2.4) - trim then case-fold, the one
normalization SPEC 2.2.4 defines, so two producers reading one column agree.
"""

from __future__ import annotations

from . import stats
from .connection import Cursor, exec_query
from .identity import Identity
from ..base import TableScope


def compute_normalized_cardinality(
    cursor: Cursor,
    identity: Identity,
    column: str,
    scope: TableScope | None = None,
) -> int:
    """The distinct count of `column` once trimmed and case-folded (SPEC 2.2.4)."""

    cn = identity.quoted_column(column)
    source = stats._source(identity, scope)
    normalized = f"lowerUTF8(trimBoth(toString({cn})))"

    row = exec_query(
        cursor,
        f"SELECT uniqExact({normalized}) FROM {source} WHERE {cn} IS NOT NULL",
    ).fetchone()

    return int(row[0]) if row and row[0] is not None else 0
