"""BigQuery catalog reads: `INFORMATION_SCHEMA`, project- and dataset-qualified, every one billed.

Past enumeration these bind the physical identifiers the catalog reported - a lowercased name
filters `WHERE table_name = %s` against nothing and reports the table empty.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from dbprint.config.selectors import expand
from .connection import exec_query
from .identity import Identity
from ..base import (
    ColumnMeta,
    CommentsMeta,
    ForeignKeyMeta,
    IndexMeta,
    PhysicalLayout,
    PhysicalLayoutKey,
    TableMeta,
    TableType,
    UniqueKeyMeta,
)


if TYPE_CHECKING:
    from .connection import Cursor


PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]*$")


class IdentifierRejected(ValueError):
    """Raised when an identifier fails SPEC 1.5 path-segment rules; format is SPEC 1.5.5."""


_TABLE_TYPE_MAP: dict[str, TableType] = {
    "BASE TABLE": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "matview",
}

_Candidate = tuple[TableMeta, str]

# `materialize()`'s scratch prefix (`adapters.base.materialized_name`) - a BigQuery sampled copy
# is a real dataset object, not a session temp table, so it is excluded by name, not by lifetime.
_SCRATCH_PREFIX = "dbprint_sample_"


def list_tables(
    cursor: Cursor,
    project: str,
    dataset: str,
    include: list[str],
    exclude: list[str],
) -> tuple[list[TableMeta], dict[str, str], dict[str, str]]:
    """Enumerate tables/views/materialized views in the dataset, filtered by selectors.

    Also returns the lowercase-FQN-to-physical-name map (the only point where both forms are
    visible) and the `fqn`-keyed DDL cache `extract_ddl` reads before querying.
    """

    try:
        rows = exec_query(
            cursor,
            f"""
            SELECT table_name, table_type, ddl
            FROM `{project}`.`{dataset}`.INFORMATION_SCHEMA.TABLES
            ORDER BY table_name
            """,
        ).fetchall()
    except Exception:  # noqa: BLE001 - retry without a column this connection cannot see
        rows = [
            (name, table_type, None)
            for name, table_type in exec_query(
                cursor,
                f"""
                SELECT table_name, table_type
                FROM `{project}`.`{dataset}`.INFORMATION_SCHEMA.TABLES
                ORDER BY table_name
                """,
            ).fetchall()
        ]

    candidates: list[_Candidate] = []
    ddl_by_fqn: dict[str, str] = {}

    for name, table_type, ddl in rows:
        canonical_type = _TABLE_TYPE_MAP.get(str(table_type).upper())

        if canonical_type is None:
            continue

        name = str(name)

        if name.startswith(_SCRATCH_PREFIX):
            continue
        # BigQuery dataset names may carry capitals and SPEC 1.5 requires a lowercase path segment,
        # so the artifact side is folded here; the physical `dataset` keeps its catalog spelling.
        name_lower = name.lower()
        dataset_lower = dataset.lower()
        fqn = f"{dataset_lower}.{name_lower}"
        candidates.append(
            (
                TableMeta(
                    fqn=fqn,
                    type=canonical_type,
                    namespace_path=(dataset_lower, name_lower),
                ),
                name,
            ),
        )

        if ddl:
            ddl_by_fqn[fqn] = str(ddl)

    selected_fqns = expand([meta.fqn for meta, _ in candidates], include, exclude)
    selected = [entry for entry in candidates if entry[0].fqn in selected_fqns]
    _enforce_identifier_rules(selected)

    return (
        [meta for meta, _ in selected],
        {fqn: ddl for fqn, ddl in ddl_by_fqn.items() if fqn in selected_fqns},
        {meta.fqn: physical for meta, physical in selected},
    )


def columns(
    cursor: Cursor,
    project: str,
    identity: Identity,
) -> tuple[list[ColumnMeta], dict[str, str]]:
    """`INFORMATION_SCHEMA.COLUMNS` in ordinal order, plus a lowercase-to-physical column-name map.

    `column_default` is absent from that view, so every column reports `default=None`; `is_hidden`
    drops the pseudo columns whose NULL `ordinal_position` would otherwise shift every real ordinal.
    `collation_name` is empty absent an explicit `COLLATE` (SPEC 2.2.2).
    """

    base_select = f"""
        SELECT column_name, data_type, is_nullable, ordinal_position{{collation}}
        FROM `{project}`.`{identity.dataset}`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = %s AND is_hidden = 'NO'
        ORDER BY ordinal_position
        """

    try:
        rows = exec_query(
            cursor,
            base_select.format(collation=", collation_name"),
            (identity.table,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - retry without the column this connection cannot see
        rows = [
            (name, data_type, is_nullable, ordinal, None)
            for name, data_type, is_nullable, ordinal in exec_query(
                cursor,
                base_select.format(collation=""),
                (identity.table,),
            ).fetchall()
        ]

    metas = [
        ColumnMeta(
            name=str(name).lower(),
            sql_type=str(data_type),
            nullable=str(is_nullable).upper() != "NO",
            default=None,
            ordinal=int(ordinal),
            physical_name=None if str(name) == str(name).lower() else str(name),
            collation=str(collation) if collation else None,
        )
        for name, data_type, is_nullable, ordinal, collation in rows
    ]

    return metas, {str(name).lower(): str(name) for name, *_rest in rows}


def default_collation() -> str:
    """Binary: BigQuery's documented default (SPEC 2.2.2), with no dataset-level default to query."""

    return "binary"


