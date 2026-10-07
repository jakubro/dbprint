"""mysql-connector session lifecycle for the MySQL adapter.

One buffered cursor is shared for the adapter's lifetime: buffering materializes the full
result set on `execute`, so sequential queries and `fetchone` never trip the "unread result
found" error. The driver is imported lazily, so dbprint imports without the `[mysql]` extra.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import driver
from ..dialect import Dialect
from ..driver import Cursor, CursorFactory, FactoryConnection, ServerParams


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


class Connection(FactoryConnection):
    """A mysql-connector session holding one buffered cursor; `mariadb` names the server family.

    The factory opens the driver connection, not a cursor: the session closes both.
    """

    error = MysqlConnectionError
    vendor = "MySQL"

    def __init__(
        self,
        params: ConnectionParams,
        cursor_factory: CursorFactory[ConnectionParams] | None = None,
    ) -> None:
        super().__init__(params, cursor_factory)
        self._conn: Any | None = None
        self.mariadb = False

    def close(self) -> None:
        super().close()

        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001, S110 - close-time failure is uninteresting
                pass

            self._conn = None

    def is_open(self) -> bool:
        return self._conn is not None and bool(self._conn.is_connected())

    def _default_factory(self, params: ConnectionParams) -> Any:
        connector = _import_connector()
        # With no database the session has no default; every catalog read names its own.
        database = {} if params.database is None else {"database": params.database}

        return connector.connect(
            host=params.host,
            port=params.port,
            user=params.user,
            password=params.password,
            autocommit=True,
            **database,
        )

    def _open_failure(self, exc: Exception) -> str:
        return driver.connect_failure("MySQL", self.params, exc)

    def _opened(self, connection: Any, /) -> Any:
        self._conn = connection
        cursor = connection.cursor(buffered=True)
        self.mariadb = "mariadb" in str(connection.server_info).lower()

        if self.params.statement_timeout is not None:
            error = _import_connector().Error

            try:
                _limit_statements(cursor, self.params.statement_timeout, error)
            except error as exc:
                raise MysqlConnectionError(f"could not set statement_timeout: {exc}") from exc

        return cursor


def exec_query(cursor: Cursor, sql: str, params: Any = None) -> Cursor:
    """Run a query and return the cursor; DEBUG-traces the text and params as a pair."""

    return driver.execute(_LOG, _is_timeout, cursor, sql, params)


def _import_connector() -> Any:
    """Import mysql.connector lazily; raise an actionable error when absent."""

    return driver.import_extra(
        "mysql.connector",
        "mysql-connector-python",
        "mysql",
        MysqlConnectionError,
    )


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
