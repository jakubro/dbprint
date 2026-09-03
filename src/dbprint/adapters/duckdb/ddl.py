"""DDL extraction from duckdb's own catalog (SPEC 2.1) - `duckdb_tables()`/`duckdb_views()`
carry the `CREATE` statement verbatim, so there is no `GET_DDL`/`pg_dump` step.
"""

from __future__ import annotations

from .connection import Cursor, exec_query


def extract_ddl(cursor: Cursor, fqn: str) -> str:
    """Return the object's own `CREATE` statement, post-normalization."""

    database, schema, table = fqn.split(".")
    row = exec_query(
        cursor,
        """
        SELECT sql FROM duckdb_tables()
        WHERE database_name = ? AND schema_name = ? AND table_name = ?
        UNION ALL
        SELECT sql FROM duckdb_views()
        WHERE database_name = ? AND schema_name = ? AND view_name = ?
        """,
        (database, schema, table, database, schema, table),
    ).fetchone()

    if row is None or not row[0]:
        raise ValueError(f"no DDL available for {fqn!r}; not found in catalog")

    return normalize(row[0])


def normalize(raw: str) -> str:
    """Strip trailing whitespace per line and ensure terminal newline."""

    lines = [line.rstrip() for line in raw.splitlines()]
    text = "\n".join(lines).strip("\n")

    return text + "\n" if text else ""
