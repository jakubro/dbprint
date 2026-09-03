"""Normalized distinct count, in-database (SPEC 2.2.3, 2.2.4) - trim then case-fold, on the
same seed Phase A's `cardinality` uses so a sampled scope draws identical rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import stats
from .connection import exec_query
from .identity import Identity
from ..base import TableScope, seed_from_fqn


if TYPE_CHECKING:
    from .connection import Cursor


def compute_normalized_cardinality(
    cursor: Cursor,
    identity: Identity,
    column: str,
    scope: TableScope | None = None,
) -> int:
    """The distinct count of `column` once trimmed and case-folded (SPEC 2.2.4)."""

    quoted_table = identity.quoted()
    cn = identity.quoted_column(column)
    normalized = f"LOWER(TRIM(CAST({cn} AS STRING)))"
    seed = seed_from_fqn(identity.dotted().lower(), stats.SEED_MODULUS)
    source = stats._source(quoted_table, scope, seed)

    row = exec_query(
        cursor,
        f"SELECT COUNT(DISTINCT {normalized}) AS n FROM {source} WHERE {cn} IS NOT NULL",
    ).fetchone()

    return int(row[0]) if row and row[0] is not None else 0
