"""DDL extraction via GET_DDL + minimal normalization.

`extract_ddl` reads the object's type from INFORMATION_SCHEMA, asks `GET_DDL` for it, and
normalizes per SPEC 2.1.3 (minimal section).
"""

from __future__ import annotations

from .connection import Cursor, exec_query
from .identity import Identity


def extract_ddl(cursor: Cursor, identity: Identity) -> str:
    """Return native-dialect DDL for the object, post-normalization."""

    type_row = exec_query(
        cursor,
        """
        SELECT table_type
        FROM information_schema.tables
        WHERE table_catalog = ? AND table_schema = ? AND table_name = ?
        """,
        identity.parts,
    ).fetchone()

    if type_row is None:
        raise ValueError(f"no DDL available for {identity.dotted()!r}; not found in catalog")

    object_type = "VIEW" if "VIEW" in str(type_row[0]).upper() else "TABLE"

    # GET_DDL takes single-quoted constant arguments, so the name is inlined, not bound.
    object_name = identity.dotted().replace("'", "''")
    ddl_row = exec_query(cursor, f"SELECT GET_DDL('{object_type}', '{object_name}')").fetchone()

    if ddl_row is None or not ddl_row[0]:
        raise ValueError(f"no DDL available for {identity.dotted()!r}; GET_DDL returned nothing")

    return normalize(ddl_row[0])


def normalize(raw: str) -> str:
    """Strip trailing whitespace per line and ensure terminal newline."""

    lines = [line.rstrip() for line in raw.splitlines()]
    text = "\n".join(lines).strip("\n")

    return text + "\n" if text else ""
