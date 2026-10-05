"""duckdb session lifecycle. See ARCHITECTURE.md 2 - `cursor_factory` is the seam every
adapter exposes, letting tests inject an already-seeded in-memory database.
"""

from __future__ import annotations

import importlib
import logging
import threading
from dataclasses import dataclass
from typing import Any

from .. import driver
from ..dialect import Dialect
from ..driver import Cursor, FactoryConnection


# The Python duckdb driver binds parameters positionally with `?`, like sqlite3.
DIALECT = Dialect(vendor="duckdb", paramstyle="qmark", quote_char='"')

_LOG = logging.getLogger(__name__)


class DuckdbConnectionError(RuntimeError):
    """Raised when the adapter cannot open a working duckdb connection."""


@dataclass(frozen=True)
class ConnectionParams:
    """Resolved duckdb credentials: a file path, or `:memory:` for an ephemeral database."""

    database: str
    read_only: bool = False
    statement_timeout: int | None = None

    @classmethod
    def from_credentials(
        cls,
        creds: dict[str, str],
        statement_timeout: int | None = None,
    ) -> ConnectionParams:
        try:
            database = creds["database"]
        except KeyError as exc:
            raise DuckdbConnectionError(
                f"missing required credential key: {exc.args[0]!r}",
            ) from exc

        read_only = str(creds.get("read_only", "")).strip().lower() in ("1", "true", "yes")

        return cls(database=database, read_only=read_only, statement_timeout=statement_timeout)


class Connection(FactoryConnection):
    """A duckdb session opened by a cursor factory."""

    error = DuckdbConnectionError
    vendor = "duckdb"

    def _default_factory(self, params: ConnectionParams) -> Any:
        return _default_cursor_factory(params)

    def _open_failure(self, exc: Exception) -> str:
        return f"could not open duckdb database {self.params.database!r}: {exc}"

    def _opened(self, cursor: Any) -> Any:
        limit = self.params.statement_timeout

        return cursor if limit is None else _TimedCursor(cursor, limit)


def exec_query(cursor: Cursor, sql: str, params: Any = None) -> Cursor:
    """Run a query and return the cursor; DEBUG-traces the text and params as a pair."""

    return driver.execute(_LOG, _is_timeout, cursor, sql, params)


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


class _TimedCursor:
    """A connection a client-side timer interrupts, and only while its own statement is in flight."""

    def __init__(self, cursor: Any, seconds: int) -> None:
        self._cursor = cursor
        self._seconds = seconds
        self._lock = threading.Lock()
        self._in_flight: object | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def execute(self, sql: str, params: Any = None) -> Any:
        token = object()
        fired = threading.Event()

        def interrupt() -> None:
            with self._lock:
                if self._in_flight is token:
                    fired.set()
                    self._cursor.interrupt()

        timer = threading.Timer(self._seconds, interrupt)

        with self._lock:
            self._in_flight = token

        timer.start()

        try:
            return (
                self._cursor.execute(sql) if params is None else self._cursor.execute(sql, params)
            )
        except Exception as exc:
            if fired.is_set():
                raise TimeoutError(f"statement interrupted after {self._seconds}s") from exc

            raise
        finally:
            timer.cancel()

            with self._lock:
                self._in_flight = None

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def close(self) -> None:
        self._cursor.close()


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError)
