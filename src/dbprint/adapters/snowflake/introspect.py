"""INFORMATION_SCHEMA queries for structural metadata, one function per intermediate type.

Every function past enumeration binds the physical identifiers the catalog reported; the
engine's lowercased path segments would match zero rows and read as an empty table. System
schemas are excluded from `list_tables` regardless of selectors.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, cast

from dbprint.config.selectors import expand
from dbprint.spec.classification import map_types
from dbprint.spec.fqn import join as join_fqn
from .connection import DIALECT, Cursor, exec_query
from ..base import (
    ColumnMeta,
    CommentsMeta,
    FkAction,
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
from ..identifiers import (
    Identity,
    column_meta,
    enforce_table_identifiers,
    fold,
    quote,
    table_meta,
)
from ..sql_layout import listed


# The spellings information_schema.tables.table_type actually reports for Snowflake.
_TABLE_TYPE_MAP: dict[str, TableType] = {
    "BASE TABLE": "table",
    "EVENT TABLE": "table",
    "EXTERNAL TABLE": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "matview",
}

# Reported kinds deliberately left out: a temporary table lives in its own session alone.
_UNLISTED_TABLE_TYPES = frozenset({"TEMPORARY TABLE"})

_FK_ACTIONS: dict[str, FkAction] = {
    "NO ACTION": "NO ACTION",
    "CASCADE": "CASCADE",
    "SET NULL": "SET NULL",
    "SET DEFAULT": "SET DEFAULT",
    "RESTRICT": "RESTRICT",
}

# Only Snowflake's own system schema; a MAIN or PG_CATALOG here is a user schema to profile.
_SYSTEM_SCHEMAS = ("information_schema",)

# Snowflake's own shared database, listed by SHOW DATABASES as `kind = APPLICATION`.
_SYSTEM_DATABASE = "SNOWFLAKE"

# Column offset of `name` in a `SHOW DATABASES` row: created_on, name, is_default, ...
_SHOW_DATABASES_NAME = 1

# SHOW output is a fixed result-set shape, not a view, so this module reads it positionally.

# Offsets in the shared `SHOW PRIMARY KEYS`/`SHOW UNIQUE KEYS` shape: created_on, database,
# schema, table, column, key_sequence, constraint_name, rely, comment.
_KEY_COLUMN = 4
_KEY_SEQUENCE = 5
_KEY_CONSTRAINT_NAME = 6

# Column offsets in a `SHOW IMPORTED KEYS` row.
_IK_PK_DATABASE = 1
_IK_PK_SCHEMA = 2
_IK_PK_TABLE = 3
_IK_PK_COLUMN = 4
_IK_FK_COLUMN = 8
_IK_KEY_SEQUENCE = 9
_IK_UPDATE_RULE = 10
_IK_DELETE_RULE = 11
_IK_FK_NAME = 12

# Column offsets in a `SHOW TABLES` row: created_on, name, database_name, schema_name, kind,
# comment, cluster_by, ... Unverifiable against a live Snowflake account from this substrate
# (see GUIDELINES "adapters"): duckdb proves the statement shape, never the real output.
_SHOW_TABLES_NAME = 1
_SHOW_TABLES_CLUSTER_BY = 6

# Snowflake's clustering-key form: `LINEAR(expr[, expr...])`.
_CLUSTER_BY_RE = re.compile(r"^LINEAR\((.*)\)$", re.IGNORECASE)
# A bare identifier, optionally quoted or cast (`LOGGED_AT::DATE`) - the base column filtered on.
_BASE_COLUMN_RE = re.compile(r'^"?([A-Za-z_][A-Za-z0-9_$]*)"?(?:::.*)?$')

_Candidate = tuple[TableMeta, tuple[str, str, str]]


def list_tables(
    cursor: Cursor,
    databases: Sequence[str],
    include: list[str],
    exclude: list[str],
) -> tuple[list[_Candidate], tuple[SkippedNamespace, ...]]:
    """Enumerate tables/views/matviews in user schemas of `databases`, filtered by selectors.

    Each comes with its physical spelling, beside every database that failed to list.
    """

    rows: list[tuple[Any, ...]] = []
    skipped: list[SkippedNamespace] = []

    for database in databases:
        # Each database's INFORMATION_SCHEMA describes that database alone; the bound catalog
        # filter is what confines the read on a substrate whose schema spans every catalog.
        try:
            rows += exec_query(
                cursor,
                f"""
                SELECT
                  tbl.table_catalog,
                  tbl.table_schema,
                  tbl.table_name,
                  tbl.table_type
                FROM
                  {_info_schema(database, "tables")} tbl
                WHERE
                  tbl.table_catalog = ?
                ORDER BY
                  tbl.table_catalog, tbl.table_schema, tbl.table_name
                """,
                [database],
            ).fetchall()
        except QueryFailed as exc:
            skipped.append(SkippedNamespace(name=database, cause=str(exc)))

    candidates: list[_Candidate] = []

    for catalog, schema, name, table_type in rows:
        if fold(schema) in _SYSTEM_SCHEMAS or table_type in _UNLISTED_TABLE_TYPES:
            continue

        canonical_type = _TABLE_TYPE_MAP.get(table_type)

        if canonical_type is None:
            continue

        physical = (catalog, schema, name)
        meta = table_meta(physical, canonical_type, external=table_type == "EXTERNAL TABLE")
        candidates.append((meta, physical))

    in_scope = set(
        expand(
            [meta.fqn for meta, _ in candidates],
            config_include=include,
            config_exclude=exclude,
        ),
    )
    selected = [entry for entry in candidates if entry[0].fqn in in_scope]
    enforce_table_identifiers(selected)

    return selected, tuple(skipped)


def list_databases(cursor: Cursor, like: str | None = None) -> tuple[str, ...]:
    """Databases the role holds a privilege on, less Snowflake's own; `like` narrows by name."""

    statement = "SHOW DATABASES" if like is None else f"SHOW DATABASES LIKE '{_like(like)}'"
    rows = exec_query(cursor, statement).fetchall()
    names = (str(row[_SHOW_DATABASES_NAME]) for row in rows)

    return tuple(name for name in names if name.upper() != _SYSTEM_DATABASE)


