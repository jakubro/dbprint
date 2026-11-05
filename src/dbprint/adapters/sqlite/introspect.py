"""SQLite's own catalog pragmas for structural metadata, one per intermediate type.

A declared type is free text and values are typed one by one, so an object a run reads also takes
a census of the storage classes its columns hold, which SPEC 3.3's classification rule reads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

from dbprint.spec.classification import (
    classify,
    is_binary_type,
    is_boolean_type,
    is_floating_type,
    is_json_type,
    is_numeric_type,
    is_recognised_type,
    is_spatial_type,
    is_temporal_type,
    is_vector_type,
)
from dbprint.spec.fqn import join as join_fqn
from .connection import DIALECT, Cursor, exec_query
from ..base import (
    ColumnMeta,
    CommentsMeta,
    FkAction,
    ForeignKeyMeta,
    IndexMeta,
    PhysicalLayout,
    TableMeta,
    TableType,
    UniqueKeyMeta,
)
from ..errors import QueryFailed
from ..identifiers import (
    Identity,
    column_meta,
    fold,
    quote,
    quote_path,
    select_tables,
    source_column,
    table_meta,
)
from ..sql_layout import listed


_Candidate = tuple[TableMeta, tuple[str, str]]

_TABLE_TYPES: dict[str, TableType] = {"table": "table", "virtual": "table", "view": "view"}
_ACTIONS: dict[str, FkAction] = {
    action: action for action in ("CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT")
}

# Columns per census statement: five result columns each, under SQLite's default limit of 2000.
_CENSUS_COLUMNS_PER_STATEMENT = 300

DEFAULT_COLLATION = "binary"


def list_tables(cursor: Cursor, include: list[str], exclude: list[str]) -> list[_Candidate]:
    """Enumerate tables, views and virtual tables in scope, filtered by selectors - a virtual
    table's shadow tables and every name SQLite reserves for itself are left out.
    """

    rows = exec_query(
        cursor,
        """
        SELECT
          tbl.schema,
          tbl.name,
          tbl.type
        FROM
          PRAGMA_TABLE_LIST() tbl
        WHERE
          tbl.schema <> 'temp'
          AND tbl.type IN ('table', 'view', 'virtual')
          AND LOWER(SUBSTR(tbl.name, 1, 7)) <> 'sqlite_'
        ORDER BY
          tbl.schema, tbl.name
        """,
    ).fetchall()

    candidates = [
        (table_meta((schema, name), _TABLE_TYPES[kind]), (schema, name))
        for schema, name, kind in rows
    ]

    return select_tables(candidates, include, exclude)


def columns(cursor: Cursor, identity: Identity) -> list[ColumnMeta]:
    """Per-column structural metadata in ordinal order, classified by declared type and affinity
    alone (SPEC 3.3) - a catalog fact, read before any query (SPEC 2.2.15).
    """

    return [
        column_meta(
            col.name,
            sql_type=col.sql_type,
            nullable=col.nullable,
            default=col.default,
            ordinal=ordinal,
            collation=col.collation,
            classify_as=_classify_as(col.sql_type, None),
        )
        for ordinal, col in enumerate(declared_columns(cursor, identity), start=1)
    ]


def read_columns(cursor: Cursor, identity: Identity, columns: list[ColumnMeta]) -> list[ColumnMeta]:
    """`columns` reclassified per SPEC 3.3 from a census of the values each one holds."""

    return [
        replace(col, classify_as=_classify_as(col.sql_type, census))
        for col, census in zip(columns, _census(cursor, identity, columns), strict=True)
    ]


@dataclass(frozen=True)
class DeclaredColumn:
    """One column as the schema declares it; `collation` None where it compares as BINARY."""

    name: str
    sql_type: str
    nullable: bool
    default: str | None
    collation: str | None


def declared_columns(cursor: Cursor, identity: Identity) -> list[DeclaredColumn]:
    """Each stored or generated column's declared type, nullability, default and collation."""

    schema, table = identity.parts
    rows = exec_query(
        cursor,
        """
        SELECT
          col.name,
          col.type,
          col."notnull",
          col.dflt_value,
          col.pk,
          col.hidden
        FROM
          PRAGMA_TABLE_XINFO(?, ?) col
        WHERE
          col.hidden <> 1
        ORDER BY
          col.cid
        """,
        (table, schema),
    ).fetchall()
    keyed = sum(1 for row in rows if row[4])

    return [
        DeclaredColumn(
            name=name,
            sql_type=sql_type or "",
            # An INTEGER PRIMARY KEY is the rowid itself and never holds a NULL.
            nullable=not notnull and not (pk and keyed == 1 and sql_type.upper() == "INTEGER"),
            default=default,
            collation=_collation(cursor, identity, name),
        )
        for name, sql_type, notnull, default, pk, _ in rows
    ]


