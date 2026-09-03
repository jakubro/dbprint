"""INFORMATION_SCHEMA queries for structural metadata, one function per intermediate type.

Every function past enumeration binds the physical identifiers the catalog reported; the
engine's lowercased path segments would match zero rows and read as an empty table. System
schemas are excluded from `list_tables` regardless of selectors.
"""

from __future__ import annotations

import re
from typing import Any, cast

from dbprint.config.selectors import expand
from .connection import Cursor, exec_query
from .identity import Identity, quote_ident
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
    """Raised when an identifier fails SPEC 1.5 path-segment rules; format is SPEC 1.5.5."""


# The spellings information_schema.tables.table_type actually reports for Snowflake.
_TABLE_TYPE_MAP: dict[str, TableType] = {
    "BASE TABLE": "table",
    "EXTERNAL TABLE": "table",
    "TEMPORARY TABLE": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "matview",
}

_FK_ACTIONS: dict[str, FkAction] = {
    "NO ACTION": "NO ACTION",
    "CASCADE": "CASCADE",
    "SET NULL": "SET NULL",
    "SET DEFAULT": "SET DEFAULT",
    "RESTRICT": "RESTRICT",
}

# Only Snowflake's own system schema; a MAIN or PG_CATALOG here is a user schema to profile.
_SYSTEM_SCHEMAS = ("information_schema",)

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
    include: list[str],
    exclude: list[str],
) -> tuple[list[TableMeta], dict[str, tuple[str, str, str]]]:
    """Enumerate tables/views/matviews in user schemas, filtered by selectors.

    Also returns the lowercased-FQN-to-physical map; this is the only point where both
    forms are visible to capture.
    """

    rows = exec_query(
        cursor,
        """
        SELECT table_catalog, table_schema, table_name, table_type
        FROM information_schema.tables
        ORDER BY table_catalog, table_schema, table_name
        """,
    ).fetchall()

    candidates: list[_Candidate] = []

    for catalog, schema, name, table_type in rows:
        if schema.lower() in _SYSTEM_SCHEMAS:
            continue

        canonical_type = _TABLE_TYPE_MAP.get(table_type)

        if canonical_type is None:
            continue

        path = (catalog.lower(), schema.lower(), name.lower())
        meta = TableMeta(fqn=".".join(path), type=canonical_type, namespace_path=path)
        candidates.append((meta, (catalog, schema, name)))

    in_scope = set(
        expand(
            [meta.fqn for meta, _ in candidates],
            config_include=include,
            config_exclude=exclude,
        ),
    )
    selected = [entry for entry in candidates if entry[0].fqn in in_scope]
    _enforce_identifier_rules(selected)

    return [meta for meta, _ in selected], {meta.fqn: parts for meta, parts in selected}


