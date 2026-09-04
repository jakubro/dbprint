"""Redshift catalog reads: batched `SVV_REDSHIFT_*`/`STV_MV_INFO`, the standard PostgreSQL catalog
tables for constraints, and per-object `SHOW` for DDL - lowercased (SPEC 1.3).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from dbprint.config.selectors import expand
from .connection import exec_query
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


if TYPE_CHECKING:
    from .connection import Cursor


PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]*$")


class IdentifierRejected(ValueError):
    """Raised when a Redshift identifier fails SPEC 1.5 path-segment rules; format SPEC 1.5.5."""


_TABLE_TYPE_MAP: dict[str, TableType] = {
    "TABLE": "table",
    "VIEW": "view",
}

# `pg_constraint.confdeltype`/`confupdtype` codes - Redshift's own FK grammar has no
# referential-action clause, so a real cluster's rows are expected to always read 'a'.
_FK_ACTIONS: dict[str, FkAction] = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}

_Candidate = tuple[TableMeta, tuple[str, str]]


def list_tables(cursor: Cursor, include: list[str], exclude: list[str]) -> list[TableMeta]:
    """Enumerate tables/views in the database, filtered by selectors - `SVV_REDSHIFT_TABLES` cannot
    distinguish a materialized view, so `STV_MV_INFO` is joined in to override that one case.
    """

    rows = exec_query(
        cursor,
        """
        SELECT t.schema_name, t.table_name, t.table_type, mv.name IS NOT NULL AS is_matview
        FROM svv_redshift_tables t
        LEFT JOIN stv_mv_info mv
            ON mv.db_name = t.database_name
           AND mv.schema = t.schema_name
           AND mv.name = t.table_name
        WHERE t.database_name = current_database()
        ORDER BY t.schema_name, t.table_name
        """,
    ).fetchall()

    candidates: list[_Candidate] = []

    for schema, name, table_type, is_matview in rows:
        schema_lower = _norm(schema)

        if schema_lower in ("pg_catalog", "information_schema", "pg_internal", "catalog_history"):
            continue

        canonical_type = "matview" if is_matview else _TABLE_TYPE_MAP.get(str(table_type).upper())

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
    """Per-column metadata in ordinal order from `SVV_REDSHIFT_COLUMNS` - collation is not read,
    Redshift declaring it per database (SPEC 2.2.2); `database_name` filters out datashared columns.
    """

    schema, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT column_name, ordinal_position, data_type, is_nullable, column_default
        FROM svv_redshift_columns
        WHERE database_name = current_database() AND schema_name = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    ).fetchall()

    return [
        ColumnMeta(
            name=_norm(col_name),
            sql_type=str(data_type),
            nullable=_nullable(str(is_nullable)),
            default=col_default,
            ordinal=int(ordinal),
            physical_name=None if col_name == _norm(col_name) else col_name,
        )
        for col_name, ordinal, data_type, is_nullable, col_default in rows
    ]


def _nullable(raw: str) -> bool:
    """`is_nullable` has a documented third state, blank, meaning "no information" - only an
    explicit 'NO' (or a boolean spelling of it) makes the NOT NULL claim.
    """

    return raw.strip().upper() not in ("NO", "FALSE", "F")


def default_collation(cursor: Cursor) -> str:
    """The session's default collation (SPEC 2.2.2): `case_sensitive` or `case_insensitive`."""

    row = exec_query(cursor, "SELECT db_collation()").fetchone()

    return str(row[0]) if row and row[0] is not None else ""