def is_strict(cursor: Cursor, identity: Identity) -> bool:
    """Whether the table is `STRICT`, where no column affinity converts a value."""

    schema, table = identity.parts
    row = exec_query(
        cursor,
        """
        SELECT
          tbl.strict
        FROM
          PRAGMA_TABLE_LIST() tbl
        WHERE
          tbl.schema = ?
          AND tbl.name = ?
        """,
        (schema, table),
    ).fetchone()

    return bool(row and row[0])


def default_collation(cursor: Cursor) -> str:
    """The comparison collation of a column that declares none."""

    del cursor

    return DEFAULT_COLLATION


def relationships(cursor: Cursor, identity: Identity) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs, one per constraint (composite as arrays) - SQLite names no
    constraint, and a reference naming no column means the parent's primary key, so one whose
    parent has none, or no matching one, declares no edge.
    """

    schema, table = identity.parts
    rows = exec_query(
        cursor,
        """
        SELECT
          fky.id,
          fky."table",
          fky."from",
          fky."to",
          fky.on_update,
          fky.on_delete
        FROM
          PRAGMA_FOREIGN_KEY_LIST(?, ?) fky
        ORDER BY
          fky.id, fky.seq
        """,
        (table, schema),
    ).fetchall()
    grouped: dict[int, list[Any]] = {}

    for row in rows:
        grouped.setdefault(row[0], []).append(row)

    out: list[ForeignKeyMeta] = []

    for members in grouped.values():
        _, parent, _, _, on_update, on_delete = members[0]
        targets = [row[3] for row in members]

        if any(target is None for target in targets):
            targets = _primary_key(cursor, schema, parent)

        if len(targets) != len(members):
            continue

        out.append(
            ForeignKeyMeta(
                column=tuple(fold(row[2]) for row in members),
                target_table=join_fqn([fold(schema), fold(parent)]),
                target_column=tuple(fold(name) for name in targets),
                on_delete=_action(on_delete),
                on_update=_action(on_update),
                constraint_name=None,
            ),
        )

    return out


def indexes(cursor: Cursor, identity: Identity) -> list[IndexMeta]:
    """Secondary, non-unique indexes made by `CREATE INDEX` (SPEC 2.6.7); an expression member
    is left out, and an index with no column member is not reported.
    """

    schema, table = identity.parts
    rows = exec_query(
        cursor,
        """
        SELECT
          idx.name
        FROM
          PRAGMA_INDEX_LIST(?, ?) idx
        WHERE
          idx."unique" = 0
          AND idx.origin = 'c'
        ORDER BY
          idx.name
        """,
        (table, schema),
    ).fetchall()
    out: list[IndexMeta] = []

    for (name,) in rows:
        if members := tuple(fold(m) for m in _members(cursor, schema, name) if m is not None):
            out.append(IndexMeta(name=fold(name), columns=members, unique=False, type="btree"))

    return out


def unique_keys(cursor: Cursor, identity: Identity) -> list[UniqueKeyMeta]:
    """Declared-unique column groups: the primary key, then unique constraints and bare unique
    indexes in declaration order; a partial index, or one with an expression member, declares
    none (SPEC 2.2.12).
    """

    schema, table = identity.parts
    out: list[UniqueKeyMeta] = []

    if primary := _primary_key(cursor, schema, table):
        out.append(UniqueKeyMeta(columns=tuple(fold(name) for name in primary), primary=True))

    rows = exec_query(
        cursor,
        """
        SELECT
          idx.name
        FROM
          PRAGMA_INDEX_LIST(?, ?) idx
        WHERE
          idx."unique" = 1
          AND idx.origin IN ('u', 'c')
          AND idx.partial = 0
        ORDER BY
          idx.seq DESC
        """,
        (table, schema),
    ).fetchall()

    for (name,) in rows:
        members = _members(cursor, schema, name)

        if members and all(m is not None for m in members):
            out.append(UniqueKeyMeta(columns=tuple(fold(m) for m in members if m), primary=False))

    return out


def physical_layout(cursor: Cursor, identity: Identity) -> PhysicalLayout | None:
    """SQLite has no declarative clustering or partitioning key."""

    del cursor, identity

    return None


def view_dependencies(cursor: Cursor, views: Sequence[Identity]) -> dict[str, tuple[str, ...]]:
    """Each view's direct reads, as SQLite itself resolves the view (SPEC 2.2.17): every table it
    reports read and every view it expands while preparing a select of it, less what each such
    view reports in turn unless the view reads it by column itself. A view SQLite cannot prepare
    is left out.
    """

    catalog = _view_names(cursor)
    resolved: dict[tuple[str, str], _Resolution | None] = {}

    def resolve(schema: str, name: str) -> _Resolution | None:
        if (schema, name) not in resolved:
            resolved[schema, name] = _resolution(cursor, schema, name, catalog)

        return resolved[schema, name]

    out: dict[str, tuple[str, ...]] = {}

    for view in views:
        schema, name = view.parts

        if (found := resolve(schema, name)) is None:
            continue

        inner = {
            obj
            for db, other in found.touched
            if (fold(db), fold(other)) in catalog and (nested := resolve(db, other)) is not None
            for obj in nested.touched
        }
        direct = found.by_column | (found.touched - inner)
        out[view.fqn] = tuple(sorted(join_fqn([fold(db), fold(obj)]) for db, obj in direct))

    return out


def comments(cursor: Cursor, identity: Identity) -> CommentsMeta:
    """SQLite stores no table or column comment."""

    del cursor, identity

    return CommentsMeta(table=None, columns={})


def row_count_estimate(cursor: Cursor, identity: Identity) -> int:
    """Exact row count, counted once per session; -1 for a view, never queried (SPEC 2.2.15)."""

    if _kind(cursor, identity) == "view":
        return -1

    counted: dict[str, int] = getattr(cursor, "row_counts", {})

    if identity.fqn not in counted:
        query = f"SELECT COUNT(1) FROM {identity.quoted()} src"
        counted[identity.fqn] = int(exec_query(cursor, query).fetchone()[0])

    return counted[identity.fqn]


def affinity(declared: str) -> str:
    """SQLite's type affinity for a declared type (sqlite.org/datatype3.html section 3.1)."""

    upper = declared.upper()

    if "INT" in upper:
        return "integer"
    elif any(part in upper for part in ("CHAR", "CLOB", "TEXT")):
        return "text"
    elif "BLOB" in upper or not upper.strip():
        return "blob"
    elif any(part in upper for part in ("REAL", "FLOA", "DOUB")):
        return "real"
    else:
        return "numeric"


