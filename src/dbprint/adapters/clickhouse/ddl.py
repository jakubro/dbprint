"""DDL extraction via `system.tables.create_table_query` - an ordinary catalog column, so no
per-object statement and no external binary.
"""

from __future__ import annotations

from .connection import Cursor, exec_query
from .introspect import FORCE_DISPLAY_MASKING
from ..identifiers import Identity
from ..sql_layout import trimmed_lines


def extract_ddl(cursor: Cursor, identity: Identity, *, hide_secrets: bool = False) -> str:
    """Return native-dialect DDL for the object, post-normalization.

    `hide_secrets` reads it with the server's masking forced on, for a session that turned it off.
    """

    row = exec_query(
        cursor,
        f"""
        SELECT
          tbl.create_table_query
        FROM
          system.tables tbl
        WHERE
          tbl.database = %s
          AND tbl.name = %s{FORCE_DISPLAY_MASKING if hide_secrets else ""}
        """,
        identity.parts,
    ).fetchone()

    if not row or not row[0]:
        raise ValueError(f"no DDL available for {identity.fqn!r}; not found in catalog")

    return normalize(str(row[0]))


def normalize(raw: str) -> str:
    """Strip trailing whitespace per line; one final newline."""

    return trimmed_lines(raw)
