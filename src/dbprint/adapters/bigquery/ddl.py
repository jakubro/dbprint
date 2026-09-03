"""`INFORMATION_SCHEMA.TABLES.ddl` extraction - an ordinary column, no per-object command, so
this costs one row of a statement the catalog pre-pass already issues.
"""

from __future__ import annotations

from .connection import Cursor, exec_query
from .identity import Identity


def extract_ddl(cursor: Cursor, project: str, identity: Identity) -> str:
    """Return the vendor's own recreate-DDL text for the object."""

    row = exec_query(
        cursor,
        f"""
        SELECT ddl
        FROM `{project}`.`{identity.dataset}`.INFORMATION_SCHEMA.TABLES
        WHERE table_name = %s
        """,
        (identity.table,),
    ).fetchone()

    if not row or not row[0]:
        raise ValueError(f"no DDL available for {identity.dotted()!r}; not found in catalog")

    return normalize(str(row[0]))


def normalize(raw: str) -> str:
    """Ensure a single terminal newline; the catalog's own text needs no further cleanup."""

    text = raw.strip("\n")

    return text + "\n" if text else ""
