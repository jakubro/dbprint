"""Catalog reads Postgres and Redshift both answer through their common `pg_catalog`."""

from __future__ import annotations

from dbprint.spec.fqn import join as join_fqn
from .base import FkAction, ForeignKeyMeta
from .identifiers import Identity, fold
from .sql_layout import listed
from .statements import Execute


_FK_ACTIONS: dict[str, FkAction] = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}

FOREIGN_KEYS = """
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
"""


def relationships(
    execute: Execute,
    identity: Identity,
    *,
    on_unknown: FkAction | None,
) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs, one entry per constraint (composite as arrays).

    An action code outside `_FK_ACTIONS` reads as `on_unknown`, or raises `KeyError` when None.
    """

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
    ) in execute(FOREIGN_KEYS, identity.addressed).fetchall():
        out.append(
            ForeignKeyMeta(
                column=tuple(attnums_to_names(execute, src_relid, list(src_attnums))),
                # `confrelid` is a local oid, so a target is always in the source's own database.
                target_table=join_fqn((fold(identity.parts[0]), fold(dst_schema), fold(dst_table))),
                target_column=tuple(attnums_to_names(execute, dst_relid, list(dst_attnums))),
                on_delete=_action(on_del, on_unknown),
                on_update=_action(on_upd, on_unknown),
                constraint_name=name,
            ),
        )

    return out


def attnums_to_names(execute: Execute, relid: int, attnums: list[int]) -> list[str]:
    """Resolve attnums for one relation into lowercased column names, order preserved - lowercase
    agrees with the `columns` map key (SPEC 2.2.1), and nothing quotes these into a statement.
    """

    if not attnums:
        return []

    # An explicit `IN` list, not `= ANY(<array>)`: AWS lists array constructors among the
    # PostgreSQL features Redshift does not support. Every bound value is an int from the catalog.
    placeholders = listed(["%s"] * len(attnums), 12)
    rows = execute(
        f"""
        SELECT
          att.attnum,
          att.attname
        FROM
          pg_attribute att
        WHERE
          att.attrelid = %s
          AND att.attnum IN (
            {placeholders}
          )
          AND NOT att.attisdropped
        """,
        (relid, *attnums),
    ).fetchall()
    name_by_attnum = {int(attnum): fold(str(attname)) for attnum, attname in rows}

    return [name_by_attnum[a] for a in attnums if a in name_by_attnum]


def _action(code: object, on_unknown: FkAction | None) -> FkAction:
    key = str(code)

    return _FK_ACTIONS[key] if on_unknown is None else _FK_ACTIONS.get(key, on_unknown)
