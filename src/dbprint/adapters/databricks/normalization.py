"""Normalized distinct count, in-database (SPEC 2.2.3, 2.2.4) - trim then case-fold, on the
same seed Phase A's `cardinality` uses so a sampled scope draws identical rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import stats
from .connection import exec_query
from ..base import TableScope, seed_from_fqn
from ..identifiers import Identity
from ..sql_layout import indented


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
    cn = stats._qualified(column)
    normalized = f"LOWER(TRIM(CAST({cn} AS STRING)))"
    source = stats._source(quoted_table, scope, seed_from_fqn(identity.fqn, stats.SEED_MODULUS))

    row = exec_query(
        cursor,
        f"""
        SELECT
          COUNT(DISTINCT {normalized}) AS n
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
        """,
    ).fetchone()

    return int(row[0]) if row and row[0] is not None else 0
