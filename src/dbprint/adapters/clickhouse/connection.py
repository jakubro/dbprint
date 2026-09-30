"""ClickHouse session lifecycle. See ARCHITECTURE.md 2 - no connection-level `SETTINGS
readonly` is applied; read-only is the connected user's own grants.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .. import trace_context
from ..dialect import Dialect
from ..errors import QueryFailed


# clickhouse-connect's DB-API defaults to pyformat (%s); the adapter does not override it.
DIALECT = Dialect(vendor="clickhouse", paramstyle="pyformat", quote_char="`")

_LOG = logging.getLogger(__name__)


class ClickhouseConnectionError(RuntimeError):
    """Raised when the adapter cannot open a working ClickHouse session."""


@dataclass(frozen=True)
class ConnectionParams:
    """Resolved ClickHouse credentials passed to the adapter."""

    host: str
    port: int
    user: str
    password: str
    database: str | None = None
    statement_timeout: int | None = None

    @classmethod
    def from_credentials(
        cls,
        creds: dict[str, str],
        statement_timeout: int | None = None,
    ) -> ConnectionParams:
        try:
            return cls(
                host=creds["host"],
                port=int(creds.get("port", 8123)),
                database=creds.get("database"),
                user=creds.get("user", "default"),
                password=creds.get("password", ""),
                statement_timeout=statement_timeout,
            )
        except KeyError as exc:
            raise ClickhouseConnectionError(
                f"missing required credential key: {exc.args[0]!r}",
            ) from exc
        except ValueError as exc:
            raise ClickhouseConnectionError(f"invalid port {creds.get('port')!r}: {exc}") from exc


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
        try:
            self._cursor = self._factory(self.params)
        except ClickhouseConnectionError:
            raise
        except Exception as exc:
            where = f"{self.params.host}:{self.params.port}"
            where += f"/{self.params.database}" if self.params.database is not None else ""

            raise ClickhouseConnectionError(
                f"could not connect to ClickHouse at {where} as {self.params.user!r}: {exc}",
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
            raise ClickhouseConnectionError("connection is not open; call connect() first")

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
    """Open a real clickhouse-connect DB-API connection; raises with an install hint if absent
    - lazy, so a base install never pays clickhouse-connect's import cost.
    """

    try:
        dbapi = importlib.import_module("clickhouse_connect.dbapi")
    except ImportError as exc:
        raise ClickhouseConnectionError(
            "clickhouse-connect is not installed. Install dbprint with the [clickhouse] "
            "extra: `pip install dbprint[clickhouse]`.",
        ) from exc

    # Unrecognised keywords travel as server settings on every request; a zero speed-check delay
    # makes the limit wall-clock rather than a projection that can fire before it is reached.
    settings = (
        {}
        if params.statement_timeout is None
        else {
            "max_execution_time": params.statement_timeout,
            "timeout_before_checking_execution_speed": 0,
        }
    )
    conn = dbapi.connect(
        host=params.host,
        port=params.port,
        database=params.database,
        username=params.user,
        password=params.password,
        **settings,
    )

    return conn.cursor()


def _is_timeout(exc: BaseException) -> bool:
    # TIMEOUT_EXCEEDED; older clients carry the server code only in the message.
    return getattr(exc, "code", None) == 159 or "Code: 159" in str(exc)
