"""Redshift session lifecycle. See ARCHITECTURE.md 2 - no session-level read-only setting is
applied; read-only is the connected user's own grants.
"""

from __future__ import annotations

import importlib
import logging
from types import MappingProxyType
from typing import Any

from .. import driver
from ..dialect import Dialect
from ..driver import Cursor, FactoryConnection, ServerParams


# redshift_connector defaults to pyformat, same as psycopg2; the adapter does not override it.
DIALECT = Dialect(
    vendor="redshift",
    paramstyle="pyformat",
    quote_char='"',
    addressed_parts=2,
    # No row constructor: each half is length-prefixed, so no embedded delimiter can collide.
    pair_distinct=(
        "COUNT(DISTINCT LENGTH({a}::VARCHAR)::VARCHAR || ':' || {a}::VARCHAR"
        " || '|' || LENGTH({b}::VARCHAR)::VARCHAR || ':' || {b}::VARCHAR)"
    ),
)

_LOG = logging.getLogger(__name__)


class RedshiftConnectionError(RuntimeError):
    """Raised when the adapter cannot open a working Redshift session."""


class ConnectionParams(ServerParams):
    """Resolved Redshift credentials passed to the adapter."""

    error = RedshiftConnectionError
    defaults = MappingProxyType({"port": "5439"})


class Connection(FactoryConnection):
    """A Redshift session opened by a cursor factory."""

    error = RedshiftConnectionError
    vendor = "Redshift"

    def _default_factory(self, params: ConnectionParams) -> Any:
        return _default_cursor_factory(params)

    def _open_failure(self, exc: Exception) -> str:
        return (
            f"could not connect to Redshift at {self.params.host}:{self.params.port}/"
            f"{self.params.database} as {self.params.user!r}: {exc}"
        )


def exec_query(cursor: Cursor, sql: str, params: Any = None) -> Cursor:
    """Run a query and return the cursor; DEBUG-traces the text and params as a pair."""

    return driver.execute(_LOG, _is_timeout, cursor, sql, params)


def _default_cursor_factory(params: ConnectionParams) -> Any:
    """Open a real redshift_connector DB-API connection; raises with an install hint if absent
    - lazy, so a base install never pays redshift_connector's import cost.
    """

    try:
        redshift_connector = importlib.import_module("redshift_connector")
    except ImportError as exc:
        raise RedshiftConnectionError(
            "redshift-connector is not installed. Install dbprint with the [redshift] "
            "extra: `pip install dbprint[redshift]`.",
        ) from exc

    conn = redshift_connector.connect(
        host=params.host,
        port=params.port,
        database=params.database,
        user=params.user,
        password=params.password,
    )
    conn.autocommit = True
    cursor = conn.cursor()

    if params.statement_timeout is not None:
        cursor.execute(f"SET statement_timeout TO {params.statement_timeout * 1000}")

    return cursor


def _is_timeout(exc: BaseException) -> bool:
    # redshift_connector's first argument is the server's ErrorResponse field map.
    fields = exc.args[0] if exc.args else None

    return isinstance(fields, dict) and fields.get("C") == "57014"
