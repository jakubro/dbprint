"""`new_session`: an unconnected sibling on the same target, sharing identifier maps, never sessions."""

from __future__ import annotations

from typing import Any, LiteralString, cast

import mysql.connector
import pytest

from dbprint.adapters import (
    BigqueryAdapter,
    ClickhouseAdapter,
    DatabricksAdapter,
    DuckdbAdapter,
    MysqlAdapter,
    PostgresAdapter,
    RedshiftAdapter,
    SnowflakeAdapter,
)
from dbprint.adapters.mysql import connection as mysql_connection
from dbprint.adapters.postgres import connection as postgres_connection
from tests.conftest import pg_connect


_ADAPTERS: dict[str, tuple[Any, dict[str, Any], tuple[str, ...]]] = {
    "postgres": (
        PostgresAdapter,
        {"host": "h", "port": "5432", "user": "u", "password": "p"},
        ("_identities",),
    ),
    "mysql": (
        MysqlAdapter,
        {"host": "h", "port": "3306", "user": "u", "password": "p"},
        ("_identities",),
    ),
    "redshift": (
        RedshiftAdapter,
        {"host": "h", "user": "u", "password": "p"},
        ("_identities",),
    ),
    "snowflake": (
        SnowflakeAdapter,
        {"account": "a", "user": "u", "warehouse": "w", "role": "r", "password": "p"},
        ("_identities",),
    ),
    "clickhouse": (
        ClickhouseAdapter,
        {"host": "h"},
        ("_samplable", "_identities"),
    ),
    "bigquery": (
        BigqueryAdapter,
        {"project": "p"},
        ("_ddl_cache", "_identities"),
    ),
    "databricks": (
        DatabricksAdapter,
        {"server_hostname": "h", "http_path": "p", "access_token": "t"},
        ("_identities",),
    ),
    "duckdb": (DuckdbAdapter, {"database": ":memory:"}, ("_identities",)),
}


@pytest.fixture(params=sorted(_ADAPTERS))
def pair(request: pytest.FixtureRequest) -> tuple[Any, Any, tuple[str, ...]]:
    cls, credentials, shared = _ADAPTERS[request.param]
    adapter = cls(credentials, statement_timeout=30)

    return adapter, adapter.new_session(), shared


def test_the_session_is_its_own(pair: tuple[Any, Any, tuple[str, ...]]) -> None:
    adapter, session, _ = pair

    assert type(session) is type(adapter)
    assert session._connection is not adapter._connection
    assert session._connection.params == adapter._connection.params


def test_the_identifier_maps_are_shared_by_reference(
    pair: tuple[Any, Any, tuple[str, ...]],
) -> None:
    adapter, session, shared = pair

    for name in shared:
        assert getattr(session, name) is getattr(adapter, name), name


@pytest.mark.parametrize("vendor", ["postgres", "redshift"])
def test_per_database_sessions_are_never_shared(vendor: str) -> None:
    """A table's database session belongs to one worker, whose sample copies live on it."""

    cls, credentials, _ = _ADAPTERS[vendor]
    adapter: Any = cls(credentials)
    session: Any = adapter.new_session()

    assert session._sessions is not adapter._sessions
    assert list(session._sessions.values()) == [session._connection]


class TestTheStringLiteralGrammarIsPinned:
    """A key read back from the data is spelled into SQL under one grammar each session pins."""

    def test_a_postgres_session_reads_standard_conforming_strings_on(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with pg_connect(postgres_test_db) as conn:
            database = postgres_test_db["database"]
            conn.execute(
                cast(
                    LiteralString,
                    f'ALTER DATABASE "{database}" SET standard_conforming_strings = off',
                ),
            )

        adapter = PostgresAdapter(postgres_test_db, statement_timeout=30)
        sibling = adapter.new_session()

        try:
            for session in (adapter, sibling):
                session.connect()
                assert session.execute_query("SHOW standard_conforming_strings") == [("on",)]
        finally:
            adapter.close()
            sibling.close()

    def test_a_postgres_session_whose_pin_did_not_hold_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _Off:
            closed = False

            def execute(self, *_args: Any) -> _Off:
                return self

            def fetchone(self) -> tuple[str]:
                return ("off",)

            def close(self) -> None:
                self.closed = True

        class _Psycopg:
            Error = RuntimeError

            @staticmethod
            def connect(**_kwargs: Any) -> _Off:
                return _Off()

        monkeypatch.setattr(postgres_connection, "_import_psycopg", lambda: _Psycopg)
        monkeypatch.setattr(postgres_connection, "ensure_pg_dump_available", lambda: None)
        session = postgres_connection.Connection(
            postgres_connection.ConnectionParams.from_credentials(_ADAPTERS["postgres"][1]),
        )

        with pytest.raises(
            postgres_connection.PostgresConnectionError,
            match="standard_conforming",
        ):
            session.open()

        assert not session.is_open()

    def test_a_mysql_session_clears_no_backslash_escapes_and_keeps_every_other_mode(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        params = mysql_connection.ConnectionParams.from_credentials(mysql_test_db)

        def escaping_off(_params: Any) -> Any:
            connection = mysql.connector.connect(
                host=params.host,
                port=params.port,
                user=params.user,
                password=params.password,
                database=params.database,
                autocommit=True,
            )
            cursor = connection.cursor()
            cursor.execute(
                "SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES,ANSI_QUOTES'",
            )
            cursor.close()

            return connection

        session = mysql_connection.Connection(params, cursor_factory=escaping_off)
        session.open()

        try:
            session.cursor.execute("SELECT @@SESSION.sql_mode")
            modes = set(session.cursor.fetchone()[0].split(","))
        finally:
            session.close()

        assert modes == {"STRICT_TRANS_TABLES", "ANSI_QUOTES"}

    def test_a_mysql_session_that_cannot_clear_the_mode_is_refused(self) -> None:
        refused = mysql.connector.Error

        class _Cursor:
            def execute(self, sql: str) -> None:
                if "sql_mode" in sql:
                    raise refused("SET is not allowed here")

            def close(self) -> None:
                return None

        class _Connection:
            server_info = "11.8.0-MariaDB"

            def cursor(self, **_kwargs: Any) -> _Cursor:
                return _Cursor()

            def close(self) -> None:
                return None

        params = mysql_connection.ConnectionParams.from_credentials(_ADAPTERS["mysql"][1])
        session = mysql_connection.Connection(params, cursor_factory=lambda _p: _Connection())

        with pytest.raises(mysql_connection.MysqlConnectionError, match="sql_mode"):
            session.open()
