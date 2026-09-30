"""INFORMATION_SCHEMA queries for MySQL structural metadata.

No schema layer below the database: the FQN is `<database>.<table>`, enumeration scoped to
the configured database or every readable one. At `lower_case_table_names=0` reads past enumeration bind `Identity`'s spelling.
"""

from __future__ import annotations

import re

from dbprint.config.selectors import expand
from dbprint.spec.fqn import join as join_fqn
from .connection import Cursor, exec_query
from ..base import (
    ColumnMeta,
    CommentsMeta,
    FkAction,
    ForeignKeyMeta,
    IndexMeta,
    PhysicalLayout,
    PhysicalLayoutKey,
    TableMeta,
    TableType,
    UniqueKeyMeta,
)
from ..identifiers import Identity, column_meta, enforce_table_identifiers, fold, table_meta


_TABLE_TYPE_MAP: dict[str, TableType] = {
    "BASE TABLE": "table",
    "VIEW": "view",
}

_FK_ACTIONS: dict[str, FkAction] = {
    "NO ACTION": "NO ACTION",
    "CASCADE": "CASCADE",
    "SET NULL": "SET NULL",
    "SET DEFAULT": "SET DEFAULT",
    "RESTRICT": "RESTRICT",
}

_SYSTEM_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")

_Candidate = tuple[TableMeta, tuple[str, str]]


def list_tables(
    cursor: Cursor,
    database: str | None,
    include: list[str],
    exclude: list[str],
) -> list[_Candidate]:
    """Enumerate tables/views in `database`, or in every database the account can read."""

    where, params = _schema_predicate("tbl.table_schema", database)
    rows = exec_query(
        cursor,
        f"""
        SELECT
          tbl.table_schema,
          tbl.table_name,
          tbl.table_type
        FROM
          information_schema.tables tbl
        WHERE
          {where}
        ORDER BY
          tbl.table_schema, tbl.table_name
        """,
        params,
    ).fetchall()

    candidates: list[_Candidate] = []

    for schema, name, table_type in rows:
        schema_lower = fold(schema)

        if schema_lower in _SYSTEM_SCHEMAS:
            continue

        canonical_type = _TABLE_TYPE_MAP.get(table_type)

        if canonical_type is None:
            continue

        candidates.append((table_meta((schema, name), canonical_type), (schema, name)))

    in_scope = set(
        expand(
            [meta.fqn for meta, _ in candidates],
            config_include=include,
            config_exclude=exclude,
        ),
    )
    selected = [entry for entry in candidates if entry[0].fqn in in_scope]
    enforce_table_identifiers(selected)

    return selected


def columns(cursor: Cursor, identity: Identity) -> list[ColumnMeta]:
    """Per-column structural metadata in ordinal order.

    `physical_name` carries the catalog's spelling; MySQL folds column names case-insensitively,
    so nothing downstream needs it to address the column. MySQL populates `collation_name`
    unconditionally, so the caller compares it against `default_collation()` before emitting.
    """

    rows = exec_query(
        cursor,
        """
        SELECT
          col.column_name,
          col.ordinal_position,
          col.column_type,
          col.is_nullable,
          col.column_default,
          col.collation_name
        FROM
          information_schema.columns col
        WHERE
          col.table_schema = %s
          AND col.table_name = %s
        ORDER BY
          col.ordinal_position
        """,
        identity.parts,
    ).fetchall()

    return [
        column_meta(
            col_name,
            sql_type=str(column_type),
            nullable=(is_nullable == "YES"),
            default=col_default,
            ordinal=int(ordinal),
            collation=collation_name,
        )
        for col_name, ordinal, column_type, is_nullable, col_default, collation_name in rows
    ]


def default_collation(cursor: Cursor) -> str:
    """The session's default collation (SPEC 2.2.2) - one scalar, once per run."""

    row = exec_query(cursor, "SELECT @@collation_database").fetchone()

    return str(row[0]) if row and row[0] is not None else ""