def relationships(cursor: Cursor, project: str, identity: Identity) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs, informational only - `enforced` is documented 'Only `NO`'.

    `KEY_COLUMN_USAGE` alone orders a composite key: `ordinal_position` over the referencing
    columns, `position_in_unique_constraint` into the referenced key. `CONSTRAINT_COLUMN_USAGE`
    carries no ordinal, so it is read only for the referenced table's name.
    """

    fk_rows = exec_query(
        cursor,
        f"""
        SELECT kcu.constraint_name, kcu.column_name, kcu.position_in_unique_constraint
        FROM `{project}`.`{identity.dataset}`.INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN `{project}`.`{identity.dataset}`.INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
            ON tc.constraint_name = kcu.constraint_name
        WHERE tc.table_name = %s AND tc.constraint_type = 'FOREIGN KEY'
        ORDER BY kcu.constraint_name, kcu.ordinal_position
        """,
        (identity.table,),
    ).fetchall()

    if not fk_rows:
        return []

    grouped: dict[str, list[tuple[str, int]]] = {}

    for constraint_name, column_name, position_in_unique in fk_rows:
        grouped.setdefault(str(constraint_name), []).append(
            (str(column_name).lower(), int(position_in_unique)),
        )

    out: list[ForeignKeyMeta] = []

    for name, columns_and_positions in grouped.items():
        ref_table = _referenced_table(cursor, project, identity.dataset, name)

        if ref_table is None:
            continue

        ref_ordinals = _primary_key_ordinals(cursor, project, identity.dataset, ref_table)
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
                    target_table=f"{identity.dataset.lower()}.{ref_table.lower()}",
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
        SELECT table_name
        FROM `{project}`.`{dataset}`.INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE
        WHERE constraint_name = %s
        LIMIT 1
        """,
        (constraint_name,),
    ).fetchone()

    return str(row[0]) if row else None


def _primary_key_ordinals(cursor: Cursor, project: str, dataset: str, table: str) -> dict[int, str]:
    """The referenced primary key as an ordinal-to-column map - what an FK's ordinal indexes into."""

    rows = exec_query(
        cursor,
        f"""
        SELECT kcu.ordinal_position, kcu.column_name
        FROM `{project}`.`{dataset}`.INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN `{project}`.`{dataset}`.INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
            ON tc.constraint_name = kcu.constraint_name
        WHERE tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY'
        """,
        (table,),
    ).fetchall()

    return {int(position): str(column).lower() for position, column in rows}


def indexes(cursor: Cursor, fqn: str) -> list[IndexMeta]:
    """Always empty: search and vector indexes are neither secondary indexes nor a SQL join
    target.
    """

    del cursor, fqn

    return []


