"""pg_catalog queries for structural metadata.

`list_tables` excludes system schemas and partitioning children (`relispartition`)
regardless of selectors: a partition is a fragment of its parent's logical table.

`pg_class` compares case-sensitively, so every read past enumeration binds `Identity`'s spelling.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from dbprint.config.selectors import expand
from dbprint.spec.fqn import join as join_fqn
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
from ..identifiers import Identity, column_meta, enforce_table_identifiers, fold, table_meta


if TYPE_CHECKING:
    import psycopg


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

_Candidate = tuple[TableMeta, tuple[str, str, str]]


def list_databases(conn: psycopg.Connection) -> tuple[str, ...]:
    """Every database a session can connect to, templates excluded."""

    rows = exec_query(
        conn,
        """
        SELECT
          dbs.datname
        FROM
          pg_database dbs
        WHERE
          dbs.datallowconn
          AND NOT dbs.datistemplate
        ORDER BY
          dbs.datname
        """,
    ).fetchall()

    return tuple(str(name) for (name,) in rows)


def relations(conn: psycopg.Connection, database: str) -> list[_Candidate]:
    """Tables/views/matviews in the user schemas of `database`, read on its own session."""

    rows = exec_query(
        conn,
        """
        SELECT
          nsp.nspname AS schema,
          cls.relname AS name,
          cls.relkind AS kind
        FROM
          pg_class cls
          JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
        WHERE
          cls.relkind IN ('r', 'p', 'v', 'm')
          AND NOT cls.relispartition
          AND nsp.nspname NOT IN ('pg_catalog', 'information_schema')
          AND nsp.nspname NOT LIKE 'pg_toast%'
          AND nsp.nspname NOT LIKE 'pg_temp_%'
        ORDER BY
          nsp.nspname, cls.relname
        """,
    ).fetchall()

    return [
        (table_meta((database, schema, name), _RELKIND_TO_TYPE[kind]), (database, schema, name))
        for schema, name, kind in rows
    ]


def select_tables(
    candidates: list[_Candidate],
    include: list[str],
    exclude: list[str],
) -> list[_Candidate]:
    """Filter `candidates` by selectors, refusing SPEC 1.5 violations across every database."""

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


def columns(conn: psycopg.Connection, identity: Identity) -> list[ColumnMeta]:
    """Per-column structural metadata in ordinal order.

    `name` is lowercased for the artifact's map key (SPEC 2.2.1), with the catalog's own
    spelling carried as `physical_name` when the two differ. `collation` comes from
    `information_schema.columns`, which reports NULL for a type's default collation and a
    name only when one was set explicitly - the omit-unless-it-differs rule of SPEC 2.2.2.
    """

    rows = exec_query(
        conn,
        """
        SELECT
          att.attname AS name,
          pg_catalog.FORMAT_TYPE(att.atttypid, att.atttypmod) AS sql_type,
          NOT att.attnotnull AS nullable,
          PG_GET_EXPR(adf.adbin, adf.adrelid) AS default_expr,
          att.attnum AS ordinal,
          isc.collation_name AS collation_name

        FROM
          pg_attribute att
          JOIN pg_class cls ON cls.oid = att.attrelid
          JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
          LEFT JOIN pg_attrdef adf ON
            adf.adrelid = att.attrelid
            AND adf.adnum = att.attnum
          LEFT JOIN information_schema.columns isc ON
            isc.table_schema = nsp.nspname
            AND isc.table_name = cls.relname
            AND isc.column_name = att.attname

        WHERE
          nsp.nspname = %s
          AND cls.relname = %s
          AND att.attnum > 0
          AND NOT att.attisdropped

        ORDER BY
          att.attnum
        """,
        identity.addressed,
    ).fetchall()

    bases = _resolved_bases(conn, identity)

    return [
        column_meta(
            name,
            sql_type=sql_type,
            nullable=nullable,
            default=default,
            ordinal=ordinal,
            collation=collation_name,
            classify_as=_classify_as(*bases[name]),
        )
        for name, sql_type, nullable, default, ordinal, collation_name in rows
    ]


def composite_columns(conn: psycopg.Connection, identity: Identity) -> frozenset[str]:
    """Lowercased names of columns whose type resolves to a composite (row) type.

    A domain chain resolves to its ultimate base type first: a domain over a composite is
    still a composite for SPEC 3.1's representability boundary. The test is the catalog's
    `pg_type.typtype = 'c'`, since a composite type's name is whatever its author chose.
    """

    return frozenset(
        fold(name)
        for name, (_, typtype, _) in _resolved_bases(conn, identity).items()
        if typtype == "c"
    )


def default_collation(conn: psycopg.Connection) -> str:
    """The current database's default collation (SPEC 2.2.2) - one scalar, once per run."""

    row = exec_query(
        conn,
        """
        SELECT
          dbs.datcollate
        FROM
          pg_database dbs
        WHERE
          dbs.datname = CURRENT_DATABASE()
        """,
    ).fetchone()

    return row[0] if row else ""


