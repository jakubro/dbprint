"""INFORMATION_SCHEMA queries for MySQL structural metadata.

MySQL has no schema layer below the database, so the FQN is `<database>.<table>` and
enumeration is scoped to `DATABASE()`. Identifiers are lowercased (SPEC 1.3), backticks stripped.
"""

from __future__ import annotations

import re

from dbprint.config.selectors import expand
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


PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]*$")


class IdentifierRejected(ValueError):
    """Raised when a MySQL identifier fails SPEC 1.5 path-segment rules; format SPEC 1.5.5."""


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


def list_tables(cursor: Cursor, include: list[str], exclude: list[str]) -> list[TableMeta]:
    """Enumerate tables/views in the connected database, filtered by selectors."""

    rows = exec_query(
        cursor,
        """
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
        ORDER BY table_schema, table_name
        """,
    ).fetchall()

    candidates: list[_Candidate] = []

    for schema, name, table_type in rows:
        schema_lower = _norm(schema)

        if schema_lower in _SYSTEM_SCHEMAS:
            continue

        canonical_type = _TABLE_TYPE_MAP.get(table_type)

        if canonical_type is None:
            continue

        name_lower = _norm(name)
        candidates.append(
            (
                TableMeta(
                    fqn=f"{schema_lower}.{name_lower}",
                    type=canonical_type,
                    namespace_path=(schema_lower, name_lower),
                ),
                (schema, name),
            ),
        )

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
    """Per-column structural metadata in ordinal order.

    `physical_name` carries the catalog's spelling; MySQL folds column names case-insensitively,
    so nothing downstream needs it to address the column. MySQL populates `collation_name`
    unconditionally, so the caller compares it against `default_collation()` before emitting.
    """

    database, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT column_name, ordinal_position, column_type, is_nullable, column_default,
               collation_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (database, table),
    ).fetchall()

    return [
        ColumnMeta(
            name=_norm(col_name),
            sql_type=str(column_type),
            nullable=(is_nullable == "YES"),
            default=col_default,
            ordinal=int(ordinal),
            physical_name=None if col_name == _norm(col_name) else col_name.strip("`"),
            collation=collation_name,
        )
        for col_name, ordinal, column_type, is_nullable, col_default, collation_name in rows
    ]


def default_collation(cursor: Cursor) -> str:
    """The session's default collation (SPEC 2.2.2) - one scalar, once per run."""

    row = exec_query(cursor, "SELECT @@collation_database").fetchone()

    return str(row[0]) if row and row[0] is not None else ""


def relationships(cursor: Cursor, fqn: str) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs; one entry per constraint (composite as arrays)."""

    database, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT
            kcu.constraint_name,
            kcu.column_name,
            kcu.referenced_table_schema,
            kcu.referenced_table_name,
            kcu.referenced_column_name,
            rc.update_rule,
            rc.delete_rule
        FROM information_schema.key_column_usage kcu
        JOIN information_schema.referential_constraints rc
          ON rc.constraint_schema = kcu.constraint_schema
         AND rc.constraint_name = kcu.constraint_name
        WHERE kcu.table_schema = %s
          AND kcu.table_name = %s
          AND kcu.referenced_table_name IS NOT NULL
        ORDER BY kcu.constraint_name, kcu.ordinal_position
        """,
        (database, table),
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
                f"{_norm(ref_schema)}.{_norm(ref_table)}",
                _FK_ACTIONS.get(str(update_rule).upper(), "NO ACTION"),
                _FK_ACTIONS.get(str(delete_rule).upper(), "NO ACTION"),
            )
            order.append(name)

        src_cols[name].append(_norm(column))
        dst_cols[name].append(_norm(ref_column))

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