def columns(cursor: Cursor, identity: Identity) -> list[ColumnMeta]:
    """Per-column structural metadata in ordinal order.

    `collation_name` is NULL without an explicit `COLLATE` (SPEC 2.2.2); Snowflake has no default.
    """

    rows = exec_query(
        cursor,
        f"""
        SELECT
          col.column_name,
          col.ordinal_position,
          col.data_type,
          col.is_nullable,
          col.column_default,
          col.collation_name
        FROM
          {_info_schema(identity.parts[0], "columns")} col
        WHERE
          col.table_catalog = ?
          AND col.table_schema = ?
          AND col.table_name = ?
        ORDER BY
          col.ordinal_position
        """,
        identity.parts,
    ).fetchall()

    maps = _map_types(cursor, identity) if any(row[2] == "MAP" for row in rows) else {}

    return [
        column_meta(
            col_name,
            sql_type=data_type,
            nullable=(is_nullable == "YES"),
            default=col_default,
            ordinal=int(ordinal),
            collation=collation_name,
            classify_as=maps.get(col_name),
        )
        for col_name, ordinal, data_type, is_nullable, col_default, collation_name in rows
    ]


# Snowflake carries no database- or session-level default collation to query: a column with
# no explicit COLLATE compares under raw UTF-8 binary ordering, a documented engine fact.
DEFAULT_COLLATION = "utf8_binary"


def default_collation(cursor: Cursor) -> str:
    """Snowflake's documented comparison default for a column with no explicit `COLLATE`.

    Nothing here is queryable; `cursor` only matches the other two adapters' signature.
    """

    del cursor

    return DEFAULT_COLLATION