def relationships(conn: psycopg.Connection, identity: Identity) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs; one entry per constraint (composite as arrays)."""

    rows = exec_query(
        conn,
        """
        SELECT
          con.conname AS constraint_name,
          con.conkey AS src_attnums,
          con.confkey AS dst_attnums,
          tnp.nspname AS dst_schema,
          tcl.relname AS dst_table,
          con.confdeltype AS on_delete,
          con.confupdtype AS on_update,
          con.conrelid AS src_relid,
          con.confrelid AS dst_relid

        FROM
          pg_constraint con
          JOIN pg_class scl ON scl.oid = con.conrelid
          JOIN pg_namespace snp ON snp.oid = scl.relnamespace
          JOIN pg_class tcl ON tcl.oid = con.confrelid
          JOIN pg_namespace tnp ON tnp.oid = tcl.relnamespace

        WHERE
          con.contype = 'f'
          AND snp.nspname = %s
          AND scl.relname = %s

        ORDER BY
          con.conname
        """,
        identity.addressed,
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
                # A constraint references a relation in its own database only.
                target_table=join_fqn((fold(identity.parts[0]), fold(dst_schema), fold(dst_table))),
                target_column=tuple(dst_cols),
                on_delete=cast(FkAction, _FK_ACTIONS[on_del]),
                on_update=cast(FkAction, _FK_ACTIONS[on_upd]),
                constraint_name=name,
            ),
        )

    return out


def indexes(conn: psycopg.Connection, identity: Identity) -> list[IndexMeta]:
    """Secondary indexes only; PK-backed, constraint-backed and bare-unique indexes excluded.

    A bare unique index (`indisunique`, no backing `pg_constraint`, `indpred IS NULL`) is
    reported by `unique_keys` instead. A partial unique index (`indpred IS NOT NULL`) stays
    here: it enforces uniqueness over a subset of rows, which `unique_keys` does not report.
    """

    rows = exec_query(
        conn,
        """
        SELECT
          icl.relname AS index_name,
          STRING_TO_ARRAY(idx.indkey::TEXT, ' ')::INT[] AS attnums,
          idx.indisunique AS is_unique,
          acm.amname AS index_type,
          idx.indrelid AS table_relid

        FROM
          pg_index idx
          JOIN pg_class icl ON icl.oid = idx.indexrelid
          JOIN pg_class tcl ON tcl.oid = idx.indrelid
          JOIN pg_namespace nsp ON nsp.oid = tcl.relnamespace
          JOIN pg_am acm ON acm.oid = icl.relam

        WHERE
          nsp.nspname = %s
          AND tcl.relname = %s
          AND NOT idx.indisprimary
          AND NOT EXISTS (
            SELECT
              1
            FROM
              pg_constraint con
            WHERE
              con.conindid = idx.indexrelid
              AND con.contype IN ('p', 'u')
          )
          AND NOT (idx.indisunique AND idx.indpred IS NULL)

        ORDER BY
          icl.relname
        """,
        identity.addressed,
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


def comments(conn: psycopg.Connection, identity: Identity) -> CommentsMeta:
    """Table comment + per-column comments from pg_description."""

    table_row = exec_query(
        conn,
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
        conn,
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
          AND NOT att.attisdropped
        """,
        identity.addressed,
    ).fetchall()

    return CommentsMeta(
        table=table_comment,
        columns={
            fold(name): description for name, description in col_rows if description is not None
        },
    )


