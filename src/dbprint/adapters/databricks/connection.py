"""Cursor-factory wrapped session for the Databricks adapter - `cursor_factory` is the seam
every adapter here exposes; nothing runs offline against the real service.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

from .. import driver
from ..dialect import Dialect
from ..driver import Cursor, FactoryConnection


# databricks-sql-connector defaults to native parameter binding (use_inline_params=False), which
# the adapter does not override, and native positional binding takes `?` markers.
DIALECT = Dialect(
    vendor="databricks",
    paramstyle="qmark",
    quote_char="`",
    text_type="STRING",
    concat_null_flags=True,
    pair_distinct="COUNT(DISTINCT STRUCT({a}, {b}))",
    seed_hash="MD5(CONCAT({seed}, CAST({value} AS STRING)))",
)

_LOG = logging.getLogger(__name__)

# The vendor's own ceiling; a larger limit is refused rather than silently clamped.
STATEMENT_TIMEOUT_CEILING_SECONDS = 172_800


class DatabricksConnectionError(RuntimeError):
    """Raised when the adapter cannot establish a working Databricks session."""


@dataclass(frozen=True)
class ConnectionParams:
    """Resolved Databricks credentials passed to the adapter."""

    server_hostname: str
    http_path: str
    access_token: str
    catalog: str | None = None
    statement_timeout: int | None = None

    @classmethod
    def from_credentials(
        cls,
        creds: dict[str, str],
        statement_timeout: int | None = None,
    ) -> ConnectionParams:
        try:
            return cls(
                server_hostname=creds["server_hostname"],
                http_path=creds["http_path"],
                access_token=creds["access_token"],
                catalog=creds.get("catalog"),
                statement_timeout=statement_timeout,
            )
        except KeyError as exc:
            raise DatabricksConnectionError(
                f"missing required credential key: {exc.args[0]!r}",
            ) from exc


class Connection(FactoryConnection):
    """A Databricks session opened by a cursor factory."""

    error = DatabricksConnectionError
    vendor = "Databricks"
    timeout_ceiling = STATEMENT_TIMEOUT_CEILING_SECONDS

    def _default_factory(self, params: ConnectionParams) -> Any:
        return _default_cursor_factory(params)

    def _open_failure(self, exc: Exception) -> str:
        return f"could not open Databricks session for {self.params.server_hostname!r}: {exc}"


def exec_query(cursor: Cursor, sql: str, params: Any = None) -> Cursor:
    """Run a query and return the cursor; DEBUG-traces the text and params as a pair."""

    return driver.execute(_LOG, _is_timeout, cursor, sql, params)


def _default_cursor_factory(params: ConnectionParams) -> Any:
    """Open a real databricks-sql-connector DB-API connection; raises an install hint if absent
    - lazy, so a base install never pays the connector's import cost.
    """

    try:
        sql = importlib.import_module("databricks.sql")
    except ImportError as exc:
        raise DatabricksConnectionError(
            "databricks-sql-connector is not installed. Install dbprint with the "
            "[databricks] extra: `pip install dbprint[databricks]`.",
        ) from exc

    session_configuration = (
        {} if params.statement_timeout is None else {"STATEMENT_TIMEOUT": params.statement_timeout}
    )
    conn = sql.connect(
        server_hostname=params.server_hostname,
        http_path=params.http_path,
        access_token=params.access_token,
        catalog=params.catalog,
        session_configuration=session_configuration,
    )

    return conn.cursor()


def _is_timeout(exc: BaseException) -> bool:
    # The connector exposes no SQLSTATE, only the error class in its message.
    return "QUERY_EXECUTION_TIMEOUT_EXCEEDED" in str(exc)
