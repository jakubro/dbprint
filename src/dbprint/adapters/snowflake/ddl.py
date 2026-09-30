"""DDL extraction via GET_DDL + minimal normalization.

`extract_ddl` reads the object's type from INFORMATION_SCHEMA, asks `GET_DDL` for it, and
normalizes per SPEC 2.1.3 (minimal section).
"""

from __future__ import annotations

from .connection import DIALECT, Cursor, exec_query
from ..identifiers import Identity, quote


def extract_ddl(cursor: Cursor, identity: Identity) -> str:
    """Return native-dialect DDL for the object, post-normalization."""

    type_row = exec_query(
        cursor,
        f"""
        SELECT
          tbl.table_type
        FROM
          {quote(identity.parts[0], DIALECT)}.information_schema.tables tbl
        WHERE
          tbl.table_catalog = ?
          AND tbl.table_schema = ?
          AND tbl.table_name = ?
        """,
        identity.parts,
    ).fetchone()

    if type_row is None:
        raise ValueError(f"no DDL available for {identity.fqn!r}; not found in catalog")

    object_type = "VIEW" if "VIEW" in str(type_row[0]).upper() else "TABLE"

    # GET_DDL takes single-quoted constant arguments, so the name is inlined, not bound - and
    # quoted inside the string, since Snowflake upper-cases every unquoted part of it.
    statement = f"SELECT GET_DDL('{object_type}', {identity.name_string()})"
    ddl_row = exec_query(cursor, statement).fetchone()

    if ddl_row is None or not ddl_row[0]:
        raise ValueError(f"no DDL available for {identity.fqn!r}; GET_DDL returned nothing")

    return normalize(ddl_row[0])


def normalize(raw: str) -> str:
    """Strip trailing whitespace per line and ensure terminal newline."""

    lines = [line.rstrip() for line in raw.splitlines()]
    text = "\n".join(lines).strip("\n")

    return text + "\n" if text else ""
