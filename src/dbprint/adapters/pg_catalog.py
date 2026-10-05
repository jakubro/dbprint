"""Catalog statements Postgres and Redshift both answer through their common `pg_catalog`."""

from __future__ import annotations


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
