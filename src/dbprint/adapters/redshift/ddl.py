"""`SHOW TABLE` extraction + SPEC 2.1.3 Redshift normalization - `SHOW TABLE`/`SHOW VIEW` are
statements, not relations, one object per call returning the recreate-DDL text.
"""

from __future__ import annotations

import re

from .connection import Cursor, exec_query


_TRAILING_WHITESPACE_RE = re.compile(r"[ \t]+$", re.MULTILINE)


def extract_ddl(cursor: Cursor, fqn: str) -> str:
    """Return native-dialect DDL for the object, post-normalization.

    `SHOW TABLE` against a view is undocumented, so a server error is caught as well as a falsy
    row before the `SHOW VIEW` fallback.
    """

    schema, table = _split_fqn(fqn)
    quoted = f'"{schema}"."{table}"'

    try:
        row = exec_query(cursor, f"SHOW TABLE {quoted}").fetchone()
    except Exception:  # noqa: BLE001 - the object may be a view; SHOW VIEW is the real fallback
        row = None

    if not row or not row[0]:
        row = exec_query(cursor, f"SHOW VIEW {quoted}").fetchone()

    if not row or not row[0]:
        raise ValueError(f"no DDL available for {fqn!r}; not found in catalog")

    return normalize(str(row[0]))


def normalize(raw: str) -> str:
    """Strip trailing whitespace per line; ensure a single terminal newline."""

    without_trailing = _TRAILING_WHITESPACE_RE.sub("", raw)
    text = without_trailing.strip("\n")

    return text + "\n" if text else ""


def _split_fqn(fqn: str) -> tuple[str, str]:
    if "." not in fqn:
        raise ValueError(f"Redshift FQN must be 'schema.table', got {fqn!r}")

    schema, _, table = fqn.partition(".")

    return schema, table
