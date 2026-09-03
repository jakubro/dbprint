"""DDL extraction via `system.tables.create_table_query` - an ordinary catalog column, so no
per-object statement and no external binary.
"""

from __future__ import annotations

from .connection import Cursor, exec_query


def extract_ddl(cursor: Cursor, fqn: str) -> str:
    """Return native-dialect DDL for the object, post-normalization."""

    database, table = _split_fqn(fqn)
    row = exec_query(
        cursor,
        "SELECT create_table_query FROM system.tables WHERE database = %s AND name = %s",
        (database, table),
    ).fetchone()

    if not row or not row[0]:
        raise ValueError(f"no DDL available for {fqn!r}; not found in catalog")

    return normalize(str(row[0]))


def normalize(raw: str) -> str:
    """Strip trailing whitespace per line; ensure exactly one terminal newline."""

    lines = [line.rstrip() for line in raw.splitlines()]
    text = "\n".join(lines).strip("\n")

    return text + "\n" if text else ""


def _split_fqn(fqn: str) -> tuple[str, str]:
    if "." not in fqn:
        raise ValueError(f"ClickHouse FQN must be 'database.table', got {fqn!r}")

    database, _, table = fqn.partition(".")

    return database, table
