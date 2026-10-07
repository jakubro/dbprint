"""A MySQL adapter over the shared MariaDB substrate, listed and ready to extract."""

from __future__ import annotations

from dbprint.adapters import MysqlAdapter


def build_mysql(creds: dict[str, str]) -> MysqlAdapter:
    """A connected adapter that has enumerated - what per-table extraction requires.

    At `lower_case_table_names=0` a table never enumerated has no spelling to bind.
    """

    adapter = MysqlAdapter(creds)
    adapter.connect()
    adapter.list_tables(include=["*"], exclude=[])

    return adapter
