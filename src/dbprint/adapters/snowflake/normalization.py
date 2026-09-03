"""Normalized distinct count, in-database (SPEC 2.2.3, 2.2.4) - trim then case-fold, on the
same seed Phase A's `cardinality` uses so a sampled scope draws identical rows.
"""

from __future__ import annotations

from . import stats
from .connection import Cursor, exec_query
from .identity import Identity
from ..base import TableScope, seed_from_fqn


def compute_normalized_cardinality(
    cursor: Cursor,
    identity: Identity,
    column: str,
    scope: TableScope | None = None,
) -> int:
    """The distinct count of `column` once trimmed and case-folded (SPEC 2.2.4)."""

    cn = identity.quoted_column(column)
    normalized = f"LOWER(TRIM(TO_VARCHAR({cn})))"
    seed = seed_from_fqn(identity.dotted().lower(), stats.SEED_MODULUS)
    source = stats._source(identity, scope, seed)

    row = exec_query(
        cursor,
        f"SELECT COUNT(DISTINCT {normalized}) AS n FROM {source} WHERE {cn} IS NOT NULL",
    ).fetchone()

    return int(row[0]) if row and row[0] is not None else 0