def relationships(cursor: Cursor, identity: Identity) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs; one entry per constraint (composite as arrays).

    Snowflake's INFORMATION_SCHEMA carries no column-level constraint data, so the ordered
    source/target columns come from `SHOW IMPORTED KEYS`, one row per FK column, with
    `key_sequence` giving composite order.
    """

    rows = exec_query(cursor, f"SHOW IMPORTED KEYS IN TABLE {identity.quoted()}").fetchall()
    grouped: dict[str, list[Any]] = {}

    for row in rows:
        grouped.setdefault(str(row[_IK_FK_NAME]), []).append(row)

    out: list[ForeignKeyMeta] = []

    for fk_name, fk_rows in grouped.items():
        ordered = sorted(fk_rows, key=lambda r: int(r[_IK_KEY_SEQUENCE]))
        head = ordered[0]
        target = join_fqn(
            [fold(str(head[index])) for index in (_IK_PK_DATABASE, _IK_PK_SCHEMA, _IK_PK_TABLE)],
        )

        out.append(
            ForeignKeyMeta(
                column=tuple(fold(str(r[_IK_FK_COLUMN])) for r in ordered),
                target_table=target,
                target_column=tuple(fold(str(r[_IK_PK_COLUMN])) for r in ordered),
                on_delete=_FK_ACTIONS.get(str(head[_IK_DELETE_RULE]).upper(), "NO ACTION"),
                on_update=_FK_ACTIONS.get(str(head[_IK_UPDATE_RULE]).upper(), "NO ACTION"),
                constraint_name=fk_name,
            ),
        )

    # Always str here: fk_name came from grouping by str(row[_IK_FK_NAME]). The field is
    # str | None only because other adapters' inferred edges carry no name.
    return sorted(out, key=lambda fk: cast(str, fk.constraint_name))


def indexes(cursor: Cursor, identity: Identity) -> list[IndexMeta]:
    """Secondary indexes via INFORMATION_SCHEMA.INDEXES + INDEX_COLUMNS.

    Only hybrid tables have them, so a standard table yields an empty list; `key_sequence`
    carries the in-index column order.
    """

    rows = exec_query(
        cursor,
        f"""
        SELECT
          idx.name,
          idx.is_unique,
          icl.name

        FROM
          {_info_schema(identity.parts[0], "indexes")} idx
          JOIN {_info_schema(identity.parts[0], "index_columns")} icl ON
            icl.table_catalog = idx.table_catalog
            AND icl.table_schema = idx.table_schema
            AND icl.table_name = idx.table_name
            AND icl.index_name = idx.name

        WHERE
          idx.table_catalog = ?
          AND idx.table_schema = ?
          AND idx.table_name = ?

        ORDER BY
          idx.name, icl.key_sequence
        """,
        identity.parts,
    ).fetchall()

    grouped: dict[str, tuple[bool, list[str]]] = {}

    for index_name, is_unique, column_name in rows:
        _unique, index_columns = grouped.setdefault(index_name, (_is_true(is_unique), []))
        index_columns.append(fold(column_name))

    return [
        IndexMeta(name=fold(name), columns=tuple(cols), unique=unique, type="btree")
        for name, (unique, cols) in grouped.items()
    ]


def comments(cursor: Cursor, identity: Identity) -> CommentsMeta:
    """Table comment + per-column comments from INFORMATION_SCHEMA."""

    table_row = exec_query(
        cursor,
        f"""
        SELECT
          tbl.comment
        FROM
          {_info_schema(identity.parts[0], "tables")} tbl
        WHERE
          tbl.table_catalog = ?
          AND tbl.table_schema = ?
          AND tbl.table_name = ?
        """,
        identity.parts,
    ).fetchone()
    table_comment = table_row[0] if table_row else None

    col_rows = exec_query(
        cursor,
        f"""
        SELECT
          col.column_name,
          col.comment
        FROM
          {_info_schema(identity.parts[0], "columns")} col
        WHERE
          col.table_catalog = ?
          AND col.table_schema = ?
          AND col.table_name = ?
        """,
        identity.parts,
    ).fetchall()

    return CommentsMeta(
        table=table_comment,
        columns={fold(col_name): comment for col_name, comment in col_rows if comment is not None},
    )


def unique_keys(cursor: Cursor, identity: Identity) -> list[UniqueKeyMeta]:
    """Declared-unique column groups via SHOW PRIMARY KEYS / SHOW UNIQUE KEYS.

    Grouped by constraint name, `key_sequence` giving composite order. Snowflake records
    these constraints without enforcing them.
    """

    out: list[UniqueKeyMeta] = []

    for command, primary in (
        ("SHOW PRIMARY KEYS IN TABLE", True),
        ("SHOW UNIQUE KEYS IN TABLE", False),
    ):
        rows = exec_query(cursor, f"{command} {identity.quoted()}").fetchall()
        grouped: dict[str, list[Any]] = {}

        for row in rows:
            grouped.setdefault(str(row[_KEY_CONSTRAINT_NAME]), []).append(row)

        for name in sorted(grouped):
            ordered = sorted(grouped[name], key=lambda r: int(r[_KEY_SEQUENCE]))
            out.append(
                UniqueKeyMeta(
                    columns=tuple(fold(str(r[_KEY_COLUMN])) for r in ordered),
                    primary=primary,
                ),
            )

    return out


def physical_layout(cursor: Cursor, identity: Identity) -> PhysicalLayout | None:
    """Declared clustering key via `SHOW TABLES`; None when the table has none.

    INFORMATION_SCHEMA.TABLES carries no clustering-key column. The SHOW is unfiltered:
    its pattern matching reads `_`/`%` in a table name as wildcards, so a `LIKE` could
    match the wrong table.
    """

    database, schema, table = identity.parts
    schema_ref = f"{quote(database, DIALECT)}.{quote(schema, DIALECT)}"
    rows = exec_query(cursor, f"SHOW TABLES IN SCHEMA {schema_ref}").fetchall()

    for row in rows:
        if fold(str(row[_SHOW_TABLES_NAME])) != fold(table):
            continue

        cluster_by = row[_SHOW_TABLES_CLUSTER_BY]

        return _parse_cluster_by(str(cluster_by)) if cluster_by else None

    return None


def _parse_cluster_by(value: str) -> PhysicalLayout:
    match = _CLUSTER_BY_RE.match(value.strip())
    inner = match.group(1) if match else value.strip()

    return PhysicalLayout(
        mechanism="cluster",
        keys=tuple(_cluster_key(part.strip()) for part in _split_top_level_commas(inner)),
    )


def _cluster_key(expression: str) -> PhysicalLayoutKey:
    match = _BASE_COLUMN_RE.match(expression)

    return PhysicalLayoutKey(
        expression=expression,
        column=fold(match.group(1)) if match else None,
    )


def _split_top_level_commas(text: str) -> list[str]:
    """Split on commas outside parentheses - a clustering expression may nest a function call."""

    parts: list[str] = []
    depth = 0
    current: list[str] = []

    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1

        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)

    parts.append("".join(current))

    return parts


def view_dependencies(cursor: Cursor, databases: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Every view/matview's direct object dependencies in `databases` - one seed read per
    database, then one read of an account-wide catalog that lags up to three hours.
    """

    view_rows = [
        row
        for database in databases
        for row in exec_query(
            cursor,
            f"""
            SELECT
              tbl.table_catalog,
              tbl.table_schema,
              tbl.table_name
            FROM
              {_info_schema(database, "tables")} tbl
            WHERE
              tbl.table_catalog = ?
              AND tbl.table_type IN ('VIEW', 'MATERIALIZED VIEW')
            """,
            [database],
        ).fetchall()
    ]

    out: dict[str, list[str]] = {
        join_fqn((fold(catalog), fold(schema), fold(name))): []
        for catalog, schema, name in view_rows
    }

    dep_rows = exec_query(
        cursor,
        f"""
        SELECT
          dep.referencing_database,
          dep.referencing_schema,
          dep.referencing_object_name,
          dep.referenced_database,
          dep.referenced_schema,
          dep.referenced_object_name
        FROM
          snowflake.account_usage.object_dependencies dep
        WHERE
          dep.referencing_object_domain IN ('VIEW', 'MATERIALIZED VIEW')
          AND dep.referenced_object_domain IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW', 'EXTERNAL TABLE')
          AND UPPER(dep.referencing_database) IN (
            {listed(["UPPER(?)"] * len(databases), 12)}
          )
        """,
        list(databases),
    ).fetchall()

    for view_db, view_schema, view_name, source_db, source_schema, source_name in dep_rows:
        key = join_fqn((fold(view_db), fold(view_schema), fold(view_name)))
        out.setdefault(key, []).append(
            join_fqn((fold(source_db), fold(source_schema), fold(source_name))),
        )

    return {k: tuple(v) for k, v in out.items()}


