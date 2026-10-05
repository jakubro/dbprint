"""psycopg3 session lifecycle + pg_dump availability check.

`pg_dump` is probed at `connect()` time, not when DDL extraction needs it, so a missing
binary fails fast with an actionable error.
"""

from __future__ import annotations

import importlib
import logging
import shutil
import subprocess
from typing import TYPE_CHECKING, Any, LiteralString, cast

from .. import driver
from ..dialect import Dialect
from ..driver import ServerParams


# psycopg3 defaults to pyformat and the adapter does not override it.
DIALECT = Dialect(
    vendor="postgres",
    paramstyle="pyformat",
    quote_char='"',
    addressed_parts=2,
    text_type="TEXT",
)

if TYPE_CHECKING:
    import psycopg


_LOG = logging.getLogger(__name__)

PG_DUMP_BIN = "pg_dump"
PG_DUMP_PROBE_TIMEOUT_SECONDS = 5
PG_DUMP_TIMEOUT_SECONDS = 60


class PostgresConnectionError(RuntimeError):
    """Raised when the adapter cannot establish a working Postgres session."""


class ConnectionParams(ServerParams):
    """Resolved Postgres credentials passed to the adapter."""

    error = PostgresConnectionError

    def env_for_pg_dump(self, database: str) -> dict[str, str]:
        """libpq env vars consumed by a pg_dump of one relation in `database`."""

        return {
            "PGHOST": self.host,
            "PGPORT": str(self.port),
            "PGDATABASE": database,
            "PGUSER": self.user,
            "PGPASSWORD": self.password,
        }


class Connection:
    """Wraps a psycopg connection plus the pg_dump availability check."""

    def __init__(self, params: ConnectionParams) -> None:
        self.params = params
        self._conn: psycopg.Connection | None = None

    def sibling(self) -> Connection:
        """An unopened connection with the same parameters."""

        return Connection(self.params)

    def open(self) -> None:
        psycopg = _import_psycopg()
        ensure_pg_dump_available()

        try:
            self._conn = psycopg.connect(
                host=self.params.host,
                port=self.params.port,
                dbname=self.params.database,
                user=self.params.user,
                password=self.params.password,
                autocommit=True,
                **_session_options(self.params.statement_timeout),
            )
        except psycopg.Error as exc:
            raise PostgresConnectionError(
                f"could not connect to Postgres at {self.params.host}:{self.params.port}/"
                f"{self.params.database} as {self.params.user!r}: {exc}",
            ) from exc

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def psycopg_connection(self) -> psycopg.Connection:
        if self._conn is None:
            raise PostgresConnectionError("connection is not open; call connect() first")

        return self._conn

    def is_open(self) -> bool:
        return self._conn is not None and not self._conn.closed


def exec_query(conn: psycopg.Connection, query: str, params: Any = None) -> psycopg.Cursor:
    """Run a dynamically-built SQL query; the `LiteralString` cast rests on catalog-only input.

    Traces the statement at DEBUG: text and parameters as a pair, never merged.
    """

    return driver.traced(
        _LOG,
        _is_timeout,
        lambda: conn.execute(cast(LiteralString, query), params),
        query,
        params,
    )


def ensure_pg_dump_available() -> None:
    """Raise `PostgresConnectionError` when `pg_dump` is missing or unrunnable."""

    if shutil.which(PG_DUMP_BIN) is None:
        raise PostgresConnectionError(
            "pg_dump binary not found on PATH. Install postgresql-client "
            "(Debian/Ubuntu: `apt install postgresql-client`; "
            "macOS: `brew install libpq && brew link --force libpq`).",
        )

    try:
        subprocess.run(
            [PG_DUMP_BIN, "--version"],
            check=True,
            capture_output=True,
            timeout=PG_DUMP_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise PostgresConnectionError(
            f"pg_dump is present but does not run successfully: {exc}",
        ) from exc


def _import_psycopg() -> Any:
    """Import psycopg lazily; raise an actionable error when the extra is absent."""

    try:
        return importlib.import_module("psycopg")
    except ImportError as exc:
        raise PostgresConnectionError(
            "psycopg is not installed. Install dbprint with the [postgres] extra: "
            "`pip install dbprint[postgres]`.",
        ) from exc


def _session_options(statement_timeout: int | None) -> dict[str, str]:
    # Sent in the startup packet, so the limit costs no round trip of its own.
    if statement_timeout is None:
        return {}

    return {"options": f"-c statement_timeout={statement_timeout * 1000}"}


def _is_timeout(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == "57014"
