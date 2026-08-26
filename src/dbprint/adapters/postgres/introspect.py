"""pg_catalog queries for structural metadata.

`list_tables` excludes system schemas and partitioning children (`relispartition`)
regardless of selectors: a partition is a fragment of its parent's logical table.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

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
    import psycopg


PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]*$")


class IdentifierRejected(ValueError):
    """Raised when a Postgres identifier fails SPEC 1.5 path-segment rules; format SPEC 1.5.5."""


_RELKIND_TO_TYPE: dict[str, TableType] = {
    "r": "table",
    "p": "table",  # partitioned table
    "v": "view",
    "m": "matview",
}

_FK_ACTIONS = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}

_Candidate = tuple[TableMeta, tuple[str, str]]


def list_tables(
    conn: psycopg.Connection,
    include: list[str],
    exclude: list[str],
) -> list[TableMeta]:
    """Enumerate tables/views/matviews in user schemas, filtered by selectors."""

    rows = exec_query(
        conn,
        """
        SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'v', 'm')
          AND NOT c.relispartition
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND n.nspname NOT LIKE 'pg_temp_%'
        ORDER BY n.nspname, c.relname
        """,
    ).fetchall()

    candidates: list[_Candidate] = [
        (
            TableMeta(
                fqn=f"{schema.lower()}.{name.lower()}",
                type=_RELKIND_TO_TYPE[kind],
                namespace_path=(schema.lower(), name.lower()),
            ),
            (schema, name),
        )
        for schema, name, kind in rows
    ]
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


def columns(conn: psycopg.Connection, fqn: str) -> list[ColumnMeta]:
    """Per-column structural metadata in ordinal order.

    `name` is lowercased for the artifact's map key (SPEC 2.2.1), with the catalog's own
    spelling carried as `physical_name` when the two differ. `collation` comes from
    `information_schema.columns`, which reports NULL for a type's default collation and a
    name only when one was set explicitly - the omit-unless-it-differs rule of SPEC 2.2.2.
    """

    schema, table = _split_fqn(fqn)
    rows = exec_query(
        conn,
        """
        SELECT
            a.attname                                              AS name,
            pg_catalog.format_type(a.atttypid, a.atttypmod)        AS sql_type,
            NOT a.attnotnull                                       AS nullable,
            pg_get_expr(d.adbin, d.adrelid)                        AS default_expr,
            a.attnum                                               AS ordinal,
            isc.collation_name                                     AS collation_name
        FROM pg_attribute a
        JOIN pg_class c       ON c.oid = a.attrelid
        JOIN pg_namespace n   ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        LEFT JOIN information_schema.columns isc
               ON isc.table_schema = n.nspname
              AND isc.table_name = c.relname
              AND isc.column_name = a.attname
        WHERE n.nspname = %s
          AND c.relname = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (schema, table),
    ).fetchall()

    return [
        ColumnMeta(
            name=name.lower(),
            sql_type=sql_type,
            nullable=nullable,
            default=default,
            ordinal=ordinal,
            physical_name=None if name == name.lower() else name,
            collation=collation_name,
        )
        for name, sql_type, nullable, default, ordinal, collation_name in rows
    ]


def composite_columns(conn: psycopg.Connection, fqn: str) -> frozenset[str]:
    """Lowercased names of columns whose type resolves to a composite (row) type.

    A domain chain resolves to its ultimate base type first: a domain over a composite is
    still a composite for SPEC 3.1's representability boundary. The test is the catalog's
    `pg_type.typtype = 'c'`, since a composite type's name is whatever its author chose.
    """

    schema, table = _split_fqn(fqn)
    rows = exec_query(
        conn,
        """
        WITH RECURSIVE base_type AS (
            SELECT a.attname AS name, a.atttypid AS oid
            FROM pg_attribute a
            JOIN pg_class c     ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s
              AND c.relname = %s
              AND a.attnum > 0
              AND NOT a.attisdropped
            UNION ALL
            SELECT bt.name, t.typbasetype
            FROM base_type bt
            JOIN pg_type t ON t.oid = bt.oid
            WHERE t.typtype = 'd' AND t.typbasetype <> 0
        )
        SELECT DISTINCT bt.name
        FROM base_type bt
        JOIN pg_type t ON t.oid = bt.oid
        WHERE t.typtype = 'c'
        """,
        (schema, table),
    ).fetchall()

    return frozenset(name.lower() for (name,) in rows)


def default_collation(conn: psycopg.Connection) -> str:
    """The current database's default collation (SPEC 2.2.2) - one scalar, once per run."""

    row = exec_query(
        conn,
        "SELECT datcollate FROM pg_database WHERE datname = current_database()",
    ).fetchone()

    return row[0] if row else ""


