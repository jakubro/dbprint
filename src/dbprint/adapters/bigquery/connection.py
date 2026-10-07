"""BigQuery session lifecycle. See ARCHITECTURE.md 2 - `cursor_factory` is the seam every
adapter here exposes, and read-only is the connected principal's own IAM role.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import driver
from ..dialect import Dialect
from ..driver import Cursor, FactoryConnection


DIALECT = Dialect(
    vendor="bigquery",
    paramstyle="pyformat",
    quote_char="`",
    text_type="STRING",
    order_by_alias=True,
    group_by_ordinal=False,
    concat_null_flags=True,
    # `COUNT(DISTINCT)` takes one expression, so the pair hashes through its JSON text (measured).
    pair_distinct="COUNT(DISTINCT TO_JSON_STRING(STRUCT({a}, {b})))",
    seed_hash="TO_HEX(MD5(CONCAT({seed}, CAST({value} AS STRING))))",
)

_LOG = logging.getLogger(__name__)


class BigqueryConnectionError(RuntimeError):
    """Raised when the adapter cannot open a working BigQuery session."""


@dataclass(frozen=True)
class ConnectionParams:
    """Resolved BigQuery credentials passed to the adapter."""

    project: str
    dataset: str | None = None
    credentials_file: str | None = None
    statement_timeout: int | None = None

    @classmethod
    def from_credentials(
        cls,
        creds: dict[str, str],
        statement_timeout: int | None = None,
    ) -> ConnectionParams:
        try:
            return cls(
                project=creds["project"],
                dataset=creds.get("dataset"),
                credentials_file=creds.get("credentials_file"),
                statement_timeout=statement_timeout,
            )
        except KeyError as exc:
            raise BigqueryConnectionError(
                f"missing required credential key: {exc.args[0]!r}",
            ) from exc


# `datasets.list` is a client call, not SQL, so it has a seam of its own beside the cursor's.
DatasetLister = Callable[[ConnectionParams], list[str]]


class Connection(FactoryConnection):
    """A BigQuery session opened by a cursor factory."""

    error = BigqueryConnectionError
    vendor = "BigQuery"

    def _default_factory(self, params: ConnectionParams) -> Any:
        return _default_cursor_factory(params)

    def _open_failure(self, exc: Exception) -> str:
        dataset = "" if self.params.dataset is None else f", dataset {self.params.dataset!r}"

        return (
            f"could not open a BigQuery session for project {self.params.project!r}{dataset}: {exc}"
        )


def exec_query(cursor: Cursor, sql: str, params: Any = None) -> Cursor:
    """Run a query and return the cursor; DEBUG-traces the text and params as a pair."""

    return driver.execute(_LOG, _is_timeout, cursor, sql, params)


def _default_cursor_factory(params: ConnectionParams) -> Any:
    """Open a real google-cloud-bigquery DB-API connection; raises an install hint if absent.
    Credentials resolve through ADC unless `credentials_file` names a service account key.
    """

    bigquery = _import_bigquery("google.cloud.bigquery")
    dbapi = _import_bigquery("google.cloud.bigquery.dbapi")

    return dbapi.connect(_client(bigquery, params)).cursor()


def default_dataset_lister(params: ConnectionParams) -> list[str]:
    """Every dataset in the project the caller can see - billed as no query, filtered to those
    it holds `bigquery.datasets.get` on, and leaving hidden datasets out.
    """

    bigquery = _import_bigquery("google.cloud.bigquery")

    client = _client(bigquery, params)

    return [item.dataset_id for item in client.list_datasets(params.project)]


def _client(bigquery: Any, params: ConnectionParams) -> Any:
    options: dict[str, Any] = {}

    if params.statement_timeout is not None:
        options["default_query_job_config"] = bigquery.QueryJobConfig(
            job_timeout_ms=params.statement_timeout * 1000,
        )

    if params.credentials_file:
        service_account = importlib.import_module("google.oauth2.service_account")
        options["credentials"] = service_account.Credentials.from_service_account_file(
            params.credentials_file,
        )

    return bigquery.Client(project=params.project, **options)


def _is_timeout(exc: BaseException) -> bool:
    # The DB-API error wraps the job's GoogleCloudError, whose `errors` carry the reason.
    cause = exc.args[0] if exc.args else exc
    errors = getattr(cause, "errors", None) or []

    return any(isinstance(error, dict) and error.get("reason") == "timeout" for error in errors)


def _import_bigquery(module: str) -> Any:
    return driver.import_extra(module, "google-cloud-bigquery", "bigquery", BigqueryConnectionError)
