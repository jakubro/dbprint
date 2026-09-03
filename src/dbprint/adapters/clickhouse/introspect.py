"""`system.*` catalog queries for ClickHouse structural metadata - there is no schema tier
below the database, so the FQN is `<database>.<table>`.

Column names fold to the lowercase map key and keep their spelling in `physical_name` (SPEC 2.2.1).
Database and table names carry none, so one SPEC 1.5 cannot spell is refused (SPEC 1.5.5).
"""

from __future__ import annotations

import re

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
    TableMeta,
    TableType,
    UniqueKeyMeta,
)


PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]*$")

# The connection's default comparison collation (SPEC 2.2.2) - ClickHouse has no server-side
# collation model, so this names the fixed byte-comparison semantic rather than querying it.
DEFAULT_COLLATION = "binary"

_TABLE_TYPE_BY_ENGINE: dict[str, TableType] = {
    "View": "view",
    "MaterializedView": "matview",
}


class IdentifierRejected(ValueError):
    """Raised when a ClickHouse identifier fails SPEC 1.5 path-segment rules; format SPEC 1.5.5."""


def list_tables(
    cursor: Cursor,
    database: str,
    include: list[str],
    exclude: list[str],
) -> tuple[list[TableMeta], dict[str, bool]]:
    """Enumerate tables/views/matviews in the connected database, filtered by selectors -
    a matview's hidden `.inner_id.<uuid>` storage is excluded, never reported as its own table.

    Also returns the fqn-to-samplable map: `SAMPLE` needs a `SAMPLE BY` key declared at creation,
    which `system.tables.sampling_key` answers here rather than a failed `CREATE` later.
    """

    rows = exec_query(
        cursor,
        "SELECT name, engine, sampling_key FROM system.tables "
        "WHERE database = %s AND name NOT LIKE '.inner_id.%%' "
        "ORDER BY name",
        (database,),
    ).fetchall()

    candidates: list[TableMeta] = []
    samplable: dict[str, bool] = {}

    for name, engine, sampling_key in rows:
        table_type = _TABLE_TYPE_BY_ENGINE.get(str(engine), "table")
        # Both segments go into the FQN verbatim: `system.*` compares case-sensitively, so a fold
        # here would address a table that does not exist.
        fqn = f"{database}.{name}"
        candidates.append(
            TableMeta(fqn=fqn, type=table_type, namespace_path=(database, str(name))),
        )
        samplable[fqn] = bool(sampling_key)

    in_scope = set(
        expand(
            [meta.fqn for meta in candidates],
            config_include=include,
            config_exclude=exclude,
        ),
    )
    selected = [meta for meta in candidates if meta.fqn in in_scope]
    _enforce_identifier_rules(selected)

    return selected, {meta.fqn: samplable[meta.fqn] for meta in selected}


def columns(cursor: Cursor, fqn: str) -> list[ColumnMeta]:
    """Per-column metadata in ordinal order - nullability is encoded in the type as `Nullable(T)`,
    possibly nested (`LowCardinality(Nullable(T))`); `sql_type` keeps the raw catalog spelling.
    """

    database, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        "SELECT name, type, position, default_expression FROM system.columns "
        "WHERE database = %s AND table = %s ORDER BY position",
        (database, table),
    ).fetchall()

    return [
        ColumnMeta(
            name=_norm(name),
            sql_type=str(col_type),
            nullable=is_nullable_type(str(col_type)),
            default=default_expression or None,
            ordinal=int(position),
            physical_name=None if name == _norm(name) else name,
        )
        for name, col_type, position, default_expression in rows
    ]


def default_collation(cursor: Cursor) -> str:
    """The connection's default comparison collation (SPEC 2.2.2) - a fixed constant."""

    del cursor

    return DEFAULT_COLLATION