def row_count_estimate(cursor: Cursor, identity: Identity) -> int:
    """Catalog row count for the table, never a scan; -1 when the catalog has no entry."""

    row = exec_query(
        cursor,
        f"""
        SELECT
          tbl.row_count
        FROM
          {_info_schema(identity.parts[0], "tables")} tbl
        WHERE
          tbl.table_catalog = ?
          AND tbl.table_schema = ?
          AND tbl.table_name = ?
        """,
        identity.parts,
    ).fetchone()

    if not row or row[0] is None:
        return -1

    return int(row[0])


def _is_true(value: Any) -> bool:
    """Normalize a catalog boolean that may arrive as a bool or a YES/NO string."""

    if isinstance(value, str):
        return value.strip().upper() in ("YES", "TRUE", "Y")

    return bool(value)


def _info_schema(database: str, view: str) -> str:
    # Unqualified, INFORMATION_SCHEMA resolves only against a session's current database.
    return f"{quote(database, DIALECT)}.information_schema.{view}"


def _like(name: str) -> str:
    # A wildcard in the name only widens the match; the caller keeps the exact one.
    return name.replace("'", "''")


def _map_types(cursor: Cursor, identity: Identity) -> dict[str, str]:
    # `COLUMNS` reports a structured map as bare `MAP`; `DESCRIBE TABLE` names its key and value.
    rows = exec_query(cursor, f"DESCRIBE TABLE {identity.quoted()}").fetchall()

    return {str(row[0]): str(row[1]) for row in rows if map_types(str(row[1])) is not None}