def unique_keys(cursor: Cursor, project: str, identity: Identity) -> list[UniqueKeyMeta]:
    """The primary key alone - BigQuery has no UNIQUE constraint type."""

    rows = exec_query(
        cursor,
        f"""
        SELECT kcu.column_name, tc.constraint_name
        FROM `{project}`.`{identity.dataset}`.INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN `{project}`.`{identity.dataset}`.INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
            ON tc.constraint_name = kcu.constraint_name
        WHERE tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        (identity.table,),
    ).fetchall()

    if not rows:
        return []

    columns_in_order = tuple(str(r[0]).lower() for r in rows)

    return [UniqueKeyMeta(columns=columns_in_order, primary=True)]


def physical_layout(cursor: Cursor, project: str, identity: Identity) -> PhysicalLayout | None:
    """Declared clustering or partitioning key, clustering taking precedence when both are
    declared - `clustering_ordinal_position` is measured absent here, so that read is retried.
    """

    # Hidden columns stay in the read: on an ingestion-time-partitioned table the pseudo-column is
    # the only `is_partitioning_column` row; dropping it publishes "not partitioned" (SPEC 2.2.11).
    base_select = f"""
        SELECT column_name, is_partitioning_column, is_hidden{{clustering}}
        FROM `{project}`.`{identity.dataset}`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = %s
        ORDER BY ordinal_position
        """

    try:
        rows = exec_query(
            cursor,
            base_select.format(clustering=", clustering_ordinal_position"),
            (identity.table,),
        ).fetchall()
        has_clustering_column = True
    except Exception:  # noqa: BLE001 - retry without the column this connection cannot see
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

    return PhysicalLayoutKey(expression=name, column=None if hidden else name.lower())


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
        SELECT option_value
        FROM `{project}`.`{identity.dataset}`.INFORMATION_SCHEMA.TABLE_OPTIONS
        WHERE table_name = %s AND option_name = 'description'
        """,
        (identity.table,),
    ).fetchone()
    table_comment = _unquote_option(str(table_row[0])) if table_row and table_row[0] else None

    try:
        column_rows = exec_query(
            cursor,
            f"""
            SELECT column_name, description
            FROM `{project}`.`{identity.dataset}`.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS
            WHERE table_name = %s AND field_path = column_name AND description IS NOT NULL
            """,
            (identity.table,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - a connection with no such view at all
        column_rows = []

    column_comments = {str(name).lower(): str(description) for name, description in column_rows}

    return CommentsMeta(table=table_comment, columns=column_comments)


def _unquote_option(raw: str) -> str:
    """`TABLE_OPTIONS.option_value` renders a STRING option as a quoted SQL literal."""

    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]

    return raw


def estimate_row_count(cursor: Cursor, project: str, identity: Identity) -> int | None:
    """`INFORMATION_SCHEMA.TABLE_STORAGE.total_rows`, read through the adapter's own SQL cursor -
    a billed query with the 10 MB minimum, accepted to stay on the one query seam.
    """

    try:
        row = exec_query(
            cursor,
            f"""
            SELECT total_rows
            FROM `{project}`.`{identity.dataset}`.INFORMATION_SCHEMA.TABLE_STORAGE
            WHERE table_name = %s
            """,
            (identity.table,),
        ).fetchone()
    except Exception:  # noqa: BLE001 - no estimate rather than a failed table
        return None

    return int(row[0]) if row and row[0] is not None else None


def _enforce_identifier_rules(selected: list[_Candidate]) -> None:
    """Reject SPEC 1.5 violations before any write - two names differing only by case collapse
    onto one path, so one would overwrite the other.
    """

    seen: dict[str, str] = {}

    for meta, physical in selected:
        for seg in meta.namespace_path:
            if seg.startswith("."):
                raise IdentifierRejected(_reject_message(meta.fqn, "leading-period", seg))

            if not PATH_SEGMENT_RE.match(seg):
                raise IdentifierRejected(
                    _reject_message(meta.fqn, "contains-unsafe-character", seg),
                )

        previous = seen.get(meta.fqn)

        if previous is not None and previous != physical:
            raise IdentifierRejected(
                _reject_message(meta.fqn, f"case-collides-with-{previous}", physical),
            )

        seen[meta.fqn] = physical


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
