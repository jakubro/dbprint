"""ClickHouse session lifecycle. See ARCHITECTURE.md 2 - no connection-level `SETTINGS
readonly` is applied; read-only is the connected user's own grants.
"""

from __future__ import annotations

import importlib
import logging
from types import MappingProxyType
from typing import Any

from .. import driver
from ..dialect import Dialect
from ..driver import Cursor, FactoryConnection, ServerParams


# clickhouse-connect's DB-API defaults to pyformat (%s); the adapter does not override it.
DIALECT = Dialect(
    vendor="clickhouse",
    paramstyle="pyformat",
    quote_char="`",
    row_count="count()",
    count_fn="count",
    distinct_count="uniqExact({})",
    text_type=None,
    order_by_alias=True,
    group_by_ordinal=False,
    concat_null_flags=True,
    pair_distinct="uniqExact(tuple({a}, {b}))",
    seed_hash="halfMD5(concat({seed}, toString({value})))",
)

_LOG = logging.getLogger(__name__)


class ClickhouseConnectionError(RuntimeError):
    """Raised when the adapter cannot open a working ClickHouse session."""


class ConnectionParams(ServerParams):
    """Resolved ClickHouse credentials passed to the adapter."""

    error = ClickhouseConnectionError
    defaults = MappingProxyType({"port": "8123", "user": "default", "password": ""})


class Connection(FactoryConnection):
    """A ClickHouse session opened by a cursor factory."""

    error = ClickhouseConnectionError
    vendor = "ClickHouse"

    def _default_factory(self, params: ConnectionParams) -> Any:
        return _default_cursor_factory(params)

    def _open_failure(self, exc: Exception) -> str:
        where = f"{self.params.host}:{self.params.port}"
        where += f"/{self.params.database}" if self.params.database is not None else ""

        return f"could not connect to ClickHouse at {where} as {self.params.user!r}: {exc}"


def exec_query(cursor: Cursor, sql: str, params: Any = None) -> Cursor:
    """Run a query and return the cursor; DEBUG-traces the text and params as a pair."""

    return driver.execute(_LOG, _is_timeout, cursor, sql, params)


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