def unique_keys(conn: psycopg.Connection, identity: Identity) -> list[UniqueKeyMeta]:
    """Declared-unique column groups: primary key, unique constraints, bare unique indexes.

    `contype` sorts 'p' before 'u', so the primary key both leads and is marked; `conkey`
    is the attnum sequence in declaration order. The second arm adds a bare unique index
    backing no constraint - "declared unique" means enforced, not named - excluding partial
    indexes and any a constraint already covers. No relkind filter, so a matview's bare
    unique index is picked up too.
    """

    rows = exec_query(
        conn,
        """
        SELECT
          con.conkey::INT[] AS conkey,
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

        UNION ALL

        SELECT
          STRING_TO_ARRAY(idx.indkey::TEXT, ' ')::INT[] AS conkey,
          idx.indrelid AS relid,
          'u' AS contype,
          icl.relname AS name
        FROM
          pg_index idx
          JOIN pg_class icl ON icl.oid = idx.indexrelid
          JOIN pg_class cls ON cls.oid = idx.indrelid
          JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
        WHERE
          idx.indisunique
          AND NOT idx.indisprimary
          AND idx.indpred IS NULL
          AND nsp.nspname = %s
          AND cls.relname = %s
          AND NOT EXISTS (
            SELECT
              1
            FROM
              pg_constraint cns
            WHERE
              cns.conindid = idx.indexrelid
              AND cns.contype IN ('p', 'u')
          )

        ORDER BY
          contype, name
        """,
        identity.addressed * 2,
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


def physical_layout(conn: psycopg.Connection, identity: Identity) -> PhysicalLayout | None:
    """Declared partition key via `pg_get_partkeydef`; None on a non-partitioned table.

    Only the partitioned parent (`relkind = 'p'`) carries a key, and dbprint profiles the
    parent rather than its individual partitions.
    """

    row = exec_query(
        conn,
        """
        SELECT
          PG_GET_PARTKEYDEF(cls.oid)
        FROM
          pg_class cls
          JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
        WHERE
          nsp.nspname = %s
          AND cls.relname = %s
          AND cls.relkind = 'p'
        """,
        identity.addressed,
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
        column=fold(match.group(1)) if match else None,
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


def view_dependencies(conn: psycopg.Connection, database: str) -> dict[str, tuple[str, ...]]:
    """Every view/matview's direct object dependencies in `database`, one query on its own session -
    the LEFT joins seed every view, so absence means "not a view", never "reads nothing".
    """

    rows = exec_query(
        conn,
        """
        SELECT DISTINCT
          vnp.nspname AS view_schema,
          vew.relname AS view_name,
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
            AND scl.relkind IN ('r', 'p', 'v', 'm', 'f')
          LEFT JOIN pg_namespace snp ON
            snp.oid = scl.relnamespace
            AND snp.nspname NOT IN ('pg_catalog', 'information_schema')
            AND snp.nspname NOT LIKE 'pg_toast%'

        WHERE
          vew.relkind IN ('v', 'm')
          AND vnp.nspname NOT IN ('pg_catalog', 'information_schema')
          AND vnp.nspname NOT LIKE 'pg_toast%'

        ORDER BY
          1, 2, 3, 4
        """,
    ).fetchall()

    out: dict[str, list[str]] = {}

    for view_schema, view_name, source_schema, source_name in rows:
        key = join_fqn((fold(database), fold(view_schema), fold(view_name)))
        out.setdefault(key, [])

        if source_schema is not None and source_name is not None:
            out[key].append(join_fqn((fold(database), fold(source_schema), fold(source_name))))

    return {k: tuple(v) for k, v in out.items()}


def reltuples_estimate(conn: psycopg.Connection, identity: Identity) -> float:
    """Planner-stat row-count estimate; -1 if no stats yet (use exact path then)."""

    row = exec_query(
        conn,
        """
        SELECT
          cls.reltuples::DOUBLE PRECISION
        FROM
          pg_class cls
          JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
        WHERE
          nsp.nspname = %s
          AND cls.relname = %s
        """,
        identity.addressed,
    ).fetchone()

    return float(row[0]) if row else -1.0


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
        SELECT
          att.attnum,
          att.attname
        FROM
          pg_attribute att
        WHERE
          att.attrelid = %s
          AND att.attnum = ANY(%s)
          AND NOT att.attisdropped
        """,
        (relid, list(attnums)),
    ).fetchall()
    name_by_attnum = {attnum: fold(attname) for attnum, attname in rows}

    return [name_by_attnum[a] for a in attnums if a in name_by_attnum]


# The built-in name classification reads for each user-definable family; a composite is declined.
_PSEUDO_TYPES = {"e": "anyenum", "r": "anyrange", "m": "anymultirange", "c": "record"}


def _resolved_bases(
    conn: psycopg.Connection,
    identity: Identity,
) -> dict[str, tuple[str, str, bool]]:
    """Per physical column name: its ultimate base type, that type's `typtype`, and whether a
    domain chain led there.
    """

    rows = exec_query(
        conn,
        """
        WITH RECURSIVE
          chain AS (

            SELECT
              att.attname AS name,
              att.atttypid AS oid,
              att.atttypmod AS typmod,
              0 AS depth
            FROM
              pg_attribute att
              JOIN pg_class cls ON cls.oid = att.attrelid
              JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
            WHERE
              nsp.nspname = %s
              AND cls.relname = %s
              AND att.attnum > 0
              AND NOT att.attisdropped

            UNION ALL

            SELECT
              chn.name,
              typ.typbasetype,
              typ.typtypmod,
              chn.depth + 1
            FROM
              chain chn
              JOIN pg_type typ ON typ.oid = chn.oid
            WHERE
              typ.typtype = 'd'
              AND typ.typbasetype <> 0

          )
        SELECT
          chn.name,
          pg_catalog.FORMAT_TYPE(chn.oid, chn.typmod),
          typ.typtype::TEXT,
          chn.depth > 0
        FROM
          chain chn
          JOIN pg_type typ ON typ.oid = chn.oid
        WHERE
          typ.typtype <> 'd'
        """,
        identity.addressed,
    ).fetchall()

    return {name: (base, typtype, via_domain) for name, base, typtype, via_domain in rows}


def _classify_as(base: str, typtype: str, via_domain: bool) -> str | None:
    if typtype in _PSEUDO_TYPES:
        return _PSEUDO_TYPES[typtype]

    return base if via_domain else None
