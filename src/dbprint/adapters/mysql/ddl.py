"""SHOW CREATE TABLE extraction + SPEC 2.1.3 MySQL normalization.

`extract_ddl` runs `SHOW CREATE TABLE`, which serves views too on MySQL/MariaDB.
Normalization strips only the volatile `AUTO_INCREMENT=<N>` table option, keeping the
column-level `AUTO_INCREMENT` keyword; backticks are native DDL and stay verbatim.
"""

from __future__ import annotations

import re

from .connection import Cursor, exec_query


_AUTO_INCREMENT_COUNTER_RE = re.compile(r"\s+AUTO_INCREMENT=\d+", re.IGNORECASE)


def extract_ddl(cursor: Cursor, fqn: str) -> str:
    """Return native-dialect DDL for the object, post-normalization."""

    database, table = _split_fqn(fqn)
    row = exec_query(cursor, f"SHOW CREATE TABLE {_quote_qualified(database, table)}").fetchone()

    if not row or len(row) < 2 or not row[1]:
        raise ValueError(f"no DDL available for {fqn!r}; not found in catalog")

    return normalize(str(row[1]))


def normalize(raw: str) -> str:
    """Strip the AUTO_INCREMENT counter + trailing whitespace; ensure terminal newline."""

    without_counter = _AUTO_INCREMENT_COUNTER_RE.sub("", raw)
    lines = [line.rstrip() for line in without_counter.splitlines()]
    text = "\n".join(lines).strip("\n")

    return text + "\n" if text else ""


def _split_fqn(fqn: str) -> tuple[str, str]:
    if "." not in fqn:
        raise ValueError(f"MySQL FQN must be 'database.table', got {fqn!r}")

    database, _, table = fqn.partition(".")

    return database.strip("`"), table.strip("`")


def _quote_qualified(database: str, table: str) -> str:
    return f"{_quote_ident(database)}.{_quote_ident(table)}"


def _quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"
