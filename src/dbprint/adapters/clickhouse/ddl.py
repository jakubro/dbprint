"""DDL extraction via `system.tables.create_table_query` - an ordinary catalog column, so no
per-object statement and no external binary.
"""

from __future__ import annotations

from .connection import Cursor, exec_query
from .introspect import SECRETS_HIDDEN
from ..credentials import mask_clickhouse_strings
from ..identifiers import Identity
from ..sql_layout import trimmed_lines


def extract_ddl(cursor: Cursor, identity: Identity, *, hide_secrets: bool = False) -> str:
    """Return native-dialect DDL for the object, post-normalization.

    `hide_secrets` reads it with the server's masking forced on, for a session that turned it off.
    """

    row = exec_query(
        cursor,
        f"""
        SELECT
          tbl.create_table_query
        FROM
          system.tables tbl
        WHERE
          tbl.database = %s
          AND tbl.name = %s{SECRETS_HIDDEN if hide_secrets else ""}
        """,
        identity.parts,
    ).fetchone()

    if not row or not row[0]:
        raise ValueError(f"no DDL available for {identity.fqn!r}; not found in catalog")

    return normalize(str(row[0]))


def normalize(raw: str) -> str:
    """Strip trailing whitespace per line, mask a URL password in the engine; one final newline."""

    head, engine, rest = raw.partition("ENGINE =")
    start = rest.find("(")
    end = _closing_paren(rest, start) if start != -1 and rest[:start].strip().isidentifier() else -1
    masked = (
        rest if end == -1 else rest[:start] + mask_clickhouse_strings(rest[start:end]) + rest[end:]
    )
    return trimmed_lines(head + engine + masked)


def _closing_paren(text: str, start: int) -> int:
    depth, quoting, index = 0, None, start

    while index < len(text):
        char = text[index]

        if quoting is not None and char == "\\":
            index += 1
        elif quoting is not None:
            quoting = None if char == quoting else quoting
        elif char in "'`\"":
            quoting = char
        elif char in "()":
            depth += 1 if char == "(" else -1

            if depth == 0:
                return index + 1

        index += 1

    return -1
