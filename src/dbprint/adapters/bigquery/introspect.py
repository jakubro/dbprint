"""BigQuery catalog reads: `INFORMATION_SCHEMA`, project- and dataset-qualified, every one billed.

Past enumeration these bind the physical identifiers the catalog reported - a lowercased name
filters `WHERE table_name = %s` against nothing and reports the table empty.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from dbprint.spec.fqn import join as join_fqn
from .connection import DIALECT, exec_query
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
from ..identifiers import Identity, column_meta, fold, quote, select_tables, table_meta


if TYPE_CHECKING:
    from .connection import Cursor


_TABLE_TYPE_MAP: dict[str, TableType] = {
    "BASE TABLE": "table",
    "CLONE": "table",
    "EXTERNAL": "table",
    "SNAPSHOT": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "matview",
}

_Candidate = tuple[TableMeta, tuple[str, str]]

# `materialize()`'s scratch prefix (`adapters.base.materialized_name`) - a BigQuery sampled copy
# is a real dataset object, not a session temp table, so it is excluded by name, not by lifetime.
_SCRATCH_PREFIX = "dbprint_sample_"


def list_tables(
    cursor: Cursor,
    project: str,
    datasets: Sequence[str],
    include: list[str],
    exclude: list[str],
) -> tuple[list[_Candidate], dict[str, str], tuple[SkippedNamespace, ...]]:
    """Enumerate tables/views/materialized views in each of `datasets`, filtered by selectors.

    Each comes with its physical spelling, beside the DDL cache and each dataset that failed.
    """

    rows: list[tuple[str, Any, Any, Any]] = []
    skipped: list[SkippedNamespace] = []

    for dataset in datasets:
        try:
            listed = _dataset_tables(cursor, project, dataset)
        except QueryFailed as exc:
            if exc.timed_out:
                raise

            skipped.append(SkippedNamespace(name=dataset, cause=str(exc)))
            continue

        rows.extend((dataset, *row) for row in listed)

    candidates: list[_Candidate] = []
    ddl_by_fqn: dict[str, str] = {}

    for dataset, name, table_type, ddl in rows:
        kind = str(table_type).upper()
        canonical_type = _TABLE_TYPE_MAP.get(kind)

        if canonical_type is None:
            continue

        name = str(name)

        if name.startswith(_SCRATCH_PREFIX):
            continue

        meta = table_meta(
            (dataset, name),
            canonical_type,
            external=kind == "EXTERNAL",
            opt_in_only=kind == "SNAPSHOT",
        )
        candidates.append((meta, (dataset, name)))

        if ddl:
            ddl_by_fqn[meta.fqn] = str(ddl)

    selected = select_tables(candidates, include, exclude)
    selected_fqns = {meta.fqn for meta, _ in selected}

    return (
        selected,
        {fqn: ddl for fqn, ddl in ddl_by_fqn.items() if fqn in selected_fqns},
        tuple(skipped),
    )


def columns(
    cursor: Cursor,
    project: str,
    identity: Identity,
) -> list[ColumnMeta]:
    """`INFORMATION_SCHEMA.COLUMNS` in ordinal order.

    The view has no `column_default`, so every default is None; `is_hidden` pseudo columns are dropped.
    """

    base_select = f"""
        SELECT
          col.column_name,
          col.data_type,
          col.is_nullable,
          col.ordinal_position{{collation}}
        FROM
          {_info_schema(project, identity.parts[0])}.COLUMNS col
        WHERE
          col.table_name = %s
          AND col.is_hidden = 'NO'
        ORDER BY
          col.ordinal_position
        """

    try:
        rows = exec_query(
            cursor,
            base_select.format(collation=", col.collation_name"),
            (identity.table,),
        ).fetchall()
    except Exception as exc:
        if getattr(exc, "timed_out", False):
            raise

        rows = [
            (name, data_type, is_nullable, ordinal, None)
            for name, data_type, is_nullable, ordinal in exec_query(
                cursor,
                base_select.format(collation=""),
                (identity.table,),
            ).fetchall()
        ]

    return [
        column_meta(
            str(name),
            sql_type=str(data_type),
            nullable=str(is_nullable).upper() != "NO",
            default=None,
            ordinal=int(ordinal),
            collation=str(collation) if collation else None,
        )
        for name, data_type, is_nullable, ordinal, collation in rows
    ]


def default_collation() -> str:
    """Binary: BigQuery's documented default (SPEC 2.2.2), with no dataset-level default to query."""

    return "binary"