def relationships(conn: psycopg.Connection, fqn: str) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs; one entry per constraint (composite as arrays)."""

    schema, table = _split_fqn(fqn)
    rows = exec_query(
        conn,
        """
        SELECT
            con.conname                  AS constraint_name,
            con.conkey                   AS src_attnums,
            con.confkey                  AS dst_attnums,
            tn.nspname                   AS dst_schema,
            tc.relname                   AS dst_table,
            con.confdeltype              AS on_delete,
            con.confupdtype              AS on_update,
            con.conrelid                 AS src_relid,
            con.confrelid                AS dst_relid
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
        src_cols = _attnums_to_names(conn, src_relid, src_attnums)
        dst_cols = _attnums_to_names(conn, dst_relid, dst_attnums)
        out.append(
            ForeignKeyMeta(
                column=tuple(src_cols),
                target_table=f"{dst_schema.lower()}.{dst_table.lower()}",
                target_column=tuple(dst_cols),
                on_delete=cast(FkAction, _FK_ACTIONS[on_del]),
                on_update=cast(FkAction, _FK_ACTIONS[on_upd]),
                constraint_name=name,
            ),
        )

    return out


def indexes(conn: psycopg.Connection, fqn: str) -> list[IndexMeta]:
    """Secondary indexes only; PK-backed, constraint-backed and bare-unique indexes excluded.

    A bare unique index (`indisunique`, no backing `pg_constraint`, `indpred IS NULL`) is
    reported by `unique_keys` instead. A partial unique index (`indpred IS NOT NULL`) stays
    here: it enforces uniqueness over a subset of rows, which `unique_keys` does not report.
    """

    schema, table = _split_fqn(fqn)
    rows = exec_query(
        conn,
        """
        SELECT
            ic.relname                                              AS index_name,
            string_to_array(ix.indkey::text, ' ')::int[]            AS attnums,
            ix.indisunique                                          AS is_unique,
            am.amname                                               AS index_type,
            ix.indrelid                                             AS table_relid
        FROM pg_index    ix
        JOIN pg_class    ic ON ic.oid = ix.indexrelid
        JOIN pg_class    tc ON tc.oid = ix.indrelid
        JOIN pg_namespace n ON n.oid = tc.relnamespace
        JOIN pg_am       am ON am.oid = ic.relam
        WHERE n.nspname = %s
          AND tc.relname = %s
          AND NOT ix.indisprimary
          AND NOT EXISTS (
            SELECT 1 FROM pg_constraint con
            WHERE con.conindid = ix.indexrelid AND con.contype IN ('p', 'u')
          )
          AND NOT (ix.indisunique AND ix.indpred IS NULL)
        ORDER BY ic.relname
        """,
        (schema, table),
    ).fetchall()

    out: list[IndexMeta] = []

    for index_name, attnums, is_unique, index_type, table_relid in rows:
        cols = _attnums_to_names(conn, table_relid, list(attnums))
        out.append(
            IndexMeta(
                name=index_name,
                columns=tuple(cols),
                unique=is_unique,
                type=index_type,
            ),
        )

    return out


def comments(conn: psycopg.Connection, fqn: str) -> CommentsMeta:
    """Table comment + per-column comments from pg_description."""

    schema, table = _split_fqn(fqn)
    table_row = exec_query(
        conn,
        """
        SELECT d.description
        FROM pg_class c
        JOIN pg_namespace n   ON n.oid = c.relnamespace
        LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = 0
        WHERE n.nspname = %s AND c.relname = %s
        """,
        (schema, table),
    ).fetchone()
    table_comment = table_row[0] if table_row else None

    col_rows = exec_query(
        conn,
        """
        SELECT a.attname, d.description
        FROM pg_attribute a
        JOIN pg_class c        ON c.oid = a.attrelid
        JOIN pg_namespace n    ON n.oid = c.relnamespace
        JOIN pg_description d  ON d.objoid = c.oid AND d.objsubid = a.attnum
        WHERE n.nspname = %s
          AND c.relname = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
        """,
        (schema, table),
    ).fetchall()

    return CommentsMeta(
        table=table_comment,
        columns={
            name.lower(): description for name, description in col_rows if description is not None
        },
    )