def indexes(cursor: Cursor, fqn: str) -> list[IndexMeta]:
    """Secondary non-unique indexes; PRIMARY and unique-backed indexes excluded (SPEC 2.6.7)."""

    database, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT index_name, column_name, non_unique, index_type, seq_in_index
        FROM information_schema.statistics
        WHERE table_schema = %s
          AND table_name = %s
          AND index_name <> 'PRIMARY'
          AND non_unique = 1
        ORDER BY index_name, seq_in_index
        """,
        (database, table),
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

        index_cols[index_name].append(_norm(column_name))

    return [
        IndexMeta(
            name=_norm(index_name),
            columns=tuple(index_cols[index_name]),
            unique=index_unique[index_name],
            type=index_type_by_name[index_name],
        )
        for index_name in order
    ]


def comments(cursor: Cursor, fqn: str) -> CommentsMeta:
    """Table comment + per-column comments from INFORMATION_SCHEMA."""

    database, table = _split_fqn(fqn)
    table_row = exec_query(
        cursor,
        "SELECT table_comment FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (database, table),
    ).fetchone()
    table_comment = table_row[0] if table_row and table_row[0] else None

    col_rows = exec_query(
        cursor,
        "SELECT column_name, column_comment FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (database, table),
    ).fetchall()

    return CommentsMeta(
        table=table_comment,
        columns={_norm(name): comment for name, comment in col_rows if comment},
    )


def unique_keys(cursor: Cursor, fqn: str) -> list[UniqueKeyMeta]:
    """Declared-unique column groups; PRIMARY first, then named unique indexes.

    MySQL backs both kinds with an index, so `non_unique = 0` is exactly the declared set.
    """

    database, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT index_name, column_name, seq_in_index
        FROM information_schema.statistics
        WHERE table_schema = %s
          AND table_name = %s
          AND non_unique = 0
        ORDER BY index_name, seq_in_index
        """,
        (database, table),
    ).fetchall()

    grouped: dict[str, list[str]] = {}

    for index_name, column_name, _ in rows:
        if column_name is None:
            # Functional/expression key part: see `indexes()`'s identical skip above.
            continue

        grouped.setdefault(str(index_name), []).append(_norm(column_name))

    ordered = sorted(grouped, key=lambda name: (name != "PRIMARY", name))

    return [
        UniqueKeyMeta(columns=tuple(grouped[name]), primary=name == "PRIMARY") for name in ordered
    ]


# A bare identifier, optionally backtick-quoted: the base column a predicate would filter on.
# A function call (`year(created_at)`) does not match, so `column` comes back None for one.
_BASE_COLUMN_RE = re.compile(r"^`?([A-Za-z_][A-Za-z0-9_$]*)`?$")


def physical_layout(cursor: Cursor, fqn: str) -> PhysicalLayout | None:
    """Declared partitioning key via INFORMATION_SCHEMA.PARTITIONS; None when unpartitioned.

    Every partition row repeats the same `partition_expression`, so `LIMIT 1` answers.
    COLUMNS-based multi-column partitioning renders as a comma-separated list in that one
    column; a functional expression (`year(created_at)`) yields one key with no base column.
    """

    database, table = _split_fqn(fqn)
    row = exec_query(
        cursor,
        "SELECT partition_expression FROM information_schema.partitions "
        "WHERE table_schema = %s AND table_name = %s AND partition_name IS NOT NULL "
        "LIMIT 1",
        (database, table),
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
        column=_norm(match.group(1)) if match else None,
    )


def table_rows_estimate(cursor: Cursor, fqn: str) -> int:
    """Catalog row-count estimate (approximate for InnoDB); -1 when unavailable."""

    database, table = _split_fqn(fqn)
    row = exec_query(
        cursor,
        "SELECT table_rows FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (database, table),
    ).fetchone()

    if not row or row[0] is None:
        return -1

    return int(row[0])


def _enforce_identifier_rules(selected: list[_Candidate]) -> None:
    """Reject identifiers that violate SPEC 1.5 before any artifact is written.

    Two names differing only by case collapse onto one path, so one would overwrite the other.
    """

    seen: dict[str, tuple[str, str]] = {}

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


def _split_fqn(fqn: str) -> tuple[str, str]:
    if "." not in fqn:
        raise ValueError(f"MySQL FQN must be 'database.table', got {fqn!r}")

    database, _, table = fqn.partition(".")

    return database.strip("`"), table.strip("`")


def _norm(name: str) -> str:
    """Lowercase an identifier and strip backticks - the `columns` map key (SPEC 2.2.1)."""

    return name.strip("`").lower()
