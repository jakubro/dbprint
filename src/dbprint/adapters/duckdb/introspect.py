"""duckdb's own catalog functions for structural metadata, one per intermediate type - every
identifier resolves case-insensitively, so `physical_name` is carried for the reader alone.
"""

from __future__ import annotations

from dbprint.spec.fqn import join as join_fqn
from .connection import DIALECT, Cursor, exec_query
from ..base import (
    ColumnMeta,
    CommentsMeta,
    ForeignKeyMeta,
    IndexMeta,
    PhysicalLayout,
    TableMeta,
    UniqueKeyMeta,
)
from ..identifiers import Identity, column_meta, fold, select_tables, table_meta, unquote


_Candidate = tuple[TableMeta, tuple[str, str, str]]


def list_tables(cursor: Cursor, include: list[str], exclude: list[str]) -> list[_Candidate]:
    """Enumerate tables and views in scope, filtered by selectors - the catalog functions
    exclude internal objects, and there is no materialized-view concept here.
    """

    candidates: list[_Candidate] = []

    for source, name_column, table_type in (
        ("DUCKDB_TABLES()", "table_name", "table"),
        ("DUCKDB_VIEWS()", "view_name", "view"),
    ):
        rows = exec_query(
            cursor,
            f"""
            SELECT
              obj.database_name,
              obj.schema_name,
              obj.{name_column}
            FROM
              {source} obj
            WHERE
              NOT obj.internal
            ORDER BY
              obj.database_name, obj.schema_name, obj.{name_column}
            """,
        ).fetchall()

        for database, schema, name in rows:
            physical = (database, schema, name)
            candidates.append((table_meta(physical, table_type), physical))

    selected = select_tables(candidates, include, exclude)

    return selected


def columns(cursor: Cursor, identity: Identity) -> list[ColumnMeta]:
    """Per-column structural metadata in ordinal order - `name` is lowercased for the map key
    (SPEC 2.2.1), and `collation` is always `None`, duckdb exposing no per-column surface.
    """

    database, schema, table = identity.parts
    rows = exec_query(
        cursor,
        """
        SELECT
          col.column_name,
          col.column_index,
          col.data_type,
          col.is_nullable,
          col.column_default
        FROM
          DUCKDB_COLUMNS() col
        WHERE
          col.database_name = ?
          AND col.schema_name = ?
          AND col.table_name = ?
        ORDER BY
          col.column_index
        """,
        (database, schema, table),
    ).fetchall()

    return [
        column_meta(
            name,
            sql_type=sql_type,
            nullable=nullable,
            default=default,
            ordinal=int(ordinal),
        )
        for name, ordinal, sql_type, nullable, default in rows
    ]


# duckdb's own comparison default: byte order over UTF-8, no locale awareness unless the
# optional ICU extension is loaded and `default_collation` set - neither happens here.
DEFAULT_COLLATION = "binary"


def default_collation(cursor: Cursor) -> str:
    """The connection's default comparison collation for a column with no explicit override."""

    row = exec_query(
        cursor,
        """
        SELECT
          stg.value
        FROM
          DUCKDB_SETTINGS() stg
        WHERE
          stg.name = 'default_collation'
        """,
    ).fetchone()
    value = row[0] if row else ""

    return value or DEFAULT_COLLATION


