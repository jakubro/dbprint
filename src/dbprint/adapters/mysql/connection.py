"""mysql-connector session lifecycle for the MySQL adapter.

One buffered cursor is shared for the adapter's lifetime: buffering materializes the full
result set on `execute`, so sequential queries and `fetchone` never trip the "unread result
found" error. The driver is imported lazily, so dbprint imports without the `[mysql]` extra.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from .. import driver
from ..dialect import Dialect
from ..driver import Cursor, ServerParams


# mysql-connector-python defaults to pyformat; the adapter does not override it.
DIALECT = Dialect(
    vendor="mysql",
    paramstyle="pyformat",
    quote_char="`",
    text_type="CHAR",
    concat_null_flags=True,
    pair_distinct="COUNT(DISTINCT {a}, {b})",
    seed_hash="MD5(CONCAT({seed}, CAST({value} AS CHAR)))",
)

_LOG = logging.getLogger(__name__)

_UNKNOWN_SYSTEM_VARIABLE = 1193

# ER_QUERY_TIMEOUT (Oracle MySQL) and ER_STATEMENT_TIMEOUT (MariaDB).
_TIMEOUT_ERRNOS = (3024, 1969)


class MysqlConnectionError(RuntimeError):
    """Raised when the adapter cannot establish a working MySQL session."""


class ConnectionParams(ServerParams):
    """Resolved MySQL credentials passed to the adapter."""

    error = MysqlConnectionError


class Connection:
    """Wraps a mysql-connector session plus its shared buffered cursor."""

    def __init__(self, params: ConnectionParams) -> None:
        self.params = params
        self._conn: Any | None = None
        self._cursor: Cursor | None = None
        self.mariadb = False

    def sibling(self) -> Connection:
        """An unopened connection with the same parameters."""

        return Connection(self.params)

    def open(self) -> None:
        connector = _import_connector()

        try:
            # With no database the session has no default; every catalog read names its own.
            database = {} if self.params.database is None else {"database": self.params.database}
            self._conn = connector.connect(
                host=self.params.host,
                port=self.params.port,
                user=self.params.user,
                password=self.params.password,
                autocommit=True,
                **database,
            )
        except connector.Error as exc:
            where = f"{self.params.host}:{self.params.port}"
            where += f"/{self.params.database}" if self.params.database is not None else ""

            raise MysqlConnectionError(
                f"could not connect to MySQL at {where} as {self.params.user!r}: {exc}",
            ) from exc

        self._cursor = self._conn.cursor(buffered=True)
        self.mariadb = "mariadb" in str(self._conn.server_info).lower()

        if self.params.statement_timeout is not None:
            try:
                _limit_statements(self._cursor, self.params.statement_timeout, connector.Error)
            except connector.Error as exc:
                raise MysqlConnectionError(f"could not set statement_timeout: {exc}") from exc

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

    return driver.execute(_LOG, _is_timeout, cursor, sql, params)


def _import_connector() -> Any:
    """Import mysql.connector lazily; raise an actionable error when absent."""

    try:
        return importlib.import_module("mysql.connector")
    except ImportError as exc:
        raise MysqlConnectionError(
            "mysql-connector-python is not installed. Install dbprint with the "
            "[mysql] extra: `pip install dbprint[mysql]`.",
        ) from exc


def _limit_statements(cursor: Cursor, seconds: int, error: type[Exception]) -> None:
    # Oracle MySQL bounds read-only SELECTs in ms; MariaDB names its own variable, in seconds.
    try:
        cursor.execute(f"SET SESSION max_execution_time = {seconds * 1000}")
    except error as exc:
        if getattr(exc, "errno", None) != _UNKNOWN_SYSTEM_VARIABLE:
            raise

        cursor.execute(f"SET SESSION max_statement_time = {seconds}")


def _is_timeout(exc: BaseException) -> bool:
    return getattr(exc, "errno", None) in _TIMEOUT_ERRNOS
