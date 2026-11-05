"""SQLite session lifecycle - Python's own `sqlite3`, the file always opened read-only, with the
functions the statements call registered on every connection a factory opens.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import pathname2url

from . import functions
from .. import driver, interrupt
from ..dialect import Dialect
from ..driver import Cursor, FactoryConnection


DIALECT = Dialect(
    vendor="sqlite",
    paramstyle="qmark",
    quote_char='"',
    text_type="TEXT",
    pair_distinct="COUNT(DISTINCT QUOTE({a}) || ',' || QUOTE({b}))",
    seed_hash="DBPRINT_MD5({seed} || HEX({value}))",
)

MIN_VERSION = (3, 38, 0)

# VM instructions between two looks at the deadline and the run's interrupt flag.
_PROGRESS_STEPS = 1000

_LOG = logging.getLogger(__name__)


class SqliteConnectionError(RuntimeError):
    """Raised when the adapter cannot open a working SQLite connection."""


@dataclass(frozen=True)
class ConnectionParams:
    """Resolved SQLite credentials: the database file's path."""

    database: str
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
            raise SqliteConnectionError(
                f"missing required credential key: {exc.args[0]!r}",
            ) from exc

        return cls(database=database, statement_timeout=statement_timeout)


class Connection(FactoryConnection):
    """A read-only SQLite session opened by a cursor factory."""

    error = SqliteConnectionError
    vendor = "sqlite"

    def _default_factory(self, params: ConnectionParams) -> Any:
        return _default_cursor_factory(params)

    def _open_failure(self, exc: Exception) -> str:
        return f"could not open sqlite database {self.params.database!r}: {exc}"

    def close(self) -> None:
        connection = getattr(self._cursor, "connection", None)
        super().close()

        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001, S110 - close-time failure is uninteresting
                pass

    def _opened(self, cursor: Any, /) -> Any:
        if sqlite3.sqlite_version_info < MIN_VERSION:
            raise SqliteConnectionError(
                f"sqlite {sqlite3.sqlite_version} is older than 3.38.0, "
                "the first release with JSON functions built in",
            )

        return _GuardedCursor(cursor, self.params.statement_timeout)


def exec_query(cursor: Cursor, sql: str, params: Any = None) -> Cursor:
    """Run a query and return the cursor; DEBUG-traces the text and params as a pair."""

    return driver.execute(_LOG, _is_timeout, cursor, sql, params)


def _default_cursor_factory(params: ConnectionParams) -> Any:
    """Open the file read-only; a missing one is refused rather than created."""

    path = Path(params.database)
    target = path.as_uri() if path.is_absolute() else f"file:{pathname2url(params.database)}"
    connection = sqlite3.connect(
        f"{target}?mode=ro",
        uri=True,
        check_same_thread=False,
        autocommit=True,
    )

    return connection.cursor()


class _GuardedCursor:
    """A cursor whose statements end on the run's interrupt or past their deadline.

    A progress handler ends the statement; which of the two fired is read back here, since
    SQLite reports both as the same `interrupted` error.
    """

    def __init__(self, cursor: Any, seconds: int | None) -> None:
        self._cursor = cursor
        self._seconds = seconds
        self._deadline: float | None = None
        self._timed_out = False
        self.row_counts: dict[str, int] = {}
        connection = cursor.connection
        functions.register(connection)
        connection.set_progress_handler(self._progress, _PROGRESS_STEPS)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def execute(self, sql: str, params: Any = None) -> Any:
        self._timed_out = False
        self._deadline = None if self._seconds is None else time.monotonic() + self._seconds

        try:
            if params is None:
                self._cursor.execute(sql)
            else:
                self._cursor.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorname", None) != "SQLITE_INTERRUPT":
                raise

            if self._timed_out:
                raise TimeoutError(f"statement interrupted after {self._seconds}s") from exc

            raise exc from KeyboardInterrupt()
        finally:
            self._deadline = None

        return self

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def close(self) -> None:
        self._cursor.close()

    def _progress(self) -> int:
        if interrupt.requested():
            return 1

        if self._deadline is not None and time.monotonic() > self._deadline:
            self._timed_out = True

            return 1

        return 0


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError)
