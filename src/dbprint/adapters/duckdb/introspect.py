"""duckdb's own catalog functions for structural metadata, one per intermediate type - every
identifier resolves case-insensitively, so `physical_name` is carried for the reader alone.
"""

from __future__ import annotations

import re

from dbprint.config.selectors import expand
from .connection import Cursor, exec_query
from ..base import (
    ColumnMeta,
    CommentsMeta,
    ForeignKeyMeta,
    IndexMeta,
    PhysicalLayout,
    TableMeta,
    UniqueKeyMeta,
)


PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]*$")


class IdentifierRejected(ValueError):
    """Raised when an identifier fails SPEC 1.5 path-segment rules; format is SPEC 1.5.5."""


_Candidate = tuple[TableMeta, tuple[str, str, str]]


def list_tables(cursor: Cursor, include: list[str], exclude: list[str]) -> list[TableMeta]:
    """Enumerate tables and views in scope, filtered by selectors - the catalog functions
    exclude internal objects, and there is no materialized-view concept here.
    """

    candidates: list[_Candidate] = []

    for source, name_column, table_type in (
        ("duckdb_tables()", "table_name", "table"),
        ("duckdb_views()", "view_name", "view"),
    ):
        rows = exec_query(
            cursor,
            f"""
            SELECT database_name, schema_name, {name_column}
            FROM {source}
            WHERE NOT internal
            ORDER BY database_name, schema_name, {name_column}
            """,
        ).fetchall()

        for database, schema, name in rows:
            path = (database.lower(), schema.lower(), name.lower())
            meta = TableMeta(fqn=".".join(path), type=table_type, namespace_path=path)
            candidates.append((meta, (database, schema, name)))

    in_scope = set(
        expand(
            [meta.fqn for meta, _ in candidates],
            config_include=include,
            config_exclude=exclude,
        ),
    )
    selected = [entry for entry in candidates if entry[0].fqn in in_scope]
    _enforce_identifier_rules(selected)

    return [meta for meta, _ in selected]


def columns(cursor: Cursor, fqn: str) -> list[ColumnMeta]:
    """Per-column structural metadata in ordinal order - `name` is lowercased for the map key
    (SPEC 2.2.1), and `collation` is always `None`, duckdb exposing no per-column surface.
    """

    database, schema, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT column_name, column_index, data_type, is_nullable, column_default
        FROM duckdb_columns()
        WHERE database_name = ? AND schema_name = ? AND table_name = ?
        ORDER BY column_index
        """,
        (database, schema, table),
    ).fetchall()

    return [
        ColumnMeta(
            name=name.lower(),
            sql_type=sql_type,
            nullable=nullable,
            default=default,
            ordinal=int(ordinal),
            physical_name=None if name == name.lower() else name,
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
        "SELECT value FROM duckdb_settings() WHERE name = 'default_collation'",
    ).fetchone()
    value = row[0] if row else ""

    return value or DEFAULT_COLLATION


def relationships(cursor: Cursor, fqn: str) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs; one entry per constraint (composite as arrays) - duckdb parses
    no action clause at all, so every edge is unconditionally `NO ACTION` on both sides.
    """

    database, schema, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT constraint_name, constraint_column_names, referenced_table,
               referenced_column_names
        FROM duckdb_constraints()
        WHERE constraint_type = 'FOREIGN KEY'
          AND database_name = ? AND schema_name = ? AND table_name = ?
        ORDER BY constraint_name
        """,
        (database, schema, table),
    ).fetchall()

    return [
        ForeignKeyMeta(
            column=tuple(source_columns),
            target_table=f"{database}.{schema}.{target_table}".lower(),
            target_column=tuple(target_columns),
            on_delete="NO ACTION",
            on_update="NO ACTION",
            constraint_name=name,
        )
        for name, source_columns, target_table, target_columns in rows
    ]


def indexes(cursor: Cursor, fqn: str) -> list[IndexMeta]:
    """Secondary, non-unique indexes only (SPEC 2.6.7) - constraints and bare indexes live in
    separate catalog functions that never overlap, so no exclusion join is needed.
    """

    database, schema, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT index_name, sql
        FROM duckdb_indexes()
        WHERE NOT is_unique
          AND database_name = ? AND schema_name = ? AND table_name = ?
        ORDER BY index_name
        """,
        (database, schema, table),
    ).fetchall()

    return [
        IndexMeta(name=name.lower(), columns=tuple(_index_columns(sql)), unique=False, type="art")
        for name, sql in rows
    ]