def relationships(cursor: Cursor, fqn: str) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs, informational only: `detection` stays `declared` - the FK grammar
    carries no referential-action slot, so both rules are expected to always read `NO ACTION`.

    Read from `pg_constraint` rather than `SHOW CONSTRAINTS`, whose two forms share no column
    shape, so one composite key's rows would need reassembling across them.
    """

    schema, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT
            con.conname     AS constraint_name,
            con.conkey      AS src_attnums,
            con.confkey     AS dst_attnums,
            tn.nspname      AS dst_schema,
            tc.relname      AS dst_table,
            con.confdeltype AS on_delete,
            con.confupdtype AS on_update,
            con.conrelid    AS src_relid,
            con.confrelid   AS dst_relid
        FROM pg_constraint con
        JOIN pg_class      sc ON sc.oid = con.conrelid
        JOIN pg_namespace  sn ON sn.oid = sc.relnamespace
        JOIN pg_class      tc ON tc.oid = con.confrelid
        JOIN pg_namespace  tn ON tn.oid = tc.relnamespace
        WHERE con.contype = 'f'
          AND sn.nspname = %s
          AND sc.relname = %s
        ORDER BY con.conname
        """,
        (schema, table),
    ).fetchall()

    out: list[ForeignKeyMeta] = []

    for (
        name,
        src_attnums,
        dst_attnums,
        dst_schema,
        dst_table,
        on_del,
        on_upd,
        src_relid,
        dst_relid,
    ) in rows:
        out.append(
            ForeignKeyMeta(
                column=tuple(_attnums_to_names(cursor, src_relid, list(src_attnums))),
                target_table=f"{_norm(dst_schema)}.{_norm(dst_table)}",
                target_column=tuple(_attnums_to_names(cursor, dst_relid, list(dst_attnums))),
                on_delete=_FK_ACTIONS.get(str(on_del), "NO ACTION"),
                on_update=_FK_ACTIONS.get(str(on_upd), "NO ACTION"),
                constraint_name=name,
            ),
        )

    return out


def indexes(cursor: Cursor, fqn: str) -> list[IndexMeta]:
    """No index concept exists on Redshift; always empty."""

    del cursor, fqn

    return []


def unique_keys(cursor: Cursor, fqn: str) -> list[UniqueKeyMeta]:
    """Declared-unique column groups, PRIMARY first - read from `pg_constraint`, since
    `SHOW CONSTRAINTS` emits no UNIQUE rows and Redshift has no index to union in.
    """

    schema, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT con.conkey AS conkey, con.conrelid AS relid, con.contype AS contype,
               con.conname AS name
        FROM pg_constraint con
        JOIN pg_class     c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE con.contype IN ('p', 'u')
          AND n.nspname = %s
          AND c.relname = %s
        ORDER BY contype, name
        """,
        (schema, table),
    ).fetchall()

    return [
        UniqueKeyMeta(
            columns=tuple(_attnums_to_names(cursor, relid, list(conkey))),
            primary=contype == "p",
        )
        for conkey, relid, contype, _name in rows
    ]


def physical_layout(cursor: Cursor, fqn: str) -> PhysicalLayout | None:
    """Declared SORTKEY via `SVV_REDSHIFT_COLUMNS.sortkey`, None when none is declared -
    interleaved keys encode as alternating signs, so ordering is by `ABS(sortkey)`.
    """

    schema, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT column_name, sortkey
        FROM svv_redshift_columns
        WHERE database_name = current_database()
          AND schema_name = %s AND table_name = %s AND sortkey <> 0
        ORDER BY ABS(sortkey)
        """,
        (schema, table),
    ).fetchall()

    if not rows:
        return None

    return PhysicalLayout(
        mechanism="sort",
        keys=tuple(
            PhysicalLayoutKey(expression=_norm(name), column=_norm(name)) for name, _ in rows
        ),
    )


def view_dependencies(cursor: Cursor) -> dict[str, tuple[str, ...]]:
    """Every view's direct object dependencies, one query for the connection - a late-binding view
    has no `pg_rewrite` entry at all, which MUST NOT collapse into "resolved, reads nothing".
    """

    rows = exec_query(
        cursor,
        """
        SELECT DISTINCT
            vn.nspname AS view_schema,
            v.relname  AS view_name,
            r.oid IS NOT NULL AS resolved,
            sn.nspname AS source_schema,
            s.relname  AS source_name
        FROM pg_class v
        JOIN pg_namespace vn ON vn.oid = v.relnamespace
        LEFT JOIN pg_rewrite r ON r.ev_class = v.oid
        LEFT JOIN pg_depend dep ON dep.objid = r.oid
            AND dep.refobjsubid > 0
            AND dep.deptype = 'n'
        LEFT JOIN pg_class s ON s.oid = dep.refobjid
            AND s.oid <> v.oid
            AND s.relkind IN ('r', 'v', 'm')
        LEFT JOIN pg_namespace sn ON sn.oid = s.relnamespace
            AND sn.nspname NOT IN ('pg_catalog', 'information_schema')
        WHERE v.relkind IN ('v', 'm')
          AND vn.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY 1, 2, 3, 4, 5
        """,
    ).fetchall()

    out: dict[str, list[str]] = {}

    for view_schema, view_name, resolved, source_schema, source_name in rows:
        if not resolved:
            continue

        key = f"{_norm(view_schema)}.{_norm(view_name)}"
        out.setdefault(key, [])

        if source_schema is not None and source_name is not None:
            out[key].append(f"{_norm(source_schema)}.{_norm(source_name)}")

    return {k: tuple(v) for k, v in out.items()}