def unique_keys(conn: psycopg.Connection, fqn: str) -> list[UniqueKeyMeta]:
    """Declared-unique column groups: primary key, unique constraints, bare unique indexes.

    `contype` sorts 'p' before 'u', so the primary key both leads and is marked; `conkey`
    is the attnum sequence in declaration order. The second arm adds a bare unique index
    backing no constraint - "declared unique" means enforced, not named - excluding partial
    indexes and any a constraint already covers. No relkind filter, so a matview's bare
    unique index is picked up too.
    """

    schema, table = _split_fqn(fqn)
    rows = exec_query(
        conn,
        """
        SELECT con.conkey::int[] AS conkey, con.conrelid AS relid, con.contype AS contype,
               con.conname AS name
        FROM pg_constraint con
        JOIN pg_class     c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE con.contype IN ('p', 'u')
          AND n.nspname = %s
          AND c.relname = %s

        UNION ALL

        SELECT string_to_array(ix.indkey::text, ' ')::int[] AS conkey, ix.indrelid AS relid,
               'u' AS contype, ic.relname AS name
        FROM pg_index     ix
        JOIN pg_class     ic ON ic.oid = ix.indexrelid
        JOIN pg_class     c  ON c.oid = ix.indrelid
        JOIN pg_namespace n  ON n.oid = c.relnamespace
        WHERE ix.indisunique
          AND NOT ix.indisprimary
          AND ix.indpred IS NULL
          AND n.nspname = %s
          AND c.relname = %s
          AND NOT EXISTS (
            SELECT 1 FROM pg_constraint con2
            WHERE con2.conindid = ix.indexrelid AND con2.contype IN ('p', 'u')
          )

        ORDER BY contype, name
        """,
        (schema, table, schema, table),
    ).fetchall()

    return [
        UniqueKeyMeta(
            columns=tuple(_attnums_to_names(conn, relid, list(conkey))),
            primary=contype == "p",
        )
        for conkey, relid, contype, _name in rows
    ]


_PARTKEYDEF_RE = re.compile(r"^(?:RANGE|LIST|HASH)\s*\((.*)\)$", re.IGNORECASE)
# A bare identifier, optionally quoted: the base column a predicate would filter on. A
# function call (`date_trunc(...)`) does not match, so `column` comes back None for one.
_BASE_COLUMN_RE = re.compile(r'^"?([A-Za-z_][A-Za-z0-9_$]*)"?$')


def physical_layout(conn: psycopg.Connection, fqn: str) -> PhysicalLayout | None:
    """Declared partition key via `pg_get_partkeydef`; None on a non-partitioned table.

    Only the partitioned parent (`relkind = 'p'`) carries a key, and dbprint profiles the
    parent rather than its individual partitions.
    """

    schema, table = _split_fqn(fqn)
    row = exec_query(
        conn,
        """
        SELECT pg_get_partkeydef(c.oid)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'p'
        """,
        (schema, table),
    ).fetchone()

    if not row or not row[0]:
        return None

    return _parse_partkeydef(row[0])


def _parse_partkeydef(value: str) -> PhysicalLayout:
    match = _PARTKEYDEF_RE.match(value.strip())
    inner = match.group(1) if match else value.strip()

    return PhysicalLayout(
        mechanism="partition",
        keys=tuple(_partition_key(part.strip()) for part in _split_top_level_commas(inner)),
    )


def _partition_key(expression: str) -> PhysicalLayoutKey:
    match = _BASE_COLUMN_RE.match(expression)

    return PhysicalLayoutKey(
        expression=expression,
        column=match.group(1).lower() if match else None,
    )


def _split_top_level_commas(text: str) -> list[str]:
    """Split on commas outside parentheses - a partition expression may nest a function call."""

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


def reltuples_estimate(conn: psycopg.Connection, fqn: str) -> float:
    """Planner-stat row-count estimate; -1 if no stats yet (use exact path then)."""

    schema, table = _split_fqn(fqn)
    row = exec_query(
        conn,
        """
        SELECT c.reltuples::double precision
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s
        """,
        (schema, table),
    ).fetchone()

    return float(row[0]) if row else -1.0


def resolve_column(conn: psycopg.Connection, fqn: str, column: str) -> str:
    """The catalog's own spelling for a lowercased column name (SPEC 2.2.1's map key).

    For a caller holding only the artifact key, not the `columns()` read `physical_name`
    rides on: `sample_values` (SPEC 4.1.2), which the engine addresses by map key.
    """

    schema, table = _split_fqn(fqn)
    row = exec_query(
        conn,
        """
        SELECT a.attname
        FROM pg_attribute a
        JOIN pg_class c     ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND c.relname = %s
          AND lower(a.attname) = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
        """,
        (schema, table, column),
    ).fetchone()

    if row is None:
        raise KeyError(f"no column named {column!r} (case-insensitive) on {fqn!r}")

    return row[0]


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
        raise ValueError(f"Postgres FQN must be 'schema.table', got {fqn!r}")

    schema, _, table = fqn.partition(".")

    return schema, table


def _attnums_to_names(conn: psycopg.Connection, relid: int, attnums: list[int]) -> list[str]:
    """Resolve a list of attnums for one relation into lowercased column names, preserving order.

    Lowercased to agree with the `columns` map key (SPEC 2.2.1); no artifact these feed
    quotes the name back into a live statement, so no physical spelling is preserved.
    """

    if not attnums:
        return []

    rows = exec_query(
        conn,
        """
        SELECT attnum, attname
        FROM pg_attribute
        WHERE attrelid = %s AND attnum = ANY(%s) AND NOT attisdropped
        """,
        (relid, list(attnums)),
    ).fetchall()
    name_by_attnum = {attnum: attname.lower() for attnum, attname in rows}

    return [name_by_attnum[a] for a in attnums if a in name_by_attnum]
