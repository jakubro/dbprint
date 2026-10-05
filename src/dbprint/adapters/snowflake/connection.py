"""Cursor-factory wrapped session for the Snowflake adapter.

`cursor_factory` is the seam between production (snowflake-connector-python) and tests
(duckdb), both producing a `Cursor`-protocol object; the default factory imports the
connector lazily, so an injected duckdb cursor never pays the import cost.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import driver
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
        "role": params.role,
        # The adapter writes `?` placeholders; the connector's default pyformat binds
        # client-side via `command % params` and fails on them.
        "paramstyle": "qmark",
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


def _is_timeout(exc: BaseException) -> bool:
    # 000630: "Statement reached its statement or warehouse timeout ... and was canceled."
    return getattr(exc, "errno", None) == 630