@dataclass(frozen=True)
class _Census:
    """One column's non-null values by storage class, and how many the gates read (SPEC 3.3)."""

    non_null: int
    integer: int
    real: int
    blob: int
    readable: int


def _classify_as(declared: str, census: _Census | None) -> str | None:
    """The type a column classifies as under SPEC 3.3, or None where the declared type does."""

    family = _family(declared)

    if census is None:
        return _declared_classify_as(declared, family)

    numbers = census.integer + census.real
    passes = {
        "boolean": census.readable == census.non_null,
        "numeric": numbers == census.non_null,
        "temporal": census.readable == census.non_null,
        "binary": census.blob == census.non_null,
        "json": census.readable == census.non_null,
        "text": True,
    }

    if passes.get(family):
        if family == "numeric":
            return "real" if census.real else "integer"

        return None if is_recognised_type(declared) else family

    if census.non_null and numbers == census.non_null:
        return "real" if census.real else "integer"
    elif census.non_null and census.blob == census.non_null:
        return "blob"
    else:
        return "text"


def _family(declared: str) -> str | None:
    """The kind of type a declared type names: by the shared type names, else by its affinity,
    and None for BLOB affinity, which classifies by the values held.
    """

    if is_recognised_type(declared) and classify(declared, None, False, 0) == "unsupported":
        return "unreadable"
    elif is_boolean_type(declared):
        return "boolean"
    elif is_json_type(declared):
        return "json"
    elif is_spatial_type(declared) or is_vector_type(declared):
        return "unreadable"
    elif is_temporal_type(declared):
        return "temporal"
    elif is_numeric_type(declared):
        return "numeric"
    elif is_binary_type(declared):
        return "binary"
    elif is_recognised_type(declared):
        return "text"

    kind = affinity(declared)

    return {"integer": "numeric", "real": "numeric", "numeric": "numeric", "text": "text"}.get(kind)


