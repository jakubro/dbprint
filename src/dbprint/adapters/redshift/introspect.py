"""Redshift catalog reads: batched `SVV_*`/`STV_MV_INFO`, the standard PostgreSQL catalog
tables for constraints, and per-object `SHOW` for DDL - lowercased (SPEC 1.3).

The catalog stores a quoted `CREATE`'s case, so reads past enumeration bind `Identity`'s spelling.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING

from dbprint.spec.fqn import join as join_fqn
from .connection import exec_query
from .. import pg_catalog
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
from ..identifiers import Identity, column_meta, fold, select_tables, table_meta
from ..sql_layout import listed


if TYPE_CHECKING:
    from .connection import Cursor


_TABLE_TYPE_MAP: dict[str, TableType] = {
    "TABLE": "table",
    "VIEW": "view",
}

_Candidate = tuple[TableMeta, tuple[str, str, str]]

# System databases Redshift will not let a user drop or create tables in.
_SYSTEM_DATABASES = ("template0", "template1", "padb_harvest", "sys:internal")


def list_tables(
    cursor: Cursor,
    databases: Sequence[str],
    include: list[str],
    exclude: list[str],
) -> list[_Candidate]:
    """Enumerate tables/views in `databases`, filtered by selectors - `SVV_REDSHIFT_TABLES` cannot
    distinguish a materialized view, so `STV_MV_INFO` is joined in to override that one case.
    """

    placeholders = listed(["%s"] * len(databases), 12)
    rows = exec_query(
        cursor,
        f"""
        SELECT
          tbl.database_name,
          tbl.schema_name,
          tbl.table_name,
          tbl.table_type,
          mvi.name IS NOT NULL AS is_matview
        FROM
          svv_redshift_tables tbl
          LEFT JOIN stv_mv_info mvi ON
            mvi.db_name = tbl.database_name
            AND mvi.schema = tbl.schema_name
            AND mvi.name = tbl.table_name
        WHERE
          tbl.database_name IN (
            {placeholders}
          )
        ORDER BY
          tbl.database_name, tbl.schema_name, tbl.table_name
        """,
        tuple(databases),
    ).fetchall()

    candidates: list[_Candidate] = []

    for database, schema, name, table_type, is_matview in rows:
        schema_lower = fold(schema)

        if schema_lower in ("pg_catalog", "information_schema", "pg_internal", "catalog_history"):
            continue

        canonical_type = "matview" if is_matview else _TABLE_TYPE_MAP.get(str(table_type).upper())

        if canonical_type is None:
            continue

        physical = (database, schema, name)
        candidates.append((table_meta(physical, canonical_type), physical))

    candidates += [
        (table_meta(physical, "table", external=True), physical)
        for physical in _external_tables(cursor, databases)
    ]
    selected = select_tables(candidates, include, exclude)

    return selected


def external_columns(cursor: Cursor, identity: Identity) -> list[ColumnMeta]:
    """An external table's columns from `SVV_EXTERNAL_COLUMNS`, `external_type` as the type."""

    rows = exec_query(
        cursor,
        """
        SELECT
          col.columnname,
          col.columnnum,
          col.external_type,
          col.is_nullable
        FROM
          svv_external_columns col
        WHERE
          col.redshift_database_name = CURRENT_DATABASE()
          AND col.schemaname = %s
          AND col.tablename = %s
        ORDER BY
          col.columnnum
        """,
        identity.addressed,
    ).fetchall()

    return [
        column_meta(
            col_name,
            sql_type=str(external_type),
            nullable=_nullable(str(is_nullable)),
            default=None,
            ordinal=int(ordinal),
        )
        for col_name, ordinal, external_type, is_nullable in rows
    ]


def external_physical_layout(cursor: Cursor, identity: Identity) -> PhysicalLayout | None:
    """An external table's partition columns in `part_key` order, None when it has none."""

    rows = exec_query(
        cursor,
        """
        SELECT
          col.columnname
        FROM
          svv_external_columns col
        WHERE
          col.redshift_database_name = CURRENT_DATABASE()
          AND col.schemaname = %s
          AND col.tablename = %s
          AND col.part_key > 0
        ORDER BY
          col.part_key
        """,
        identity.addressed,
    ).fetchall()

    if not rows:
        return None

    return PhysicalLayout(
        mechanism="partition",
        keys=tuple(PhysicalLayoutKey(expression=fold(name), column=fold(name)) for (name,) in rows),
    )


