"""BigQuery session lifecycle. See ARCHITECTURE.md 2 - `cursor_factory` is the seam every
adapter here exposes, and read-only is the connected principal's own IAM role.
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


class BigqueryConnectionError(RuntimeError):
    """Raised when the adapter cannot open a working BigQuery session."""


@dataclass(frozen=True)
class ConnectionParams:
    """Resolved BigQuery credentials passed to the adapter."""

    project: str
    dataset: str
    credentials_file: str | None = None

    @classmethod
    def from_credentials(cls, creds: dict[str, str]) -> ConnectionParams:
        try:
            return cls(
                project=creds["project"],
                dataset=creds["dataset"],
                credentials_file=creds.get("credentials_file"),
            )
        except KeyError as exc:
            raise BigqueryConnectionError(
                f"missing required credential key: {exc.args[0]!r}",
            ) from exc


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
        except BigqueryConnectionError:
            raise
        except Exception as exc:
            raise BigqueryConnectionError(
                f"could not open a BigQuery session for project {self.params.project!r}, "
                f"dataset {self.params.dataset!r}: {exc}",
            ) from exc

    def close(self) -> None:
        if self._cursor is not None:
            try:
                self._cursor.close()
            except Exception:  # noqa: BLE001, S110 - close-time failure is uninteresting
                pass

            self._cursor = None

    def is_open(self) -> bool:
        return self._cursor is not None

    @property
    def cursor(self) -> Cursor:
        if self._cursor is None:
            raise BigqueryConnectionError("connection is not open; call connect() first")

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
    """Open a real google-cloud-bigquery DB-API connection; raises an install hint if absent.
    Credentials resolve through ADC unless `credentials_file` names a service account key.
    """

    try:
        bigquery = importlib.import_module("google.cloud.bigquery")
        dbapi = importlib.import_module("google.cloud.bigquery.dbapi")
    except ImportError as exc:
        raise BigqueryConnectionError(
            "google-cloud-bigquery is not installed. Install dbprint with the [bigquery] "
            "extra: `pip install dbprint[bigquery]`.",
        ) from exc

    if params.credentials_file:
        service_account = importlib.import_module("google.oauth2.service_account")
        credentials = service_account.Credentials.from_service_account_file(
            params.credentials_file,
        )
        client = bigquery.Client(project=params.project, credentials=credentials)
    else:
        client = bigquery.Client(project=params.project)

    return dbapi.connect(client).cursor()