def _declared_classify_as(declared: str, family: str | None) -> str | None:
    if is_recognised_type(declared) and family != "unreadable":
        return None
    elif family == "numeric":
        return "real" if is_floating_type(declared) or affinity(declared) == "real" else "integer"
    else:
        return "text"


def _census(cursor: Cursor, identity: Identity, columns: list[ColumnMeta]) -> list[_Census]:
    out: list[_Census] = []

    for start in range(0, len(columns), _CENSUS_COLUMNS_PER_STATEMENT):
        batch = columns[start : start + _CENSUS_COLUMNS_PER_STATEMENT]
        selected: list[str] = ["COUNT(1)"]

        for col in batch:
            cn = source_column(col, DIALECT)
            selected.extend(
                (
                    f"COUNT({cn})",
                    _counted(f"TYPEOF({cn}) = 'integer'"),
                    _counted(f"TYPEOF({cn}) = 'real'"),
                    _counted(f"TYPEOF({cn}) = 'blob'"),
                    _counted(_gate_predicate(cn, _family(col.sql_type))),
                ),
            )

        row = exec_query(
            cursor,
            f"""
            SELECT
              {listed(selected, 14)}
            FROM
              {identity.quoted()} src
            """,
        ).fetchone()

        for i in range(len(batch)):
            counts = [int(value or 0) for value in row[1 + 5 * i : 6 + 5 * i]]
            out.append(_Census(*counts))

    return out


def _gate_predicate(cn: str, family: str | None) -> str:
    if family == "boolean":
        return f"TYPEOF({cn}) = 'integer' AND {cn} IN (0, 1)"
    elif family == "temporal":
        return (
            f"TYPEOF({cn}) = 'text' AND {cn} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' "
            f"AND JULIANDAY({cn}) IS NOT NULL"
        )
    elif family == "json":
        jsonb = (
            f" OR (TYPEOF({cn}) = 'blob' AND JSON_VALID({cn}, 8))"
            if sqlite3.sqlite_version_info >= (3, 45, 0)
            else ""
        )

        return f"(TYPEOF({cn}) = 'text' AND JSON_VALID({cn})){jsonb}"
    else:
        return "0"


def _counted(condition: str) -> str:
    return f"SUM(CASE WHEN {condition} THEN 1 ELSE 0 END)"


