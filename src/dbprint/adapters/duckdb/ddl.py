"""DDL extraction from duckdb's own catalog (SPEC 2.1) - `duckdb_tables()`/`duckdb_views()`
carry the `CREATE` statement verbatim and `duckdb_indexes()` each index's, so there is no
`GET_DDL`/`pg_dump` step.
"""

from __future__ import annotations

from .connection import Cursor, exec_query
from ..identifiers import Identity
from ..sql_layout import trimmed_lines


def extract_ddl(cursor: Cursor, identity: Identity) -> str:
    """Return the object's own `CREATE` statement, then its `CREATE INDEX` statements in
    `index_name` order, one blank line apart, post-normalization (SPEC 2.1.5).
    """

    database, schema, table = identity.parts
    row = exec_query(
        cursor,
        """
        SELECT
          tbl.sql
        FROM
          DUCKDB_TABLES() tbl
        WHERE
          tbl.database_name = ?
          AND tbl.schema_name = ?
          AND tbl.table_name = ?

        UNION ALL

        SELECT
          vew.sql
        FROM
          DUCKDB_VIEWS() vew
        WHERE
          vew.database_name = ?
          AND vew.schema_name = ?
          AND vew.view_name = ?
        """,
        (database, schema, table, database, schema, table),
    ).fetchone()

    if row is None or not row[0]:
        raise ValueError(f"no DDL available for {identity.fqn!r}; not found in catalog")

    indexes = exec_query(
        cursor,
        """
        SELECT
          idx.index_name,
          idx.sql
        FROM
          DUCKDB_INDEXES() idx
        WHERE
          idx.database_name = ?
          AND idx.schema_name = ?
          AND idx.table_name = ?
          AND idx.sql IS NOT NULL
        """,
        (database, schema, table),
    ).fetchall()
    statements = [row[0], *(sql for _, sql in sorted(indexes))]

    return normalize("\n\n".join(statement.rstrip() for statement in statements))


def normalize(raw: str) -> str:
    """Strip trailing whitespace per line and ensure terminal newline."""

    return trimmed_lines(raw)
