"""`SHOW CREATE TABLE` extraction + SPEC 2.1.3 normalization - not a byte-exact round trip,
Databricks filtering table properties from its own output (documented).
"""

from __future__ import annotations

from .connection import Cursor, exec_query
from .introspect import _split_fqn


def extract_ddl(cursor: Cursor, fqn: str) -> str:
    """Return native-dialect DDL for the object, post-normalization."""

    schema, table = _split_fqn(fqn)
    row = exec_query(cursor, f"SHOW CREATE TABLE `{schema}`.`{table}`").fetchone()

    if not row or not row[0]:
        raise ValueError(f"no DDL available for {fqn!r}; not found in catalog")

    return normalize(str(row[0]))


def normalize(raw: str) -> str:
    """Strip trailing whitespace per line; ensure a single terminal newline."""

    lines = [line.rstrip() for line in raw.splitlines()]
    text = "\n".join(lines).strip("\n")

    return text + "\n" if text else ""