def columns(cursor: Cursor, identity: Identity) -> tuple[list[ColumnMeta], dict[str, str]]:
    """Per-column structural metadata in ordinal order.

    Returns the metadata plus a lowercase-to-physical column-name map, which later
    statements need to quote the real identifiers. `collation_name` is NULL for a column
    with no explicit `COLLATE` (SPEC 2.2.2), and Snowflake has no default to fill in.
    """

    rows = exec_query(
        cursor,
        """
        SELECT column_name, ordinal_position, data_type, is_nullable, column_default,
               collation_name
        FROM information_schema.columns
        WHERE table_catalog = ? AND table_schema = ? AND table_name = ?
        ORDER BY ordinal_position
        """,
        identity.parts,
    ).fetchall()

    metas = [
        ColumnMeta(
            name=col_name.lower(),
            sql_type=data_type,
            nullable=(is_nullable == "YES"),
            default=col_default,
            ordinal=int(ordinal),
            physical_name=None if col_name == col_name.lower() else col_name,
            collation=collation_name,
        )
        for col_name, ordinal, data_type, is_nullable, col_default, collation_name in rows
    ]

    return metas, {row[0].lower(): row[0] for row in rows}


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
        target = ".".join(
            str(head[index]).lower() for index in (_IK_PK_DATABASE, _IK_PK_SCHEMA, _IK_PK_TABLE)
        )

        out.append(
            ForeignKeyMeta(
                column=tuple(str(r[_IK_FK_COLUMN]).lower() for r in ordered),
                target_table=target,
                target_column=tuple(str(r[_IK_PK_COLUMN]).lower() for r in ordered),
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
        """
        SELECT i.name, i.is_unique, c.name
        FROM information_schema.indexes i
        JOIN information_schema.index_columns c
          ON c.table_catalog = i.table_catalog
         AND c.table_schema = i.table_schema
         AND c.table_name = i.table_name
         AND c.index_name = i.name
        WHERE i.table_catalog = ? AND i.table_schema = ? AND i.table_name = ?
        ORDER BY i.name, c.key_sequence
        """,
        identity.parts,
    ).fetchall()

    grouped: dict[str, tuple[bool, list[str]]] = {}

    for index_name, is_unique, column_name in rows:
        _unique, index_columns = grouped.setdefault(index_name, (_is_true(is_unique), []))
        index_columns.append(column_name.lower())

    return [
        IndexMeta(name=name.lower(), columns=tuple(cols), unique=unique, type="btree")
        for name, (unique, cols) in grouped.items()
    ]


def comments(cursor: Cursor, identity: Identity) -> CommentsMeta:
    """Table comment + per-column comments from INFORMATION_SCHEMA."""

    table_row = exec_query(
        cursor,
        """
        SELECT comment FROM information_schema.tables
        WHERE table_catalog = ? AND table_schema = ? AND table_name = ?
        """,
        identity.parts,
    ).fetchone()
    table_comment = table_row[0] if table_row else None

    col_rows = exec_query(
        cursor,
        """
        SELECT column_name, comment FROM information_schema.columns
        WHERE table_catalog = ? AND table_schema = ? AND table_name = ?
        """,
        identity.parts,
    ).fetchall()

    return CommentsMeta(
        table=table_comment,
        columns={
            col_name.lower(): comment for col_name, comment in col_rows if comment is not None
        },
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
                    columns=tuple(str(r[_KEY_COLUMN]).lower() for r in ordered),
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

    schema_ref = f"{quote_ident(identity.database)}.{quote_ident(identity.schema)}"
    rows = exec_query(cursor, f"SHOW TABLES IN SCHEMA {schema_ref}").fetchall()

    for row in rows:
        if str(row[_SHOW_TABLES_NAME]).lower() != identity.table.lower():
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
        column=match.group(1).lower() if match else None,
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


def view_dependencies(cursor: Cursor, database: str) -> dict[str, tuple[str, ...]]:
    """Every view/matview's direct object dependencies, for the whole connection - two
    statements, the second reading an account-wide catalog that lags up to three hours.
    """

    view_rows = exec_query(
        cursor,
        """
        SELECT table_catalog, table_schema, table_name
        FROM information_schema.tables
        WHERE table_type IN ('VIEW', 'MATERIALIZED VIEW')
        """,
    ).fetchall()

    out: dict[str, list[str]] = {
        f"{catalog.lower()}.{schema.lower()}.{name.lower()}": []
        for catalog, schema, name in view_rows
    }

    dep_rows = exec_query(
        cursor,
        """
        SELECT
            referencing_database, referencing_schema, referencing_object_name,
            referenced_database, referenced_schema, referenced_object_name
        FROM snowflake.account_usage.object_dependencies
        WHERE referencing_object_domain IN ('VIEW', 'MATERIALIZED VIEW')
          AND referenced_object_domain IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW', 'EXTERNAL TABLE')
          AND UPPER(referencing_database) = UPPER(?)
        """,
        [database],
    ).fetchall()

    for view_db, view_schema, view_name, source_db, source_schema, source_name in dep_rows:
        key = f"{view_db.lower()}.{view_schema.lower()}.{view_name.lower()}"
        out.setdefault(key, []).append(
            f"{source_db.lower()}.{source_schema.lower()}.{source_name.lower()}",
        )

    return {k: tuple(v) for k, v in out.items()}


def row_count_estimate(cursor: Cursor, identity: Identity) -> int:
    """Catalog row count for the table, never a scan; -1 when the catalog has no entry."""

    row = exec_query(
        cursor,
        """
        SELECT row_count FROM information_schema.tables
        WHERE table_catalog = ? AND table_schema = ? AND table_name = ?
        """,
        identity.parts,
    ).fetchone()

    if not row or row[0] is None:
        return -1

    return int(row[0])


def _enforce_identifier_rules(selected: list[_Candidate]) -> None:
    """Reject identifiers that violate SPEC 1.5 before any artifact is written.

    Two objects differing only by case collapse onto one path, the second overwriting it.
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


def _is_true(value: Any) -> bool:
    """Normalize a catalog boolean that may arrive as a bool or a YES/NO string."""

    if isinstance(value, str):
        return value.strip().upper() in ("YES", "TRUE", "Y")

    return bool(value)