def _collation(cursor: Cursor, identity: Identity, column: str) -> str | None:
    """The column's collation, read off a compound select that reads no row: the compound's
    comparison takes it from its first branch. None for BINARY, the default.
    """

    row = exec_query(
        cursor,
        f"""
        SELECT
          cmp.c = 'A',
          cmp.c = 'a '
        FROM
          (
            SELECT
              src.{quote(column, DIALECT)} AS c
            FROM
              {identity.quoted()} src
            WHERE
              0

            UNION ALL

            SELECT
              'a'
          ) cmp
        """,
    ).fetchone()

    if row[0]:
        return "nocase"
    elif row[1]:
        return "rtrim"
    else:
        return None


@dataclass(frozen=True)
class _Resolution:
    """`touched` is every object a select of the view reaches, nested views' reads included."""

    touched: frozenset[tuple[str, str]]
    by_column: frozenset[tuple[str, str]]


def _resolution(
    cursor: Cursor,
    schema: str,
    name: str,
    catalog: frozenset[tuple[str, str]],
) -> _Resolution | None:
    touched: set[tuple[str, str]] = set()
    by_column: set[tuple[str, str]] = set()

    def authorize(
        action: int,
        first: str | None,
        second: str | None,
        database: str | None,
        source: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and first:
            touched.add((database or schema, first))

            if second and source == name:
                by_column.add((database or schema, first))

        if source and source != name and (fold(schema), fold(source)) in catalog:
            touched.add((schema, source))

        return sqlite3.SQLITE_OK

    # A sqlite3 cursor carries its connection, which the DB-API `Cursor` protocol leaves out.
    connection: sqlite3.Connection = cast(Any, cursor).connection
    connection.set_authorizer(authorize)

    try:
        exec_query(
            cursor,
            f"EXPLAIN SELECT * FROM {quote_path((schema, name), DIALECT)}",
        ).fetchall()
    except QueryFailed:
        return None
    finally:
        connection.set_authorizer(None)

    touched.discard((schema, name))

    return _Resolution(frozenset(touched), frozenset(by_column))


def _view_names(cursor: Cursor) -> frozenset[tuple[str, str]]:
    rows = exec_query(
        cursor,
        """
        SELECT
          tbl.schema,
          tbl.name
        FROM
          PRAGMA_TABLE_LIST() tbl
        WHERE
          tbl.type = 'view'
        """,
    ).fetchall()

    return frozenset((fold(str(db)), fold(str(view))) for db, view in rows)


def _kind(cursor: Cursor, identity: Identity) -> str:
    schema, table = identity.parts
    row = exec_query(
        cursor,
        """
        SELECT
          tbl.type
        FROM
          PRAGMA_TABLE_LIST() tbl
        WHERE
          tbl.schema = ?
          AND tbl.name = ?
        """,
        (schema, table),
    ).fetchone()

    return str(row[0]) if row else "table"


def _primary_key(cursor: Cursor, schema: str, table: str) -> list[str]:
    rows = exec_query(
        cursor,
        """
        SELECT
          col.name
        FROM
          PRAGMA_TABLE_XINFO(?, ?) col
        WHERE
          col.pk > 0
        ORDER BY
          col.pk
        """,
        (table, schema),
    ).fetchall()

    return [name for (name,) in rows]


def _members(cursor: Cursor, schema: str, index: str) -> list[str | None]:
    """An index's key members in order; None for an expression member."""

    rows = exec_query(
        cursor,
        """
        SELECT
          ixi.name,
          ixi.cid
        FROM
          PRAGMA_INDEX_XINFO(?, ?) ixi
        WHERE
          ixi."key" = 1
        ORDER BY
          ixi.seqno
        """,
        (index, schema),
    ).fetchall()

    return [name if cid >= 0 else None for name, cid in rows]


def _action(action: str) -> FkAction:
    return _ACTIONS.get(action, "NO ACTION")
