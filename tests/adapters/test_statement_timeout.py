"""`statement_timeout`: enforced where a substrate runs locally, classified on every adapter.

An injected cursor never reaches the default factory that holds the setting; only classification runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.adapters import DuckdbAdapter, MysqlAdapter, PostgresAdapter
from dbprint.adapters.bigquery import connection as bigquery_connection
from dbprint.adapters.clickhouse import connection as clickhouse_connection
from dbprint.adapters.databricks import connection as databricks_connection
from dbprint.adapters.databricks.connection import ConnectionParams as DatabricksParams
from dbprint.adapters.databricks.connection import DatabricksConnectionError
from dbprint.adapters.errors import QueryFailed
from dbprint.adapters.mysql import connection as mysql_connection
from dbprint.adapters.postgres import connection as postgres_connection
from dbprint.adapters.redshift import connection as redshift_connection
from dbprint.adapters.snowflake import connection as snowflake_connection
from dbprint.adapters.snowflake.connection import ConnectionParams as SnowflakeParams
from dbprint.adapters.snowflake.connection import SnowflakeConnectionError


class TestEnforcedOnALiveSubstrate:
    def test_postgres_cancels_the_statement_in_the_server(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        adapter = PostgresAdapter(postgres_test_db, statement_timeout=1)
        adapter.connect()

        try:
            with pytest.raises(QueryFailed) as caught:
                adapter.execute_query("SELECT pg_sleep(5)")
        finally:
            adapter.close()

        assert caught.value.timed_out is True

    def test_postgres_without_a_limit_runs_to_completion(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            assert adapter.execute_query("SELECT pg_sleep(1.2), 1") == [("", 1)]
        finally:
            adapter.close()

    def test_mariadb_cancels_the_statement_in_the_server(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        adapter = MysqlAdapter(mysql_test_db, statement_timeout=1)
        adapter.connect()

        try:
            with pytest.raises(QueryFailed) as caught:
                # BENCHMARK and SLEEP return quietly when interrupted; a scan raises.
                adapter.execute_query(
                    "SELECT COUNT(*) FROM information_schema.columns a, "
                    "information_schema.columns b, information_schema.columns c",
                )
        finally:
            adapter.close()

        assert caught.value.timed_out is True

    def test_duckdb_interrupts_from_the_client(self) -> None:
        adapter = DuckdbAdapter({"database": ":memory:"}, statement_timeout=1)
        adapter.connect()

        try:
            with pytest.raises(QueryFailed) as caught:
                adapter.execute_query(
                    "SELECT sum(a.range * b.range) FROM range(200000) a, range(200000) b",
                )

            assert adapter.execute_query("SELECT 42") == [(42,)]
        finally:
            adapter.close()

        assert caught.value.timed_out is True

    def test_a_duckdb_failure_that_is_not_the_limit_is_not_a_timeout(self) -> None:
        adapter = DuckdbAdapter({"database": ":memory:"}, statement_timeout=30)
        adapter.connect()

        try:
            with pytest.raises(QueryFailed) as caught:
                adapter.execute_query("SELECT * FROM nowhere")
        finally:
            adapter.close()

        assert caught.value.timed_out is False


class TestTheVendorCeiling:
    def test_snowflake_refuses_a_limit_past_seven_days(self) -> None:
        params = SnowflakeParams.from_credentials(
            {
                "account": "a",
                "user": "u",
                "warehouse": "w",
                "database": "d",
                "role": "r",
                "password": "p",
            },
            statement_timeout=8 * 86400,
        )

        with pytest.raises(SnowflakeConnectionError, match="ceiling of 7d"):
            snowflake_connection.Connection(params, _unreachable_factory).open()

    def test_databricks_refuses_a_limit_past_two_days(self) -> None:
        params = DatabricksParams.from_credentials(
            {"server_hostname": "h", "http_path": "p", "access_token": "t", "catalog": "c"},
            statement_timeout=3 * 86400,
        )

        with pytest.raises(DatabricksConnectionError, match="ceiling of 2d"):
            databricks_connection.Connection(params, _unreachable_factory).open()

    def test_a_limit_at_the_ceiling_opens(self) -> None:
        params = DatabricksParams.from_credentials(
            {"server_hostname": "h", "http_path": "p", "access_token": "t", "catalog": "c"},
            statement_timeout=2 * 86400,
        )
        connection = databricks_connection.Connection(params, lambda _: _Closeable())

        connection.open()

        assert connection.is_open()


class TestClassification:
    """Each driver's own timeout signal, and a neighbouring failure that must not read as one."""

    def test_postgres_query_canceled(self) -> None:
        assert postgres_connection._is_timeout(_with(sqlstate="57014"))
        assert not postgres_connection._is_timeout(_with(sqlstate="42P01"))

    def test_mysql_and_mariadb_errnos(self) -> None:
        assert mysql_connection._is_timeout(_with(errno=3024))
        assert mysql_connection._is_timeout(_with(errno=1969))
        assert not mysql_connection._is_timeout(_with(errno=1146))

    def test_snowflake_000630(self) -> None:
        assert snowflake_connection._is_timeout(_with(errno=630))
        assert not snowflake_connection._is_timeout(_with(errno=2003))

    def test_clickhouse_code_159(self) -> None:
        assert clickhouse_connection._is_timeout(_with(code=159))
        assert clickhouse_connection._is_timeout(RuntimeError("Code: 159. DB::Exception: Timeout"))
        assert not clickhouse_connection._is_timeout(_with(code=60))

    def test_redshift_error_sqlstate(self) -> None:
        assert redshift_connection._is_timeout(RuntimeError({"C": "57014", "M": "canceled"}))
        assert not redshift_connection._is_timeout(RuntimeError({"C": "42P01"}))
        assert not redshift_connection._is_timeout(RuntimeError("plain message"))

    def test_databricks_error_class(self) -> None:
        assert databricks_connection._is_timeout(
            RuntimeError("[QUERY_EXECUTION_TIMEOUT_EXCEEDED] Query was cancelled"),
        )
        assert not databricks_connection._is_timeout(RuntimeError("[TABLE_OR_VIEW_NOT_FOUND]"))

    def test_bigquery_job_reason(self) -> None:
        assert bigquery_connection._is_timeout(RuntimeError(_with(errors=[{"reason": "timeout"}])))
        assert not bigquery_connection._is_timeout(
            RuntimeError(_with(errors=[{"reason": "notFound"}])),
        )


class _Closeable:
    def close(self) -> None:
        pass


def _unreachable_factory(_: object) -> Any:
    raise AssertionError("the connection was attempted past the ceiling")


def _with(**attributes: Any) -> Exception:
    exc = RuntimeError("driver error")

    for name, value in attributes.items():
        setattr(exc, name, value)

    return exc