def relationships(cursor: Cursor, identity: Identity) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs; one entry per constraint (composite as arrays)."""

    rows = exec_query(
        cursor,
        """
        SELECT
          kcu.constraint_name,
          kcu.column_name,
          kcu.referenced_table_schema,
          kcu.referenced_table_name,
          kcu.referenced_column_name,
          rfc.update_rule,
          rfc.delete_rule

        FROM
          information_schema.key_column_usage kcu
          JOIN information_schema.referential_constraints rfc ON
            rfc.constraint_schema = kcu.constraint_schema
            AND rfc.constraint_name = kcu.constraint_name

        WHERE
          kcu.table_schema = %s
          AND kcu.table_name = %s
          AND kcu.referenced_table_name IS NOT NULL

        ORDER BY
          kcu.constraint_name, kcu.ordinal_position
        """,
        identity.parts,
    ).fetchall()

    src_cols: dict[str, list[str]] = {}
    dst_cols: dict[str, list[str]] = {}
    targets: dict[str, tuple[str, FkAction, FkAction]] = {}
    order: list[str] = []

    for name, column, ref_schema, ref_table, ref_column, update_rule, delete_rule in rows:
        if name not in src_cols:
            src_cols[name] = []
            dst_cols[name] = []
            targets[name] = (
                join_fqn((fold(ref_schema), fold(ref_table))),
                _FK_ACTIONS.get(str(update_rule).upper(), "NO ACTION"),
                _FK_ACTIONS.get(str(delete_rule).upper(), "NO ACTION"),
            )
            order.append(name)

        src_cols[name].append(fold(column))
        dst_cols[name].append(fold(ref_column))

    out: list[ForeignKeyMeta] = []

    for name in order:
        target_table, on_update, on_delete = targets[name]
        out.append(
            ForeignKeyMeta(
                column=tuple(src_cols[name]),
                target_table=target_table,
                target_column=tuple(dst_cols[name]),
                on_delete=on_delete,
                on_update=on_update,
                constraint_name=name,
            ),
        )

    return out


def indexes(cursor: Cursor, identity: Identity) -> list[IndexMeta]:
    """Secondary non-unique indexes; PRIMARY and unique-backed indexes excluded (SPEC 2.6.7)."""

    rows = exec_query(
        cursor,
        """
        SELECT
          sts.index_name,
          sts.column_name,
          sts.non_unique,
          sts.index_type,
          sts.seq_in_index
        FROM
          information_schema.statistics sts
        WHERE
          sts.table_schema = %s
          AND sts.table_name = %s
          AND sts.index_name <> 'PRIMARY'
          AND sts.non_unique = 1
        ORDER BY
          sts.index_name, sts.seq_in_index
        """,
        identity.parts,
    ).fetchall()

    index_cols: dict[str, list[str]] = {}
    index_unique: dict[str, bool] = {}
    index_type_by_name: dict[str, str] = {}
    order: list[str] = []

    for index_name, column_name, non_unique, index_type, _seq in rows:
        if column_name is None:
            # Functional/expression key part: no plain column for IndexMeta.columns, so skip it.
            continue

        if index_name not in index_cols:
            index_cols[index_name] = []
            index_unique[index_name] = int(non_unique) == 0
            index_type_by_name[index_name] = str(index_type).lower()
            order.append(index_name)

        index_cols[index_name].append(fold(column_name))

    return [
        IndexMeta(
            name=fold(index_name),
            columns=tuple(index_cols[index_name]),
            unique=index_unique[index_name],
            type=index_type_by_name[index_name],
        )
        for index_name in order
    ]


def comments(cursor: Cursor, identity: Identity) -> CommentsMeta:
    """Table comment + per-column comments from INFORMATION_SCHEMA."""

    table_row = exec_query(
        cursor,
        """
        SELECT
          tbl.table_comment
        FROM
          information_schema.tables tbl
        WHERE
          tbl.table_schema = %s
          AND tbl.table_name = %s
        """,
        identity.parts,
    ).fetchone()
    table_comment = table_row[0] if table_row and table_row[0] else None

    col_rows = exec_query(
        cursor,
        """
        SELECT
          col.column_name,
          col.column_comment
        FROM
          information_schema.columns col
        WHERE
          col.table_schema = %s
          AND col.table_name = %s
        """,
        identity.parts,
    ).fetchall()

    return CommentsMeta(
        table=table_comment,
        columns={fold(name): comment for name, comment in col_rows if comment},
    )


def unique_keys(cursor: Cursor, identity: Identity) -> list[UniqueKeyMeta]:
    """Declared-unique column groups; PRIMARY first, then named unique indexes.

    MySQL backs both kinds with an index, so `non_unique = 0` is exactly the declared set.
    """

    rows = exec_query(
        cursor,
        """
        SELECT
          sts.index_name,
          sts.column_name,
          sts.seq_in_index
        FROM
          information_schema.statistics sts
        WHERE
          sts.table_schema = %s
          AND sts.table_name = %s
          AND sts.non_unique = 0
        ORDER BY
          sts.index_name, sts.seq_in_index
        """,
        identity.parts,
    ).fetchall()

    grouped: dict[str, list[str]] = {}

    for index_name, column_name, _ in rows:
        if column_name is None:
            # Functional/expression key part: see `indexes()`'s identical skip above.
            continue

        grouped.setdefault(str(index_name), []).append(fold(column_name))

    ordered = sorted(grouped, key=lambda name: (name != "PRIMARY", name))

    return [
        UniqueKeyMeta(columns=tuple(grouped[name]), primary=name == "PRIMARY") for name in ordered
    ]


# A bare identifier, optionally backtick-quoted: the base column a predicate would filter on.
# A function call (`year(created_at)`) does not match, so `column` comes back None for one.
_BASE_COLUMN_RE = re.compile(r"^`?([A-Za-z_][A-Za-z0-9_$]*)`?$")


def physical_layout(cursor: Cursor, identity: Identity) -> PhysicalLayout | None:
    """Declared partitioning key via INFORMATION_SCHEMA.PARTITIONS; None when unpartitioned.

    Every partition row repeats the same `partition_expression`, so `LIMIT 1` answers.
    COLUMNS-based multi-column partitioning renders as a comma-separated list in that one
    column; a functional expression (`year(created_at)`) yields one key with no base column.
    """

    row = exec_query(
        cursor,
        """
        SELECT
          prt.partition_expression
        FROM
          information_schema.partitions prt
        WHERE
          prt.table_schema = %s
          AND prt.table_name = %s
          AND prt.partition_name IS NOT NULL
        LIMIT 1
        """,
        identity.parts,
    ).fetchone()

    if not row or not row[0]:
        return None

    return _parse_partition_expression(str(row[0]))


def _parse_partition_expression(value: str) -> PhysicalLayout:
    return PhysicalLayout(
        mechanism="partition",
        keys=tuple(_partition_key(part.strip()) for part in value.split(",")),
    )


def _partition_key(expression: str) -> PhysicalLayoutKey:
    match = _BASE_COLUMN_RE.match(expression)

    return PhysicalLayoutKey(
        expression=expression,
        column=fold(match.group(1)) if match else None,
    )


def view_dependencies(cursor: Cursor, database: str | None) -> dict[str, tuple[str, ...]]:
    """Every view's direct object dependencies, one query for the whole connection - the LEFT
    JOIN seeds every view, so one reading nothing answers `()` rather than going absent.
    """

    where, params = _schema_predicate("tbl.table_schema", database)
    rows = exec_query(
        cursor,
        f"""
        SELECT
          tbl.table_schema AS view_schema,
          tbl.table_name AS view_name,
          usg.table_schema AS source_schema,
          usg.table_name AS source_name
        FROM
          information_schema.tables tbl
          LEFT JOIN information_schema.view_table_usage usg ON
            usg.view_schema = tbl.table_schema
            AND usg.view_name = tbl.table_name
        WHERE
          {where}
          AND tbl.table_type = 'VIEW'
        """,
        params,
    ).fetchall()

    out: dict[str, list[str]] = {}

    for view_schema, view_name, source_schema, source_name in rows:
        key = join_fqn((fold(view_schema), fold(view_name)))
        out.setdefault(key, [])

        if source_schema is not None and source_name is not None:
            out[key].append(join_fqn((fold(source_schema), fold(source_name))))

    return {k: tuple(v) for k, v in out.items()}


def table_rows_estimate(cursor: Cursor, identity: Identity) -> int:
    """Catalog row-count estimate (approximate for InnoDB); -1 when unavailable."""

    row = exec_query(
        cursor,
        """
        SELECT
          tbl.table_rows
        FROM
          information_schema.tables tbl
        WHERE
          tbl.table_schema = %s
          AND tbl.table_name = %s
        """,
        identity.parts,
    ).fetchone()

    if not row or row[0] is None:
        return -1

    return int(row[0])


def _schema_predicate(column: str, database: str | None) -> tuple[str, tuple[str, ...]]:
    # Information-schema rows are privilege-filtered, so an unreadable database is absent.
    if database is not None:
        return f"{column} = %s", (database,)

    placeholders = ", ".join("%s" for _ in _SYSTEM_SCHEMAS)

    return f"{column} NOT IN ({placeholders})", _SYSTEM_SCHEMAS
