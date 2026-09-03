"""duckdb session lifecycle. See ARCHITECTURE.md 2 - `cursor_factory` is the seam every
adapter exposes, letting tests inject an already-seeded in-memory database.
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


class DuckdbConnectionError(RuntimeError):
    """Raised when the adapter cannot open a working duckdb connection."""


@dataclass(frozen=True)
class ConnectionParams:
    """Resolved duckdb credentials: a file path, or `:memory:` for an ephemeral database."""

    database: str
    read_only: bool = False

    @classmethod
    def from_credentials(cls, creds: dict[str, str]) -> ConnectionParams:
        try:
            database = creds["database"]
        except KeyError as exc:
            raise DuckdbConnectionError(
                f"missing required credential key: {exc.args[0]!r}",
            ) from exc

        read_only = str(creds.get("read_only", "")).strip().lower() in ("1", "true", "yes")

        return cls(database=database, read_only=read_only)


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
        except DuckdbConnectionError:
            raise
        except Exception as exc:
            raise DuckdbConnectionError(
                f"could not open duckdb database {self.params.database!r}: {exc}",
            ) from exc

    def close(self) -> None:
        if self._cursor is not None:
            try:
                self._cursor.close()
            except Exception:  # noqa: BLE001, S110 - best-effort; the resource may already be dead
                pass

            self._cursor = None

    def is_open(self) -> bool:
        return self._cursor is not None

    @property
    def cursor(self) -> Cursor:
        if self._cursor is None:
            raise DuckdbConnectionError("connection is not open; call connect() first")

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
    """Open a real duckdb connection; raises with an install hint if the extra is missing -
    lazy, so a base install never pays duckdb's import cost.
    """

    try:
        duckdb = importlib.import_module("duckdb")
    except ImportError as exc:
        raise DuckdbConnectionError(
            "duckdb is not installed. Install dbprint with the [duckdb] extra: "
            "`pip install dbprint[duckdb]`.",
        ) from exc

    return duckdb.connect(database=params.database, read_only=params.read_only)