def relationships(cursor: Cursor, fqn: str) -> list[ForeignKeyMeta]:
    """No source to read: `REFERENTIAL_CONSTRAINTS` is documented permanently empty and a
    `FOREIGN KEY` clause in `CREATE TABLE` is accepted and silently discarded.
    """

    del cursor, fqn

    return []


def indexes(cursor: Cursor, fqn: str) -> list[IndexMeta]:
    """Data-skipping indexes; `type` carries ClickHouse's own name (`minmax`, `bloom_filter`) -
    one covers an expression, not a column list, so `columns` is empty rather than guessed.
    """

    database, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        "SELECT name, type FROM system.data_skipping_indices "
        "WHERE database = %s AND table = %s ORDER BY name",
        (database, table),
    ).fetchall()

    return [
        IndexMeta(name=_norm(name), columns=(), unique=False, type=str(index_type))
        for name, index_type in rows
    ]


def unique_keys(cursor: Cursor, fqn: str) -> list[UniqueKeyMeta]:
    """No declared-unique column groups: `PRIMARY KEY` admits duplicate values (SPEC 2.6.7)."""

    del cursor, fqn

    return []


def physical_layout(cursor: Cursor, fqn: str) -> PhysicalLayout | None:
    """Declared partitioning key via `system.tables.partition_key`; None when unpartitioned -
    it is an expression string, so the base column is recovered by parsing, as MySQL's is.
    """

    database, table = _split_fqn(fqn)
    row = exec_query(
        cursor,
        "SELECT partition_key FROM system.tables WHERE database = %s AND name = %s",
        (database, table),
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
        column=_norm(match.group(1)) if match else None,
    )


def view_dependencies(cursor: Cursor) -> None:
    """None unconditionally: the dependency tables answer only the reverse edge, and only for
    matviews, so every view omits `depends_on` rather than publish a guess parsed from DDL.
    """

    del cursor


def comments(cursor: Cursor, fqn: str) -> CommentsMeta:
    """Table comment + per-column comments; ClickHouse reports an absent comment as `''`."""

    database, table = _split_fqn(fqn)
    table_row = exec_query(
        cursor,
        "SELECT comment FROM system.tables WHERE database = %s AND name = %s",
        (database, table),
    ).fetchone()
    table_comment = table_row[0] if table_row and table_row[0] else None

    col_rows = exec_query(
        cursor,
        "SELECT name, comment FROM system.columns WHERE database = %s AND table = %s",
        (database, table),
    ).fetchall()

    return CommentsMeta(
        table=table_comment,
        columns={_norm(name): comment for name, comment in col_rows if comment},
    )


def estimate_row_count(cursor: Cursor, fqn: str) -> float:
    """Catalog row-count estimate; -1 when unavailable (a plain `View` carries none)."""

    database, table = _split_fqn(fqn)
    row = exec_query(
        cursor,
        "SELECT total_rows FROM system.tables WHERE database = %s AND name = %s",
        (database, table),
    ).fetchone()

    if not row or row[0] is None:
        return -1.0

    return float(row[0])


def _enforce_identifier_rules(selected: list[TableMeta]) -> None:
    """Reject identifiers SPEC 1.5 cannot spell, before any artifact is written.

    The segments carry the catalog's own spelling, so a capital fails here rather than folding
    into a path that then addresses nothing - and two names differing only by case stay two.
    """

    for meta in selected:
        for seg in meta.namespace_path:
            if seg.startswith("."):
                raise IdentifierRejected(_reject_message(meta.fqn, "leading-period", seg))

            if not PATH_SEGMENT_RE.match(seg):
                raise IdentifierRejected(
                    _reject_message(meta.fqn, "contains-unsafe-character", seg),
                )


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
        raise ValueError(f"ClickHouse FQN must be 'database.table', got {fqn!r}")

    database, _, table = fqn.partition(".")

    return database, table


def _norm(name: str) -> str:
    """Lowercase an identifier - the artifact's map key (SPEC 2.2.1)."""

    return name.lower()
