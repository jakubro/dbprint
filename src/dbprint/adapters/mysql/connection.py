"""mysql-connector session lifecycle for the MySQL adapter.

One buffered cursor is shared for the adapter's lifetime: buffering materializes the full
result set on `execute`, so sequential queries and `fetchone` never trip the "unread result
found" error. The driver is imported lazily, so dbprint imports without the `[mysql]` extra.
"""

from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .. import trace_context
from ..errors import QueryFailed


_LOG = logging.getLogger(__name__)


class MysqlConnectionError(RuntimeError):
    """Raised when the adapter cannot establish a working MySQL session."""


@dataclass(frozen=True)
class ConnectionParams:
    """Resolved MySQL credentials passed to the adapter."""

    host: str
    port: int
    database: str
    user: str
    password: str

    @classmethod
    def from_credentials(cls, creds: dict[str, str]) -> ConnectionParams:
        try:
            return cls(
                host=creds["host"],
                port=int(creds["port"]),
                database=creds["database"],
                user=creds["user"],
                password=creds["password"],
            )
        except KeyError as exc:
            raise MysqlConnectionError(f"missing required credential key: {exc.args[0]!r}") from exc
        except ValueError as exc:
            raise MysqlConnectionError(f"invalid port {creds.get('port')!r}: {exc}") from exc


class Cursor(Protocol):
    """DB-API-compatible cursor surface used by the adapter."""

    def execute(self, sql: str, params: Any = ...) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def fetchone(self) -> Any: ...

    def close(self) -> None: ...


class Connection:
    """Wraps a mysql-connector session plus its shared buffered cursor."""

    def __init__(self, params: ConnectionParams) -> None:
        self.params = params
        self._conn: Any | None = None
        self._cursor: Cursor | None = None

    def open(self) -> None:
        connector = _import_connector()

        try:
            self._conn = connector.connect(
                host=self.params.host,
                port=self.params.port,
                database=self.params.database,
                user=self.params.user,
                password=self.params.password,
                autocommit=True,
            )
        except connector.Error as exc:
            raise MysqlConnectionError(
                f"could not connect to MySQL at {self.params.host}:{self.params.port}/"
                f"{self.params.database} as {self.params.user!r}: {exc}",
            ) from exc

        self._cursor = self._conn.cursor(buffered=True)

    def close(self) -> None:
        if self._cursor is not None:
            try:
                self._cursor.close()
            except Exception:  # noqa: BLE001, S110 - close-time failure is uninteresting
                pass

            self._cursor = None

        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001, S110 - close-time failure is uninteresting
                pass

            self._conn = None

    def is_open(self) -> bool:
        return self._conn is not None and bool(self._conn.is_connected())

    @property
    def cursor(self) -> Cursor:
        if self._cursor is None:
            raise MysqlConnectionError("connection is not open; call connect() first")

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
        failure = QueryFailed(exc, sql, params)
        trace_context.log_failure(_LOG, started, failure)

        raise failure from exc

    trace_context.log_success(_LOG, started, sql, params, getattr(cursor, "rowcount", None))

    return cursor


def _import_connector() -> Any:
    """Import mysql.connector lazily; raise an actionable error when absent."""

    try:
        return importlib.import_module("mysql.connector")
    except ImportError as exc:
        raise MysqlConnectionError(
            "mysql-connector-python is not installed. Install dbprint with the "
            "[mysql] extra: `pip install dbprint[mysql]`.",
        ) from exc