def columns(cursor: Cursor, identity: Identity) -> list[ColumnMeta]:
    """Per-column metadata in ordinal order from `SVV_REDSHIFT_COLUMNS` - collation is not read,
    Redshift declaring it per database (SPEC 2.2.2); `database_name` filters out datashared columns.
    """

    rows = exec_query(
        cursor,
        """
        SELECT
          col.column_name,
          col.ordinal_position,
          col.data_type,
          col.is_nullable,
          col.column_default
        FROM
          svv_redshift_columns col
        WHERE
          col.database_name = CURRENT_DATABASE()
          AND col.schema_name = %s
          AND col.table_name = %s
        ORDER BY
          col.ordinal_position
        """,
        identity.addressed,
    ).fetchall()

    return [
        column_meta(
            col_name,
            sql_type=str(data_type),
            nullable=_nullable(str(is_nullable)),
            default=col_default,
            ordinal=int(ordinal),
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

    row = exec_query(cursor, "SELECT DB_COLLATION()").fetchone()

    return str(row[0]) if row and row[0] is not None else ""


def relationships(cursor: Cursor, identity: Identity) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs, informational only: `detection` stays `declared` - the FK grammar
    carries no referential-action slot, so both rules are expected to always read `NO ACTION`.

    Read from `pg_constraint` rather than `SHOW CONSTRAINTS`, whose two forms share no column
    shape, so one composite key's rows would need reassembling across them.
    """

    return pg_catalog.relationships(partial(exec_query, cursor), identity, on_unknown="NO ACTION")


def indexes(cursor: Cursor, identity: Identity) -> list[IndexMeta]:
    """No index concept exists on Redshift; always empty."""

    del cursor, identity

    return []


def unique_keys(cursor: Cursor, identity: Identity) -> list[UniqueKeyMeta]:
    """Declared-unique column groups, PRIMARY first - read from `pg_constraint`, since
    `SHOW CONSTRAINTS` emits no UNIQUE rows and Redshift has no index to union in.
    """

    rows = exec_query(
        cursor,
        """
        SELECT
          con.conkey AS conkey,
          con.conrelid AS relid,
          con.contype AS contype,
          con.conname AS name
        FROM
          pg_constraint con
          JOIN pg_class cls ON cls.oid = con.conrelid
          JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
        WHERE
          con.contype IN ('p', 'u')
          AND nsp.nspname = %s
          AND cls.relname = %s
        ORDER BY
          contype, name
        """,
        identity.addressed,
    ).fetchall()

    return [
        UniqueKeyMeta(
            columns=tuple(
                pg_catalog.attnums_to_names(partial(exec_query, cursor), relid, list(conkey)),
            ),
            primary=contype == "p",
        )
        for conkey, relid, contype, _name in rows
    ]


def physical_layout(cursor: Cursor, identity: Identity) -> PhysicalLayout | None:
    """Declared SORTKEY via `SVV_REDSHIFT_COLUMNS.sortkey`, None when none is declared -
    interleaved keys encode as alternating signs, so ordering is by `ABS(sortkey)`.
    """

    rows = exec_query(
        cursor,
        """
        SELECT
          col.column_name,
          col.sortkey
        FROM
          svv_redshift_columns col
        WHERE
          col.database_name = CURRENT_DATABASE()
          AND col.schema_name = %s
          AND col.table_name = %s
          AND col.sortkey <> 0
        ORDER BY
          ABS(col.sortkey)
        """,
        identity.addressed,
    ).fetchall()

    if not rows:
        return None

    return PhysicalLayout(
        mechanism="sort",
        keys=tuple(PhysicalLayoutKey(expression=fold(name), column=fold(name)) for name, _ in rows),
    )


def list_databases(cursor: Cursor) -> tuple[str, ...]:
    """Local databases the user can access, less the system ones - a datashare's consumer
    database is left out, its connectability unverified.
    """

    rows = exec_query(
        cursor,
        """
        SELECT
          dbs.database_name
        FROM
          svv_redshift_databases dbs
        WHERE
          dbs.database_type = 'local'
        ORDER BY
          dbs.database_name
        """,
    ).fetchall()

    return tuple(str(name) for (name,) in rows if str(name) not in _SYSTEM_DATABASES)


def view_dependencies(cursor: Cursor, database: str) -> dict[str, tuple[str, ...]]:
    """Every view's direct object dependencies in `database`, read on that database's own session -
    a late-binding view has no `pg_rewrite` entry, which MUST NOT collapse into "reads nothing".
    """

    rows = exec_query(
        cursor,
        """
        SELECT DISTINCT
          vnp.nspname AS view_schema,
          vew.relname AS view_name,
          rwr.oid IS NOT NULL AS resolved,
          snp.nspname AS source_schema,
          scl.relname AS source_name

        FROM
          pg_class vew
          JOIN pg_namespace vnp ON vnp.oid = vew.relnamespace
          LEFT JOIN pg_rewrite rwr ON rwr.ev_class = vew.oid
          LEFT JOIN pg_depend dep ON
            dep.objid = rwr.oid
            AND dep.refobjsubid > 0
            AND dep.deptype = 'n'
          LEFT JOIN pg_class scl ON
            scl.oid = dep.refobjid
            AND scl.oid <> vew.oid
            AND scl.relkind IN ('r', 'v', 'm')
          LEFT JOIN pg_namespace snp ON
            snp.oid = scl.relnamespace
            AND snp.nspname NOT IN ('pg_catalog', 'information_schema')

        WHERE
          vew.relkind IN ('v', 'm')
          AND vnp.nspname NOT IN ('pg_catalog', 'information_schema')

        ORDER BY
          1, 2, 3, 4, 5
        """,
    ).fetchall()

    out: dict[str, list[str]] = {}

    for view_schema, view_name, resolved, source_schema, source_name in rows:
        if not resolved:
            continue

        key = join_fqn((fold(database), fold(view_schema), fold(view_name)))
        out.setdefault(key, [])

        if source_schema is not None and source_name is not None:
            out[key].append(join_fqn((fold(database), fold(source_schema), fold(source_name))))

    return {k: tuple(v) for k, v in out.items()}


def comments(cursor: Cursor, identity: Identity) -> CommentsMeta:
    """Table comment + per-column comments from `PG_DESCRIPTION`, "fully accessible" per AWS."""

    table_row = exec_query(
        cursor,
        """
        SELECT
          dsc.description
        FROM
          pg_class cls
          JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
          LEFT JOIN pg_description dsc ON
            dsc.objoid = cls.oid
            AND dsc.objsubid = 0
        WHERE
          nsp.nspname = %s
          AND cls.relname = %s
        """,
        identity.addressed,
    ).fetchone()
    table_comment = table_row[0] if table_row else None

    col_rows = exec_query(
        cursor,
        """
        SELECT
          att.attname,
          dsc.description
        FROM
          pg_attribute att
          JOIN pg_class cls ON cls.oid = att.attrelid
          JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
          JOIN pg_description dsc ON
            dsc.objoid = cls.oid
            AND dsc.objsubid = att.attnum
        WHERE
          nsp.nspname = %s
          AND cls.relname = %s
          AND att.attnum > 0
        """,
        identity.addressed,
    ).fetchall()

    return CommentsMeta(
        table=table_comment,
        columns={fold(name): desc for name, desc in col_rows if desc is not None},
    )


def estimate_row_count(cursor: Cursor, identity: Identity) -> int:
    """`SVV_TABLE_INFO.estimated_visible_rows`; -1 when refused - needs a superuser role or a
    `GRANT SELECT` on the view - or when an empty table is simply missing from it, not zero.
    """

    try:
        row = exec_query(
            cursor,
            """
            SELECT
              inf.estimated_visible_rows
            FROM
              svv_table_info inf
            WHERE
              inf.schema = %s
              AND inf."table" = %s
            """,
            identity.addressed,
        ).fetchone()
    except Exception:  # noqa: BLE001 - refused without a superuser role or a grant on the view
        return -1

    if not row or row[0] is None:
        return -1

    return int(row[0])


def table_rows_estimate(cursor: Cursor, identity: Identity) -> int:
    """Alias kept for `looks_like.py`'s naming parity with the other adapters."""

    return estimate_row_count(cursor, identity)


def _external_tables(cursor: Cursor, databases: Sequence[str]) -> list[tuple[str, str, str]]:
    """External tables of `databases` - `tabletype` also names external views, which are not."""

    placeholders = listed(["%s"] * len(databases), 12)
    rows = exec_query(
        cursor,
        f"""
        SELECT
          ext.redshift_database_name,
          ext.schemaname,
          ext.tablename
        FROM
          svv_external_tables ext
        WHERE
          ext.redshift_database_name IN (
            {placeholders}
          )
          AND COALESCE(TRIM(ext.tabletype), '') IN ('TABLE', '')
        ORDER BY
          ext.redshift_database_name, ext.schemaname, ext.tablename
        """,
        tuple(databases),
    ).fetchall()

    return [(str(database), str(schema), str(name)) for database, schema, name in rows]