def relationships(cursor: Cursor, project: str, identity: Identity) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs, informational only - `enforced` is documented 'Only `NO`'.

    `KEY_COLUMN_USAGE` alone orders a composite key: `ordinal_position` over the referencing
    columns, `position_in_unique_constraint` into the referenced key. `CONSTRAINT_COLUMN_USAGE`
    carries no ordinal, so it is read only for the referenced table's name.
    """

    info = _info_schema(project, identity.parts[0])
    fk_rows = exec_query(
        cursor,
        f"""
        SELECT
          kcu.constraint_name,
          kcu.column_name,
          kcu.position_in_unique_constraint
        FROM
          {info}.TABLE_CONSTRAINTS tcn
          JOIN {info}.KEY_COLUMN_USAGE kcu ON tcn.constraint_name = kcu.constraint_name
        WHERE
          tcn.table_name = %s
          AND tcn.constraint_type = 'FOREIGN KEY'
        ORDER BY
          kcu.constraint_name, kcu.ordinal_position
        """,
        (identity.table,),
    ).fetchall()

    if not fk_rows:
        return []

    grouped: dict[str, list[tuple[str, int]]] = {}

    for constraint_name, column_name, position_in_unique in fk_rows:
        grouped.setdefault(str(constraint_name), []).append(
            (fold(str(column_name)), int(position_in_unique)),
        )

    out: list[ForeignKeyMeta] = []

    for name, columns_and_positions in grouped.items():
        ref_table = _referenced_table(cursor, project, identity.parts[0], name)

        if ref_table is None:
            continue

        ref_ordinals = _primary_key_ordinals(cursor, project, identity.parts[0], ref_table)
        resolved: list[str] = []

        for _col, position in columns_and_positions:
            target = ref_ordinals.get(position)

            if target is None:
                break  # the referenced key could not be resolved - never publish a guess

            resolved.append(target)
        else:
            out.append(
                ForeignKeyMeta(
                    column=tuple(col for col, _position in columns_and_positions),
                    target_table=join_fqn((fold(identity.parts[0]), fold(ref_table))),
                    target_column=tuple(resolved),
                    constraint_name=name,
                    # `enforced` is documented "Only `NO`" here, so this is the fact, not a guess.
                    on_delete="NO ACTION",
                    on_update="NO ACTION",
                ),
            )

    return out


def _referenced_table(
    cursor: Cursor,
    project: str,
    dataset: str,
    constraint_name: str,
) -> str | None:
    """The table a foreign key's `CONSTRAINT_COLUMN_USAGE` rows name - all rows agree, so one does."""

    row = exec_query(
        cursor,
        f"""
        SELECT
          usg.table_name
        FROM
          {_info_schema(project, dataset)}.CONSTRAINT_COLUMN_USAGE usg
        WHERE
          usg.constraint_name = %s
        LIMIT 1
        """,
        (constraint_name,),
    ).fetchone()

    return str(row[0]) if row else None


def _primary_key_ordinals(cursor: Cursor, project: str, dataset: str, table: str) -> dict[int, str]:
    """The referenced primary key as an ordinal-to-column map - what an FK's ordinal indexes into."""

    info = _info_schema(project, dataset)
    rows = exec_query(
        cursor,
        f"""
        SELECT
          kcu.ordinal_position,
          kcu.column_name
        FROM
          {info}.TABLE_CONSTRAINTS tcn
          JOIN {info}.KEY_COLUMN_USAGE kcu ON tcn.constraint_name = kcu.constraint_name
        WHERE
          tcn.table_name = %s
          AND tcn.constraint_type = 'PRIMARY KEY'
        """,
        (table,),
    ).fetchall()

    return {int(position): fold(str(column)) for position, column in rows}


def indexes(cursor: Cursor, fqn: str) -> list[IndexMeta]:
    """Always empty: search and vector indexes are neither secondary indexes nor a SQL join
    target.
    """

    del cursor, fqn

    return []


def unique_keys(cursor: Cursor, project: str, identity: Identity) -> list[UniqueKeyMeta]:
    """The primary key alone - BigQuery has no UNIQUE constraint type."""

    info = _info_schema(project, identity.parts[0])
    rows = exec_query(
        cursor,
        f"""
        SELECT
          kcu.column_name,
          tcn.constraint_name
        FROM
          {info}.TABLE_CONSTRAINTS tcn
          JOIN {info}.KEY_COLUMN_USAGE kcu ON tcn.constraint_name = kcu.constraint_name
        WHERE
          tcn.table_name = %s
          AND tcn.constraint_type = 'PRIMARY KEY'
        ORDER BY
          kcu.ordinal_position
        """,
        (identity.table,),
    ).fetchall()

    if not rows:
        return []

    columns_in_order = tuple(fold(str(r[0])) for r in rows)

    return [UniqueKeyMeta(columns=columns_in_order, primary=True)]