def relationships(cursor: Cursor, identity: Identity) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs; one entry per constraint (composite as arrays) - duckdb parses
    no action clause at all, so every edge is unconditionally `NO ACTION` on both sides.
    """

    database, schema, table = identity.parts
    rows = exec_query(
        cursor,
        """
        SELECT
          cns.constraint_name,
          cns.constraint_column_names,
          cns.referenced_table,
          cns.referenced_column_names
        FROM
          DUCKDB_CONSTRAINTS() cns
        WHERE
          cns.constraint_type = 'FOREIGN KEY'
          AND cns.database_name = ?
          AND cns.schema_name = ?
          AND cns.table_name = ?
        ORDER BY
          cns.constraint_name
        """,
        (database, schema, table),
    ).fetchall()

    return [
        ForeignKeyMeta(
            column=tuple(fold(name) for name in source_columns),
            target_table=join_fqn([fold(part) for part in (database, schema, target_table)]),
            target_column=tuple(fold(name) for name in target_columns),
            on_delete="NO ACTION",
            on_update="NO ACTION",
            constraint_name=name,
        )
        for name, source_columns, target_table, target_columns in rows
    ]


def indexes(cursor: Cursor, identity: Identity) -> list[IndexMeta]:
    """Secondary, non-unique indexes only (SPEC 2.6.7) - constraints and bare indexes live in
    separate catalog functions that never overlap, so no exclusion join is needed. An index
    lists its column members; one with none is not reported.
    """

    database, schema, table = identity.parts
    rows = exec_query(
        cursor,
        """
        SELECT
          idx.index_name,
          CAST(idx.expressions AS VARCHAR[]) AS members
        FROM
          DUCKDB_INDEXES() idx
        WHERE
          NOT idx.is_unique
          AND idx.database_name = ?
          AND idx.schema_name = ?
          AND idx.table_name = ?
        ORDER BY
          idx.index_name
        """,
        (database, schema, table),
    ).fetchall()

    out: list[IndexMeta] = []

    for name, members in rows:
        if columns := tuple(c for c in map(_member_column, members) if c is not None):
            out.append(IndexMeta(name=fold(name), columns=columns, unique=False, type="art"))

    return out


def unique_keys(cursor: Cursor, identity: Identity) -> list[UniqueKeyMeta]:
    """Declared-unique column groups: primary key, unique constraints, bare unique indexes.

    A unique index with any expression member declares no key (SPEC 2.2.12).
    """

    database, schema, table = identity.parts
    out: list[UniqueKeyMeta] = []

    constraint_rows = exec_query(
        cursor,
        """
        SELECT
          cns.constraint_type,
          cns.constraint_column_names
        FROM
          DUCKDB_CONSTRAINTS() cns
        WHERE
          cns.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
          AND cns.database_name = ?
          AND cns.schema_name = ?
          AND cns.table_name = ?
        ORDER BY
          cns.constraint_index
        """,
        (database, schema, table),
    ).fetchall()

    for constraint_type, column_names in constraint_rows:
        out.append(
            UniqueKeyMeta(
                columns=tuple(fold(name) for name in column_names),
                primary=constraint_type == "PRIMARY KEY",
            ),
        )

    index_rows = exec_query(
        cursor,
        """
        SELECT
          CAST(idx.expressions AS VARCHAR[]) AS members
        FROM
          DUCKDB_INDEXES() idx
        WHERE
          idx.is_unique
          AND NOT idx.is_primary
          AND idx.database_name = ?
          AND idx.schema_name = ?
          AND idx.table_name = ?
        ORDER BY
          idx.index_name
        """,
        (database, schema, table),
    ).fetchall()

    for (members,) in index_rows:
        columns = [c for c in map(_member_column, members) if c is not None]

        if len(columns) == len(members):
            out.append(UniqueKeyMeta(columns=tuple(columns), primary=False))

    return out


def physical_layout(cursor: Cursor, identity: Identity) -> PhysicalLayout | None:
    """duckdb has no declarative clustering/partitioning key for an ordinary table."""

    del cursor, identity

    return None


def view_dependencies(cursor: Cursor) -> None:
    """None unconditionally: `duckdb_dependencies()` misses a plain view's read of a table, so
    every view omits `depends_on` rather than publish a guess parsed out of DDL text.
    """

    del cursor


def comments(cursor: Cursor, identity: Identity) -> CommentsMeta:
    """Table comment + per-column comments from `duckdb_tables()`/`duckdb_columns()`."""

    database, schema, table = identity.parts
    table_row = exec_query(
        cursor,
        """
        SELECT
          tbl.comment
        FROM
          DUCKDB_TABLES() tbl
        WHERE
          tbl.database_name = ?
          AND tbl.schema_name = ?
          AND tbl.table_name = ?

        UNION ALL

        SELECT
          vew.comment
        FROM
          DUCKDB_VIEWS() vew
        WHERE
          vew.database_name = ?
          AND vew.schema_name = ?
          AND vew.view_name = ?
        """,
        (database, schema, table, database, schema, table),
    ).fetchone()

    col_rows = exec_query(
        cursor,
        """
        SELECT
          col.column_name,
          col.comment
        FROM
          DUCKDB_COLUMNS() col
        WHERE
          col.database_name = ?
          AND col.schema_name = ?
          AND col.table_name = ?
        """,
        (database, schema, table),
    ).fetchall()

    return CommentsMeta(
        table=table_row[0] if table_row else None,
        columns={fold(col_name): comment for col_name, comment in col_rows if comment is not None},
    )


def row_count_estimate(cursor: Cursor, identity: Identity) -> int:
    """The table's exact row count; -1 for a view or an unknown table, which is never queried
    (SPEC 2.2.15).

    `estimated_size` keeps counting deleted rows until a whole row group empties, while a
    `COUNT(1)` is answered from row-group counts and deletion markers, reading no column data.
    """

    database, schema, table = identity.parts
    row = exec_query(
        cursor,
        """
        SELECT
          1
        FROM
          DUCKDB_TABLES() tbl
        WHERE
          tbl.database_name = ?
          AND tbl.schema_name = ?
          AND tbl.table_name = ?
        """,
        (database, schema, table),
    ).fetchone()

    if not row:
        return -1

    return int(exec_query(cursor, f"SELECT COUNT(1) FROM {identity.quoted()} src").fetchone()[0])


def _member_column(member: str) -> str | None:
    # duckdb renders a column member as its identifier, quoted only where it must be, and
    # every other member wrapped in parentheses.
    return None if member.startswith("(") else fold(unquote(member, DIALECT))
