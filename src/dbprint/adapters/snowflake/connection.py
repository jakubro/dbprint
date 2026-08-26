"""Cursor-factory wrapped session for the Snowflake adapter.

`cursor_factory` is the seam between production (snowflake-connector-python) and tests
(duckdb), both producing a `Cursor`-protocol object; the default factory imports the
connector lazily, so an injected duckdb cursor never pays the import cost.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .. import trace_context
from ..errors import QueryFailed


_LOG = logging.getLogger(__name__)


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
    database: str
    role: str
    password: str | None = None
    private_key_file: str | None = None
    private_key_file_pwd: str | None = None
    schema: str | None = None

    @classmethod
    def from_credentials(cls, creds: dict[str, str]) -> ConnectionParams:
        try:
            params = cls(
                account=creds["account"],
                user=creds["user"],
                warehouse=creds["warehouse"],
                database=creds["database"],
                role=creds["role"],
                password=creds.get("password"),
                private_key_file=creds.get("private_key_file"),
                private_key_file_pwd=creds.get("private_key_file_pwd"),
                schema=creds.get("schema"),
            )
        except KeyError as exc:
            raise SnowflakeConnectionError(
                f"missing required credential key: {exc.args[0]!r}",
            ) from exc

        if (params.password is not None) == (params.private_key_file is not None):
            raise SnowflakeConnectionError(
                "Snowflake auth requires exactly one of a password or an RSA private key. "
                "Provide either 'password' or 'private_key_file' (env "
                "DBPRINT_<CONN>_PASSWORD or DBPRINT_<CONN>_PRIVATE_KEY_FILE), not both.",
            )

        return params


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
        except SnowflakeConnectionError:
            raise
        except Exception as exc:
            raise SnowflakeConnectionError(
                f"could not open Snowflake session for account "
                f"{self.params.account!r}, user {self.params.user!r}: {exc}",
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
            raise SnowflakeConnectionError("connection is not open; call connect() first")

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
    """Build a real snowflake-connector cursor; raises if the extra is missing."""

    try:
        connector = importlib.import_module("snowflake.connector")
    except ImportError as exc:
        raise SnowflakeConnectionError(
            "snowflake-connector-python is not installed. Install dbprint with the "
            "[snowflake] extra: `pip install dbprint[snowflake]`.",
        ) from exc

    connect_kwargs: dict[str, Any] = {
        "account": params.account,
        "user": params.user,
        "warehouse": params.warehouse,
        "database": params.database,
        "role": params.role,
        # The adapter writes `?` placeholders; the connector's default pyformat binds
        # client-side via `command % params` and fails on them.
        "paramstyle": "qmark",
    }

    if params.schema is not None:
        connect_kwargs["schema"] = params.schema

    if params.private_key_file is not None:
        connect_kwargs["private_key"] = _load_private_key(
            params.private_key_file,
            params.private_key_file_pwd,
        )
    else:
        connect_kwargs["password"] = params.password

    connection = connector.connect(**connect_kwargs)

    return connection.cursor()


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
