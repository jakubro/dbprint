"""`system.*` catalog queries for ClickHouse structural metadata - there is no schema tier
below the database, so the FQN is `<database>.<table>`.

`system.*` compares case-sensitively, so every read past enumeration binds `Identity`'s spelling.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from dbprint.config.selectors import expand
from dbprint.spec.classification import is_nullable_type
from .connection import Cursor, exec_query
from ..base import (
    ColumnMeta,
    CommentsMeta,
    ForeignKeyMeta,
    IndexMeta,
    PhysicalLayout,
    PhysicalLayoutKey,
    SkippedNamespace,
    TableMeta,
    TableType,
    UniqueKeyMeta,
)
from ..errors import QueryFailed
from ..identifiers import Identity, column_meta, enforce_table_identifiers, fold, table_meta


# The connection's default comparison collation (SPEC 2.2.2) - ClickHouse has no server-side
# collation model, so this names the fixed byte-comparison semantic rather than querying it.
DEFAULT_COLLATION = "binary"

_TABLE_TYPE_BY_ENGINE: dict[str, TableType] = {
    "View": "view",
    "MaterializedView": "matview",
}

_Candidate = tuple[TableMeta, tuple[str, str]]


def list_tables(
    cursor: Cursor,
    databases: Sequence[str],
    include: list[str],
    exclude: list[str],
) -> tuple[list[_Candidate], dict[str, bool], tuple[SkippedNamespace, ...]]:
    """Enumerate tables/views/matviews in each of `databases`, less a matview's `.inner_id` storage.

    Each comes with its physical spelling, beside the samplable map and every database that failed to list.
    """

    rows: list[tuple[str, Any, Any, Any]] = []
    skipped: list[SkippedNamespace] = []

    # One read per database, so a remote-engine database that fails is skipped alone.
    for database in databases:
        try:
            listed = exec_query(
                cursor,
                """
                SELECT
                  tbl.name,
                  tbl.engine,
                  tbl.sampling_key
                FROM
                  system.tables tbl
                WHERE
                  tbl.database = %s
                  AND tbl.name NOT LIKE '.inner_id.%%'
                ORDER BY
                  tbl.name
                """,
                (database,),
            ).fetchall()
        except QueryFailed as exc:
            skipped.append(SkippedNamespace(name=database, cause=str(exc)))
            continue

        rows.extend((database, *row) for row in listed)

    candidates: list[_Candidate] = []
    samplable: dict[str, bool] = {}

    for database, name, engine, sampling_key in rows:
        table_type = _TABLE_TYPE_BY_ENGINE.get(str(engine), "table")
        physical = (database, str(name))
        meta = table_meta(physical, table_type)
        candidates.append((meta, physical))
        samplable[meta.fqn] = bool(sampling_key)

    in_scope = set(
        expand(
            [meta.fqn for meta, _ in candidates],
            config_include=include,
            config_exclude=exclude,
        ),
    )
    selected = [entry for entry in candidates if entry[0].fqn in in_scope]
    enforce_table_identifiers(selected)

    return selected, {meta.fqn: samplable[meta.fqn] for meta, _ in selected}, tuple(skipped)


def list_databases(cursor: Cursor) -> tuple[str, ...]:
    """Every database the user can see, less ClickHouse's own system databases."""

    rows = exec_query(
        cursor,
        """
        SELECT
          dbs.name
        FROM
          system.databases dbs
        WHERE
          dbs.name NOT IN (
          'system',
          'INFORMATION_SCHEMA',
          'information_schema',
          '_temporary_and_external_tables'
        )
        ORDER BY
          dbs.name
        """,
    ).fetchall()

    return tuple(str(name) for (name,) in rows)


def columns(cursor: Cursor, identity: Identity) -> list[ColumnMeta]:
    """Per-column metadata in ordinal order.

    Nullability is encoded in the type as `Nullable(T)`, possibly nested; `sql_type` keeps it raw.
    """

    rows = exec_query(
        cursor,
        """
        SELECT
          col.name,
          col.type,
          col.position,
          col.default_expression
        FROM
          system.columns col
        WHERE
          col.database = %s
          AND col.table = %s
        ORDER BY
          col.position
        """,
        identity.parts,
    ).fetchall()

    return [
        column_meta(
            str(name),
            sql_type=str(col_type),
            nullable=is_nullable_type(str(col_type)),
            default=default_expression or None,
            ordinal=int(position),
        )
        for name, col_type, position, default_expression in rows
    ]


