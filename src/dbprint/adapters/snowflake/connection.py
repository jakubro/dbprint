"""Cursor-factory wrapped session for the Snowflake adapter.

`cursor_factory` is the seam between production (snowflake-connector-python) and tests
(duckdb), both producing a `Cursor`-protocol object; the default factory imports the
connector lazily, so an injected duckdb cursor never pays the import cost.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import driver, interrupt
from ..dialect import Dialect
from ..driver import Cursor, FactoryConnection


# The connection opens with paramstyle="qmark"; statements bind via `?`.
DIALECT = Dialect(
    vendor="snowflake",
    paramstyle="qmark",
    quote_char='"',
    pair_distinct="COUNT(DISTINCT {a}, {b})",
)

_LOG = logging.getLogger(__name__)

# The vendor's own ceiling; a larger limit is refused rather than silently clamped.
STATEMENT_TIMEOUT_CEILING_SECONDS = 604_800

_POLL_SECONDS = 0.1
_CANCEL_ATTEMPTS = 2
_CANCEL_WAIT_SECONDS = 5.0


class SnowflakeConnectionError(RuntimeError):
    """Raised when the adapter cannot establish a working Snowflake session."""


@dataclass(frozen=True)
class ConnectionParams:
    """Resolved Snowflake credentials passed to the adapter.

    Auth is either-or: exactly one of `password` or `private_key_file` must be set.
    """

    account: str
    user: str
    warehouse: str
    role: str
    database: str | None = None
    password: str | None = None
    private_key_file: str | None = None
    private_key_file_pwd: str | None = None
    schema: str | None = None
    statement_timeout: int | None = None

    @classmethod
    def from_credentials(
        cls,
        creds: dict[str, str],
        statement_timeout: int | None = None,
    ) -> ConnectionParams:
        try:
            params = cls(
                account=creds["account"],
                user=creds["user"],
                warehouse=creds["warehouse"],
                database=creds.get("database"),
                role=creds["role"],
                password=creds.get("password"),
                private_key_file=creds.get("private_key_file"),
                private_key_file_pwd=creds.get("private_key_file_pwd"),
                schema=creds.get("schema"),
                statement_timeout=statement_timeout,
            )
        except KeyError as exc:
            raise SnowflakeConnectionError(
                f"missing required credential key: {exc.args[0]!r}",
            ) from exc

        if params.schema is not None and params.database is None:
            raise SnowflakeConnectionError("'schema' requires 'database'.")

        if (params.password is not None) == (params.private_key_file is not None):
            raise SnowflakeConnectionError(
                "Snowflake auth requires exactly one of a password or an RSA private key. "
                "Provide either 'password' or 'private_key_file' (env "
                "DBPRINT_<CONNECTION>_PASSWORD or DBPRINT_<CONNECTION>_PRIVATE_KEY_FILE), "
                "not both.",
            )

        return params


class Connection(FactoryConnection):
    """A Snowflake session opened by a cursor factory."""

    error = SnowflakeConnectionError
    vendor = "Snowflake"
    timeout_ceiling = STATEMENT_TIMEOUT_CEILING_SECONDS

    def _default_factory(self, params: ConnectionParams) -> Any:
        return _default_cursor_factory(params)

    def _open_failure(self, exc: Exception) -> str:
        return (
            f"could not open Snowflake session for account "
            f"{self.params.account!r}, user {self.params.user!r}: {exc}"
        )


def exec_query(cursor: Cursor, sql: str, params: Any = None) -> Cursor:
    """Run a query and return the cursor; DEBUG-traces the text and params as a pair."""

    return driver.execute(_LOG, _is_timeout, cursor, sql, params)


def exec_statements(cursor: Any, sql: str) -> Any:
    """Run user-authored SQL of any number of statements in one call and return the cursor.

    Snowflake refuses a second statement in a call that does not name a count, and 0 names any.
    """

    def run() -> Any:
        cursor.execute(sql, num_statements=0)

        return cursor

    return driver.traced(_LOG, _is_timeout, run, sql, None)


def _default_cursor_factory(params: ConnectionParams) -> Any:
    """Build a real snowflake-connector cursor; raises if the extra is missing."""

    connector = driver.import_extra(
        "snowflake.connector",
        "snowflake-connector-python",
        "snowflake",
        SnowflakeConnectionError,
    )

    connect_kwargs: dict[str, Any] = {
        "account": params.account,
        "user": params.user,
        "warehouse": params.warehouse,
        "role": params.role,
        # The adapter writes `?` placeholders; the connector's default pyformat binds
        # client-side via `command % params` and fails on them.
        "paramstyle": "qmark",
        # Unset, connector 5 warns on each connect(); False logs out on close (no async queries).
        "enable_server_session_keep_alive_auto_detection": False,
    }

    if params.database is not None:
        connect_kwargs["database"] = params.database

    if params.schema is not None:
        connect_kwargs["schema"] = params.schema

    if params.statement_timeout is not None:
        connect_kwargs["session_parameters"] = {
            "STATEMENT_TIMEOUT_IN_SECONDS": params.statement_timeout,
        }

    if params.private_key_file is not None:
        connect_kwargs["private_key"] = _load_private_key(
            params.private_key_file,
            params.private_key_file_pwd,
        )
    else:
        connect_kwargs["password"] = params.password

    return _CancellableCursor(connector.connect(**connect_kwargs))


def _load_private_key(path: str, passphrase: str | None) -> bytes:
    """Load a PEM PKCS8 RSA key into DER bytes for `connect(private_key=...)`.

    Read and parse failures surface as `SnowflakeConnectionError` carrying the path and cause.
    """

    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:
        raise SnowflakeConnectionError(
            "key-pair auth needs `cryptography`, which ships with the "
            "[snowflake] extra: `pip install dbprint[snowflake]`.",
        ) from exc

    try:
        data = Path(path).read_bytes()
        key = serialization.load_pem_private_key(
            data,
            password=passphrase.encode() if passphrase else None,
        )

        return key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    except Exception as exc:
        raise SnowflakeConnectionError(
            f"could not load Snowflake private key from {path!r}: {exc}",
        ) from exc


class _CancellableCursor:
    """Runs each statement on a helper thread, cancelling it on a Ctrl-C connector 5 never sees."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._cursor = connection.cursor()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def execute(self, sql: str, params: Any = None, num_statements: int | None = None) -> Any:
        interrupt.raise_if_requested()

        finished = threading.Event()
        outcome: list[Any] = []
        counted = {} if num_statements is None else {"num_statements": num_statements}

        def run() -> None:
            try:
                outcome.append(
                    self._cursor.execute(sql, **counted)
                    if params is None
                    else self._cursor.execute(sql, params, **counted),
                )
            except BaseException as exc:  # noqa: BLE001 - handed to the waiting thread, which raises it
                outcome.append(exc)
            finally:
                finished.set()

        try:
            threading.Thread(target=run, daemon=True).start()

            while not finished.wait(_POLL_SECONDS):
                interrupt.raise_if_requested()
        except BaseException as interrupted:
            self._cancel(finished)

            if outcome and isinstance(outcome[0], BaseException):
                raise outcome[0] from interrupted

            raise

        if isinstance(outcome[0], BaseException):
            raise outcome[0]

        return outcome[0]

    def _cancel(self, finished: threading.Event) -> None:
        # A session runs one statement at a time, so this cancels exactly the interrupted one; the
        # second attempt covers a cancel that reached the server before the statement did.
        for _ in range(_CANCEL_ATTEMPTS):
            try:
                canceller = self._connection.cursor()

                try:
                    canceller.execute(
                        "SELECT SYSTEM$CANCEL_ALL_QUERIES(?)",
                        (self._connection.session_id,),
                    )
                finally:
                    canceller.close()
            except Exception as exc:  # noqa: BLE001 - the run ends interrupted either way
                _LOG.debug("cancelling the interrupted statement failed: %s", exc)

            if finished.wait(_CANCEL_WAIT_SECONDS):
                return

    def close(self) -> None:
        try:
            self._cursor.close()
        finally:
            self._connection.close()


def _is_timeout(exc: BaseException) -> bool:
    # 000630: "Statement reached its statement or warehouse timeout ... and was canceled."
    return getattr(exc, "errno", None) == 630
