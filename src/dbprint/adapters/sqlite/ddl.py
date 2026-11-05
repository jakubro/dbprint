"""DDL extraction from SQLite's own schema table (SPEC 2.1) - `sqlite_schema.sql` carries each
`CREATE` statement as written, so there is no `GET_DDL`/`pg_dump` step.
"""

from __future__ import annotations

import sqlite3

from .connection import DIALECT, Cursor, exec_query
from ..identifiers import Identity, quote
from ..sql_layout import trimmed_lines


def extract_ddl(cursor: Cursor, identity: Identity) -> str:
    """Return the object's `CREATE` statement, then its `CREATE INDEX` statements in `rowid`
    order, each ended with `;` - on a line of its own after a trailing `--` comment, closing a
    trailing open `/*` first - and one blank line apart (SPEC 2.1.3, 2.1.5).
    """

    schema, name = identity.parts
    catalog = f"{quote(schema, DIALECT)}.sqlite_schema"
    row = exec_query(
        cursor,
        f"""
        SELECT
          sch.sql
        FROM
          {catalog} sch
        WHERE
          sch.type IN ('table', 'view')
          AND sch.name = ?
        """,
        (name,),
    ).fetchone()

    if row is None or not row[0]:
        raise ValueError(f"no DDL available for {identity.fqn!r}; not found in catalog")

    indexes = exec_query(
        cursor,
        f"""
        SELECT
          sch.sql
        FROM
          {catalog} sch
        WHERE
          sch.type = 'index'
          AND sch.tbl_name = ?
          AND sch.sql IS NOT NULL
        ORDER BY
          sch.rowid
        """,
        (name,),
    ).fetchall()

    return trimmed_lines("\n\n".join(_terminated(sql.rstrip()) for (sql,) in [row, *indexes]))


def _terminated(sql: str) -> str:
    return next(
        (
            f"{sql}{end}"
            for end in (";", "\n;", " */;")
            if sqlite3.complete_statement(f"{sql}{end}")
        ),
        f"{sql};",
    )