def comments(cursor: Cursor, fqn: str) -> CommentsMeta:
    """Table comment + per-column comments from `PG_DESCRIPTION`, "fully accessible" per AWS."""

    schema, table = _split_fqn(fqn)
    table_row = exec_query(
        cursor,
        """
        SELECT d.description
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = 0
        WHERE n.nspname = %s AND c.relname = %s
        """,
        (schema, table),
    ).fetchone()
    table_comment = table_row[0] if table_row else None

    col_rows = exec_query(
        cursor,
        """
        SELECT a.attname, d.description
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = a.attnum
        WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0
        """,
        (schema, table),
    ).fetchall()

    return CommentsMeta(
        table=table_comment,
        columns={_norm(name): desc for name, desc in col_rows if desc is not None},
    )


def estimate_row_count(cursor: Cursor, fqn: str) -> int:
    """`SVV_TABLE_INFO.estimated_visible_rows`; -1 when refused - needs a superuser role or a
    `GRANT SELECT` on the view - or when an empty table is simply missing from it, not zero.
    """

    schema, table = _split_fqn(fqn)

    try:
        row = exec_query(
            cursor,
            'SELECT estimated_visible_rows FROM svv_table_info WHERE schema = %s AND "table" = %s',
            (schema, table),
        ).fetchone()
    except Exception:  # noqa: BLE001 - refused without a superuser role or a grant on the view
        return -1

    if not row or row[0] is None:
        return -1

    return int(row[0])


def table_rows_estimate(cursor: Cursor, fqn: str) -> int:
    """Alias kept for `looks_like.py`'s naming parity with the other adapters."""

    return estimate_row_count(cursor, fqn)


def resolve_column(cursor: Cursor, fqn: str, column: str) -> str:
    """The catalog's own spelling for a lowercased column name (SPEC 2.2.1's map key).

    The only way to address a mixed-case column under `enable_case_sensitive_identifier`.
    """

    schema, table = _split_fqn(fqn)
    row = exec_query(
        cursor,
        """
        SELECT column_name
        FROM svv_redshift_columns
        WHERE database_name = current_database()
          AND schema_name = %s AND table_name = %s AND LOWER(column_name) = %s
        """,
        (schema, table, column),
    ).fetchone()

    if row is None:
        raise KeyError(f"no column named {column!r} (case-insensitive) on {fqn!r}")

    return str(row[0])


def _attnums_to_names(cursor: Cursor, relid: int, attnums: list[int]) -> list[str]:
    """Resolve attnums for one relation into lowercased column names, order preserved - lowercase
    agrees with the `columns` map key (SPEC 2.2.1), and nothing quotes these into a statement.
    """

    if not attnums:
        return []

    # An explicit `IN` list, not `= ANY(<array>)`: AWS lists array constructors among the
    # PostgreSQL features Redshift does not support. Every bound value is an int from the catalog.
    placeholders = ", ".join(["%s"] * len(attnums))
    rows = exec_query(
        cursor,
        f"""
        SELECT attnum, attname
        FROM pg_attribute
        WHERE attrelid = %s AND attnum IN ({placeholders}) AND NOT attisdropped
        """,
        (relid, *attnums),
    ).fetchall()
    name_by_attnum = {int(attnum): str(attname).lower() for attnum, attname in rows}

    return [name_by_attnum[a] for a in attnums if a in name_by_attnum]


def _enforce_identifier_rules(selected: list[_Candidate]) -> None:
    """Reject identifiers that violate SPEC 1.5 before any artifact is written - two names
    differing only by case collapse onto one path, so one would overwrite the other.
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
        raise ValueError(f"Redshift FQN must be 'schema.table', got {fqn!r}")

    schema, _, table = fqn.partition(".")

    return schema, table


def _norm(name: str) -> str:
    """Lowercase an identifier - the `columns` map key (SPEC 2.2.1)."""

    return name.lower()
