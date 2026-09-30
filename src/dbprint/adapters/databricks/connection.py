"""Cursor-factory wrapped session for the Databricks adapter - `cursor_factory` is the seam
every adapter here exposes; nothing runs offline against the real service.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from dbprint.config.duration import format_duration_seconds
from .. import trace_context
from ..dialect import Dialect
from ..errors import QueryFailed


# databricks-sql-connector defaults to native parameter binding (use_inline_params=False), which
# the adapter does not override, and native positional binding takes `?` markers.
DIALECT = Dialect(vendor="databricks", paramstyle="qmark", quote_char="`")

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


class Cursor(Protocol):
    """DB-API-compatible cursor surface used by the adapter."""

    def execute(self, sql: str, params: Any = ...) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def fetchone(self) -> Any: ...

    def close(self) -> None: ...


CursorFactory = Callable[[ConnectionParams], Any]


class Connection:
    """Wraps a cursor-factory output with open/close lifecycle hooks."""

    def __init__(
        self,
        params: ConnectionParams,
        cursor_factory: CursorFactory | None = None,
    ) -> None:
        self.params = params
        self._factory = cursor_factory or _default_cursor_factory
        self._cursor: Cursor | None = None

    def sibling(self) -> Connection:
        """An unopened connection with the same parameters and cursor factory."""

        return Connection(self.params, self._factory)

    def open(self) -> None:
        limit = self.params.statement_timeout

        if limit is not None and limit > STATEMENT_TIMEOUT_CEILING_SECONDS:
            ceiling = format_duration_seconds(STATEMENT_TIMEOUT_CEILING_SECONDS)

            raise DatabricksConnectionError(
                f"statement_timeout {format_duration_seconds(limit)} exceeds Databricks's ceiling "
                f"of {ceiling} ({STATEMENT_TIMEOUT_CEILING_SECONDS} seconds).",
            )

        try:
            self._cursor = self._factory(self.params)
        except DatabricksConnectionError:
            raise
        except Exception as exc:
            raise DatabricksConnectionError(
                f"could not open Databricks session for {self.params.server_hostname!r}: {exc}",
            ) from exc

    def close(self) -> None:
        if self._cursor is not None:
            try:
                self._cursor.close()
            except Exception:  # noqa: BLE001, S110 - close-time failure is uninteresting
                pass

            self._cursor = None

    def is_open(self) -> bool:
        return self._cursor is not None

    @property
    def cursor(self) -> Cursor:
        if self._cursor is None:
            raise DatabricksConnectionError("connection is not open; call connect() first")

        return self._cursor


def exec_query(cursor: Cursor, sql: str, params: Any = None) -> Cursor:
    """Run a query and return the cursor; DEBUG-traces the text and params as a pair."""

    started = time.monotonic()

    try:
        if params is None:
            cursor.execute(sql)
        else:
            cursor.execute(sql, params)
    except Exception as exc:
        failure = QueryFailed(exc, sql, params, timed_out=_is_timeout(exc))
        trace_context.log_failure(_LOG, started, failure)

        raise failure from exc

    trace_context.log_success(_LOG, started, sql, params, getattr(cursor, "rowcount", None))

    return cursor


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