def unique_keys(cursor: Cursor, fqn: str) -> list[UniqueKeyMeta]:
    """Declared-unique column groups: primary key, unique constraints, bare unique indexes."""

    database, schema, table = _split_fqn(fqn)
    out: list[UniqueKeyMeta] = []

    constraint_rows = exec_query(
        cursor,
        """
        SELECT constraint_type, constraint_column_names
        FROM duckdb_constraints()
        WHERE constraint_type IN ('PRIMARY KEY', 'UNIQUE')
          AND database_name = ? AND schema_name = ? AND table_name = ?
        ORDER BY constraint_index
        """,
        (database, schema, table),
    ).fetchall()

    for constraint_type, column_names in constraint_rows:
        out.append(
            UniqueKeyMeta(columns=tuple(column_names), primary=constraint_type == "PRIMARY KEY"),
        )

    index_rows = exec_query(
        cursor,
        """
        SELECT sql
        FROM duckdb_indexes()
        WHERE is_unique AND NOT is_primary
          AND database_name = ? AND schema_name = ? AND table_name = ?
        ORDER BY index_name
        """,
        (database, schema, table),
    ).fetchall()

    for (sql,) in index_rows:
        out.append(UniqueKeyMeta(columns=tuple(_index_columns(sql)), primary=False))

    return out


def physical_layout(cursor: Cursor, fqn: str) -> PhysicalLayout | None:
    """duckdb has no declarative clustering/partitioning key for an ordinary table."""

    del cursor, fqn

    return None


def view_dependencies(cursor: Cursor) -> None:
    """None unconditionally: `duckdb_dependencies()` misses a plain view's read of a table, so
    every view omits `depends_on` rather than publish a guess parsed out of DDL text.
    """

    del cursor


def comments(cursor: Cursor, fqn: str) -> CommentsMeta:
    """Table comment + per-column comments from `duckdb_tables()`/`duckdb_columns()`."""

    database, schema, table = _split_fqn(fqn)
    table_row = exec_query(
        cursor,
        """
        SELECT comment FROM duckdb_tables()
        WHERE database_name = ? AND schema_name = ? AND table_name = ?
        UNION ALL
        SELECT comment FROM duckdb_views()
        WHERE database_name = ? AND schema_name = ? AND view_name = ?
        """,
        (database, schema, table, database, schema, table),
    ).fetchone()

    col_rows = exec_query(
        cursor,
        """
        SELECT column_name, comment FROM duckdb_columns()
        WHERE database_name = ? AND schema_name = ? AND table_name = ?
        """,
        (database, schema, table),
    ).fetchall()

    return CommentsMeta(
        table=table_row[0] if table_row else None,
        columns={
            col_name.lower(): comment for col_name, comment in col_rows if comment is not None
        },
    )


def row_count_estimate(cursor: Cursor, fqn: str) -> int:
    """`estimated_size` from `duckdb_tables()`; -1 for a view or an unknown table - a view
    carries no such column, matching SPEC 2.2.15's never-queried, never-estimated rule.
    """

    database, schema, table = _split_fqn(fqn)
    row = exec_query(
        cursor,
        """
        SELECT estimated_size FROM duckdb_tables()
        WHERE database_name = ? AND schema_name = ? AND table_name = ?
        """,
        (database, schema, table),
    ).fetchone()

    if not row or row[0] is None:
        return -1

    return int(row[0])


_INDEX_COLUMNS_RE = re.compile(r"\(([^)]+)\)")


def _index_columns(create_sql: str) -> list[str]:
    """Column list from a `CREATE [UNIQUE] INDEX ... ON tbl (cols)` statement's own text -
    `duckdb_indexes().expressions` renders as a repr-like string, not a real array.
    """

    match = _INDEX_COLUMNS_RE.search(create_sql or "")

    if not match:
        return []

    return [c.strip().strip('"').lower() for c in match.group(1).split(",") if c.strip()]


def _split_fqn(fqn: str) -> tuple[str, str, str]:
    database, schema, table = fqn.split(".")

    return database, schema, table


def _enforce_identifier_rules(selected: list[_Candidate]) -> None:
    """Reject identifiers that violate SPEC 1.5 before any artifact is written - two objects
    differing only by case would collapse onto one path, the second overwriting it.
    """

    seen: dict[str, tuple[str, str, str]] = {}

    for meta, parts in selected:
        for seg in meta.namespace_path:
            if seg.startswith("."):
                raise IdentifierRejected(_reject_message(meta.fqn, "leading-period", seg))

            if not PATH_SEGMENT_RE.match(seg):
                raise IdentifierRejected(
                    _reject_message(meta.fqn, "contains-unsafe-character", seg),
                )

        previous = seen.get(meta.fqn)

        if previous is not None and previous != parts:
            raise IdentifierRejected(
                _reject_message(
                    meta.fqn,
                    f"case-collides-with-{'.'.join(previous)}",
                    ".".join(parts),
                ),
            )

        seen[meta.fqn] = parts


def _reject_message(fqn: str, reason: str, detail: str) -> str:
    """SPEC 1.5.5 error format - verbatim."""

    return (
        f"ERROR: Table identifier rejected: {fqn}\n"
        f"  Reason: {reason}\n"
        f"  Detail: {detail!r}\n"
        f"  Resolution: Either rename the identifier in the database, OR "
        f"exclude it via .dbprint.yaml selectors:\n"
        f"    exclude:\n"
        f'      - "{fqn}"'
    )
