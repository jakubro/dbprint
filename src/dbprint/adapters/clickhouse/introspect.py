"""`system.*` catalog queries for ClickHouse structural metadata - there is no schema tier
below the database, so the FQN is `<database>.<table>`.

`system.*` compares case-sensitively, so every read past enumeration binds `Identity`'s spelling.
"""

from __future__ import annotations

import re

from dbprint.config.selectors import expand
from dbprint.spec.classification import is_nullable_type
from .connection import Cursor, exec_query
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


PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]*$")

# The connection's default comparison collation (SPEC 2.2.2) - ClickHouse has no server-side
# collation model, so this names the fixed byte-comparison semantic rather than querying it.
DEFAULT_COLLATION = "binary"

_TABLE_TYPE_BY_ENGINE: dict[str, TableType] = {
    "View": "view",
    "MaterializedView": "matview",
}

_Candidate = tuple[TableMeta, tuple[str, str]]


class IdentifierRejected(ValueError):
    """Raised when a ClickHouse identifier fails SPEC 1.5 path-segment rules; format SPEC 1.5.5."""


def list_tables(
    cursor: Cursor,
    database: str,
    include: list[str],
    exclude: list[str],
) -> tuple[list[TableMeta], dict[str, bool], dict[str, tuple[str, str]]]:
    """Enumerate tables/views/matviews in the connected database, filtered by selectors -
    a matview's hidden `.inner_id.<uuid>` storage is excluded, never reported as its own table.

    Also returns fqn-to-samplable (from `system.tables.sampling_key`) and fqn-to-physical maps.
    """

    rows = exec_query(
        cursor,
        "SELECT name, engine, sampling_key FROM system.tables "
        "WHERE database = %s AND name NOT LIKE '.inner_id.%%' "
        "ORDER BY name",
        (database,),
    ).fetchall()

    candidates: list[_Candidate] = []
    samplable: dict[str, bool] = {}

    for name, engine, sampling_key in rows:
        table_type = _TABLE_TYPE_BY_ENGINE.get(str(engine), "table")
        path = (_norm(database), _norm(str(name)))
        fqn = ".".join(path)
        candidates.append(
            (
                TableMeta(fqn=fqn, type=table_type, namespace_path=path),
                (database, str(name)),
            ),
        )
        samplable[fqn] = bool(sampling_key)

    in_scope = set(
        expand(
            [meta.fqn for meta, _ in candidates],
            config_include=include,
            config_exclude=exclude,
        ),
    )
    selected = [entry for entry in candidates if entry[0].fqn in in_scope]
    _enforce_identifier_rules(selected)

    return (
        [meta for meta, _ in selected],
        {meta.fqn: samplable[meta.fqn] for meta, _ in selected},
        {meta.fqn: parts for meta, parts in selected},
    )


def columns(cursor: Cursor, identity: Identity) -> tuple[list[ColumnMeta], dict[str, str]]:
    """Per-column metadata in ordinal order, plus a lowercase-to-physical column-name map.

    Nullability is encoded in the type as `Nullable(T)`, possibly nested; `sql_type` keeps it raw.
    """

    rows = exec_query(
        cursor,
        "SELECT name, type, position, default_expression FROM system.columns "
        "WHERE database = %s AND table = %s ORDER BY position",
        identity.parts,
    ).fetchall()

    metas = [
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

    return metas, {_norm(row[0]): str(row[0]) for row in rows}


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
        "SELECT name, type FROM system.data_skipping_indices "
        "WHERE database = %s AND table = %s ORDER BY name",
        identity.parts,
    ).fetchall()

    return [
        IndexMeta(name=_norm(name), columns=(), unique=False, type=str(index_type))
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
        "SELECT partition_key FROM system.tables WHERE database = %s AND name = %s",
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
        column=_norm(match.group(1)) if match else None,
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
        "SELECT comment FROM system.tables WHERE database = %s AND name = %s",
        identity.parts,
    ).fetchone()
    table_comment = table_row[0] if table_row and table_row[0] else None

    col_rows = exec_query(
        cursor,
        "SELECT name, comment FROM system.columns WHERE database = %s AND table = %s",
        identity.parts,
    ).fetchall()

    return CommentsMeta(
        table=table_comment,
        columns={_norm(name): comment for name, comment in col_rows if comment},
    )


def estimate_row_count(cursor: Cursor, identity: Identity) -> float:
    """Catalog row-count estimate; -1 when unavailable (a plain `View` carries none)."""

    row = exec_query(
        cursor,
        "SELECT total_rows FROM system.tables WHERE database = %s AND name = %s",
        identity.parts,
    ).fetchone()

    if not row or row[0] is None:
        return -1.0

    return float(row[0])


def _enforce_identifier_rules(selected: list[_Candidate]) -> None:
    """Reject identifiers SPEC 1.5 cannot spell, before any artifact is written.

    Judged on folded segments (SPEC 1.5.1 after 1.3), so two names folding to one path are rejected.
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
    """SPEC 1.5.5 error format - verbatim, quoting the folded path an exclude then matches."""

    return (
        f"ERROR: Table identifier rejected: {fqn}\n"
        f"  Reason: {reason}\n"
        f"  Detail: {detail!r}\n"
        f"  Resolution: Either rename the identifier in the database, OR "
        f"exclude it via .dbprint.yaml selectors:\n"
        f"    exclude:\n"
        f'      - "{fqn}"'
    )


def _norm(name: str) -> str:
    """Lowercase an identifier - the artifact's path segment and map key (SPEC 1.3, 2.2.1)."""

    return name.lower()
