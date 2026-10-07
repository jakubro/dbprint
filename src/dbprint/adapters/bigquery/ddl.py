"""`INFORMATION_SCHEMA.TABLES.ddl` extraction - an ordinary column, no per-object command, so
this costs one row of a statement the catalog pre-pass already issues.
"""

from __future__ import annotations

from .connection import DIALECT, Cursor, exec_query
from ..identifiers import Identity, quote
from ..sql_layout import trimmed_lines


def extract_ddl(cursor: Cursor, project: str, identity: Identity) -> str:
    """Return the vendor's own recreate-DDL text for the object."""

    row = exec_query(
        cursor,
        f"""
        SELECT
          tbl.ddl
        FROM
          {quote(project, DIALECT)}.{quote(identity.parts[0], DIALECT)}.INFORMATION_SCHEMA.TABLES tbl
        WHERE
          tbl.table_name = %s
        """,
        (identity.table,),
    ).fetchone()

    if not row or not row[0]:
        raise ValueError(f"no DDL available for {identity.fqn!r}; not found in catalog")

    return normalize(str(row[0]))


def normalize(raw: str) -> str:
    """Strip trailing whitespace per line and ensure a single terminal newline."""

    return trimmed_lines(raw)
