"""Each adapter's dialect and stats module, and a recorder of the statements a cursor runs."""

from __future__ import annotations

import re
from types import ModuleType
from typing import Any

from dbprint.adapters.bigquery import DIALECT as BIGQUERY_DIALECT
from dbprint.adapters.bigquery import stats as bigquery_stats
from dbprint.adapters.clickhouse import DIALECT as CLICKHOUSE_DIALECT
from dbprint.adapters.clickhouse import stats as clickhouse_stats
from dbprint.adapters.databricks import DIALECT as DATABRICKS_DIALECT
from dbprint.adapters.databricks import stats as databricks_stats
from dbprint.adapters.dialect import VENDOR_SUPPORT, Dialect, Vendor
from dbprint.adapters.duckdb import DIALECT as DUCKDB_DIALECT
from dbprint.adapters.duckdb import stats as duckdb_stats
from dbprint.adapters.mysql import DIALECT as MYSQL_DIALECT
from dbprint.adapters.mysql import stats as mysql_stats
from dbprint.adapters.postgres import DIALECT as POSTGRES_DIALECT
from dbprint.adapters.postgres import stats as postgres_stats
from dbprint.adapters.redshift import DIALECT as REDSHIFT_DIALECT
from dbprint.adapters.redshift import stats as redshift_stats
from dbprint.adapters.snowflake import DIALECT as SNOWFLAKE_DIALECT
from dbprint.adapters.snowflake import stats as snowflake_stats


DIALECTS: dict[str, Dialect] = {
    "postgres": POSTGRES_DIALECT,
    "mysql": MYSQL_DIALECT,
    "snowflake": SNOWFLAKE_DIALECT,
    "duckdb": DUCKDB_DIALECT,
    "clickhouse": CLICKHOUSE_DIALECT,
    "redshift": REDSHIFT_DIALECT,
    "databricks": DATABRICKS_DIALECT,
    "bigquery": BIGQUERY_DIALECT,
}


STATS_MODULES: dict[str, ModuleType] = {
    "postgres": postgres_stats,
    "mysql": mysql_stats,
    "snowflake": snowflake_stats,
    "duckdb": duckdb_stats,
    "clickhouse": clickhouse_stats,
    "redshift": redshift_stats,
    "databricks": databricks_stats,
    "bigquery": bigquery_stats,
}


def foreign_fragments(statement: str, vendor: Vendor) -> list[str]:
    """Fragments in `statement` that `vendor`'s engine does not accept."""

    flat = " ".join(statement.lower().split())

    return sorted(
        fragment
        for fragment, accepted_by in VENDOR_SUPPORT.items()
        if re.search(r"(?<![a-z_])" + re.escape(fragment), flat) and vendor not in accepted_by
    )


class Recorder:
    """Every statement one adapter emitted, in order."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.bound: list[tuple[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []

    def record(self, sql: str, params: Any) -> None:
        self.statements.append(sql)
        self.calls.append((sql, params))

        if params is not None:
            self.bound.append((sql, params))

    def flattened(self) -> list[str]:
        """Statements with whitespace collapsed and case folded."""

        return [" ".join(s.lower().split()) for s in self.statements]


def install_recorder(adapter: Any) -> Recorder:
    """Wrap the adapter's live cursor so every emitted statement is captured.

    Reaches into the connection object because the cursor is absent from the Adapter surface.
    """

    recorder = Recorder()
    connection = adapter._connection

    for attribute in ("_conn", "_cursor"):
        target = getattr(connection, attribute, None)

        if target is not None:
            setattr(connection, attribute, _RecordingProxy(target, recorder))

    return recorder


class _RecordingProxy:
    """Forward everything to the wrapped cursor/connection; record `execute`."""

    def __init__(self, target: Any, recorder: Recorder) -> None:
        self._target = target
        self._recorder = recorder

    def execute(self, sql: str, params: Any = None) -> Any:
        self._recorder.record(sql, params)

        if params is None:
            return self._target.execute(sql)

        return self._target.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)