def default_collation(cursor: Cursor) -> str:
    """The connection's default comparison collation (SPEC 2.2.2) - a fixed constant."""

    del cursor

    return DEFAULT_COLLATION


def relationships(cursor: Cursor, identity: Identity) -> list[ForeignKeyMeta]:
    """No source to read: `REFERENTIAL_CONSTRAINTS` is documented permanently empty and a
    `FOREIGN KEY` clause in `CREATE TABLE` is accepted and silently discarded.
    """

    del cursor, identity

    return []


def indexes(cursor: Cursor, identity: Identity) -> list[IndexMeta]:
    """Data-skipping indexes; `type` carries ClickHouse's own name (`minmax`, `bloom_filter`) -
    one covers an expression, not a column list, so `columns` is empty rather than guessed.
    """

    rows = exec_query(
        cursor,
        """
        SELECT
          idx.name,
          idx.type
        FROM
          system.data_skipping_indices idx
        WHERE
          idx.database = %s
          AND idx.table = %s
        ORDER BY
          idx.name
        """,
        identity.parts,
    ).fetchall()

    return [
        IndexMeta(name=fold(name), columns=(), unique=False, type=str(index_type))
        for name, index_type in rows
    ]


def unique_keys(cursor: Cursor, identity: Identity) -> list[UniqueKeyMeta]:
    """No declared-unique column groups: `PRIMARY KEY` admits duplicate values (SPEC 2.6.7)."""

    del cursor, identity

    return []


def physical_layout(cursor: Cursor, identity: Identity) -> PhysicalLayout | None:
    """Declared partitioning key via `system.tables.partition_key`; None when unpartitioned -
    it is an expression string, so the base column is recovered by parsing, as MySQL's is.
    """

    row = exec_query(
        cursor,
        """
        SELECT
          tbl.partition_key
        FROM
          system.tables tbl
        WHERE
          tbl.database = %s
          AND tbl.name = %s
        """,
        identity.parts,
    ).fetchone()

    if not row or not row[0]:
        return None

    return PhysicalLayout(
        mechanism="partition",
        keys=tuple(_partition_key(part.strip()) for part in str(row[0]).split(",")),
    )


_BASE_COLUMN_RE = re.compile(r"^`?([A-Za-z_][A-Za-z0-9_$]*)`?$")


def _partition_key(expression: str) -> PhysicalLayoutKey:
    match = _BASE_COLUMN_RE.match(expression)

    return PhysicalLayoutKey(
        expression=expression,
        column=fold(match.group(1)) if match else None,
    )


def view_dependencies(cursor: Cursor) -> None:
    """None unconditionally: the dependency tables answer only the reverse edge, and only for
    matviews, so every view omits `depends_on` rather than publish a guess parsed from DDL.
    """

    del cursor


def comments(cursor: Cursor, identity: Identity) -> CommentsMeta:
    """Table comment + per-column comments; ClickHouse reports an absent comment as `''`."""

    table_row = exec_query(
        cursor,
        """
        SELECT
          tbl.comment
        FROM
          system.tables tbl
        WHERE
          tbl.database = %s
          AND tbl.name = %s
        """,
        identity.parts,
    ).fetchone()
    table_comment = table_row[0] if table_row and table_row[0] else None

    col_rows = exec_query(
        cursor,
        """
        SELECT
          col.name,
          col.comment
        FROM
          system.columns col
        WHERE
          col.database = %s
          AND col.table = %s
        """,
        identity.parts,
    ).fetchall()

    return CommentsMeta(
        table=table_comment,
        columns={fold(name): comment for name, comment in col_rows if comment},
    )


def estimate_row_count(cursor: Cursor, identity: Identity) -> float:
    """Catalog row-count estimate; -1 when unavailable (a plain `View` carries none)."""

    row = exec_query(
        cursor,
        """
        SELECT
          tbl.total_rows
        FROM
          system.tables tbl
        WHERE
          tbl.database = %s
          AND tbl.name = %s
        """,
        identity.parts,
    ).fetchone()

    if not row or row[0] is None:
        return -1.0

    return float(row[0])