def physical_layout(cursor: Cursor, project: str, identity: Identity) -> PhysicalLayout | None:
    """Declared clustering or partitioning key, clustering taking precedence when both are
    declared - `clustering_ordinal_position` is measured absent here, so that read is retried.
    """

    # Hidden columns stay in the read: on an ingestion-time-partitioned table the pseudo-column is
    # the only `is_partitioning_column` row; dropping it publishes "not partitioned" (SPEC 2.2.11).
    base_select = f"""
        SELECT
          col.column_name,
          col.is_partitioning_column,
          col.is_hidden{{clustering}}
        FROM
          {_info_schema(project, identity.parts[0])}.COLUMNS col
        WHERE
          col.table_name = %s
        ORDER BY
          col.ordinal_position
        """

    try:
        rows = exec_query(
            cursor,
            base_select.format(clustering=", col.clustering_ordinal_position"),
            (identity.table,),
        ).fetchall()
        has_clustering_column = True
    except Exception as exc:
        if getattr(exc, "timed_out", False):
            raise

        rows = exec_query(
            cursor,
            base_select.format(clustering=""),
            (identity.table,),
        ).fetchall()
        has_clustering_column = False

    cluster_cols = (
        sorted((r for r in rows if r[3] is not None), key=lambda r: int(r[3]))
        if has_clustering_column
        else []
    )

    if cluster_cols:
        return PhysicalLayout(
            mechanism="cluster",
            keys=tuple(_layout_key(r) for r in cluster_cols),
        )

    partition_cols = [r for r in rows if str(r[1]).upper() == "YES"]

    if partition_cols:
        return PhysicalLayout(
            mechanism="partition",
            keys=tuple(_layout_key(r) for r in partition_cols),
        )

    return None


def _layout_key(row: tuple[Any, ...]) -> PhysicalLayoutKey:
    """One layout key from a `(column_name, is_partitioning_column, is_hidden, ...)` row.

    Per SPEC 2.2.11 a hidden pseudo-column contributes its expression but no `column` back-ref.
    """

    name = str(row[0])
    hidden = str(row[2]).upper() == "YES"

    return PhysicalLayoutKey(expression=name, column=None if hidden else fold(name))


def view_dependencies(cursor: Cursor) -> None:
    """None unconditionally - BigQuery has no view-dependency catalog, only `VIEWS.view_definition`,
    so `depends_on` is omitted rather than guessed from DDL text.
    """

    del cursor


def comments(cursor: Cursor, project: str, identity: Identity) -> CommentsMeta:
    """Table description from `TABLE_OPTIONS`, column descriptions from `COLUMN_FIELD_PATHS`.

    A nested `RECORD`/`STRUCT` field carries a dotted `field_path`, so only rows where it equals
    the bare `column_name` are real columns.
    """

    table_row = exec_query(
        cursor,
        f"""
        SELECT
          opt.option_value
        FROM
          {_info_schema(project, identity.parts[0])}.TABLE_OPTIONS opt
        WHERE
          opt.table_name = %s
          AND opt.option_name = 'description'
        """,
        (identity.table,),
    ).fetchone()
    table_comment = _unquote_option(str(table_row[0])) if table_row and table_row[0] else None

    try:
        column_rows = exec_query(
            cursor,
            f"""
            SELECT
              fpt.column_name,
              fpt.description
            FROM
              {_info_schema(project, identity.parts[0])}.COLUMN_FIELD_PATHS fpt
            WHERE
              fpt.table_name = %s
              AND fpt.field_path = fpt.column_name
              AND fpt.description IS NOT NULL
            """,
            (identity.table,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - a connection with no such view at all
        column_rows = []

    column_comments = {fold(str(name)): str(description) for name, description in column_rows}

    return CommentsMeta(table=table_comment, columns=column_comments)


def _unquote_option(raw: str) -> str:
    """`TABLE_OPTIONS.option_value` renders a STRING option as a quoted SQL literal."""

    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]

    return raw


def estimate_row_count(cursor: Cursor, project: str, identity: Identity) -> int | None:
    """`INFORMATION_SCHEMA.PARTITIONS.total_rows`, summed over the table's own partitions.

    A refused read raises: `None` would be indistinguishable from a table with no rows.
    """

    row = exec_query(
        cursor,
        f"""
        SELECT
          SUM(prt.total_rows)
        FROM
          {_info_schema(project, identity.parts[0])}.PARTITIONS prt
        WHERE
          prt.table_name = %s
        """,
        (identity.table,),
    ).fetchone()

    return int(row[0]) if row and row[0] is not None else None


def row_count_hint(cursor: Cursor, project: str, identity: Identity) -> int | None:
    """The same estimate for a caller holding its own fallback, or None where the read refuses.

    Both callers have an exact alternative, so a refused read costs a round trip, not the table.
    """

    try:
        return estimate_row_count(cursor, project, identity)
    except QueryFailed:
        return None


def _dataset_tables(cursor: Cursor, project: str, dataset: str) -> list[tuple[Any, ...]]:
    try:
        return exec_query(
            cursor,
            f"""
            SELECT
              tbl.table_name,
              tbl.table_type,
              tbl.ddl
            FROM
              {_info_schema(project, dataset)}.TABLES tbl
            ORDER BY
              tbl.table_name
            """,
        ).fetchall()
    except Exception as exc:
        if getattr(exc, "timed_out", False):
            raise

        return [
            (name, table_type, None)
            for name, table_type in exec_query(
                cursor,
                f"""
                SELECT
                  tbl.table_name,
                  tbl.table_type
                FROM
                  {_info_schema(project, dataset)}.TABLES tbl
                ORDER BY
                  tbl.table_name
                """,
            ).fetchall()
        ]


def _info_schema(project: str, dataset: str) -> str:
    return f"{quote(project, DIALECT)}.{quote(dataset, DIALECT)}.INFORMATION_SCHEMA"
