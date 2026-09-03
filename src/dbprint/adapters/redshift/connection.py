"""Redshift session lifecycle. See ARCHITECTURE.md 2 - no session-level read-only setting is
applied; read-only is the connected user's own grants.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .. import trace_context
from ..errors import QueryFailed


_LOG = logging.getLogger(__name__)


class RedshiftConnectionError(RuntimeError):
    """Raised when the adapter cannot open a working Redshift session."""


@dataclass(frozen=True)
class ConnectionParams:
    """Resolved Redshift credentials passed to the adapter."""

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
                port=int(creds.get("port", 5439)),
                database=creds["database"],
                user=creds["user"],
                password=creds["password"],
            )
        except KeyError as exc:
            raise RedshiftConnectionError(
                f"missing required credential key: {exc.args[0]!r}",
            ) from exc
        except ValueError as exc:
            raise RedshiftConnectionError(f"invalid port {creds.get('port')!r}: {exc}") from exc


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

    def open(self) -> None:
        try:
            self._cursor = self._factory(self.params)
        except RedshiftConnectionError:
            raise
        except Exception as exc:
            raise RedshiftConnectionError(
                f"could not connect to Redshift at {self.params.host}:{self.params.port}/"
                f"{self.params.database} as {self.params.user!r}: {exc}",
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
            raise RedshiftConnectionError("connection is not open; call connect() first")

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

    return conn.cursor()
