"""ClickHouse DDL is read with the server's secret masking on, and a URL engine's password masked."""

from __future__ import annotations

from typing import Any

from dbprint.adapters import ClickhouseAdapter
from dbprint.adapters.clickhouse import ddl as ddl_module


class _Recorder:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.statements: list[str] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        self.statements.append(" ".join(sql.split()))

        return self._cursor.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


def _ddl(cursor: Any) -> tuple[str, list[str]]:
    cursor.execute(
        "CREATE TABLE seedbank.narrow (id UInt64) ENGINE = MySQL('h:3306', 'db', 't', 'u', 'pw')",
    )
    recorder = _Recorder(cursor)
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _p: recorder,
    )
    adapter.connect()

    try:
        adapter.list_tables(["*.narrow"], [])
        ddl = adapter.extract_ddl("seedbank.narrow")
    finally:
        adapter.close()

    return ddl, [s for s in recorder.statements if "create_table_query" in s]


def test_a_default_session_reads_the_masked_ddl_as_is(clickhouse_native_connection: Any) -> None:
    ddl, reads = _ddl(clickhouse_native_connection)

    assert "'u', '[HIDDEN]')" in ddl
    assert "'pw'" not in ddl
    assert all("SETTINGS" not in read for read in reads)


def test_a_session_that_shows_secrets_reads_with_masking_forced_on(
    clickhouse_native_connection: Any,
) -> None:
    clickhouse_native_connection.execute("SET format_display_secrets_in_show_and_select = 1")

    ddl, reads = _ddl(clickhouse_native_connection)

    assert "'pw'" not in ddl
    assert reads
    assert all(
        read.endswith("SETTINGS format_display_secrets_in_show_and_select = 0") for read in reads
    )


def test_a_url_engine_password_is_masked_whatever_the_server_returned() -> None:
    raw = (
        "CREATE TABLE seedbank.narrow (`id` UInt64 DEFAULT 'https://a:b@c') "
        "ENGINE = URL('https://u:pw@h/x.csv', 'CSV')"
    )

    assert ddl_module.normalize(raw) == (
        "CREATE TABLE seedbank.narrow (`id` UInt64 DEFAULT 'https://a:b@c') "
        "ENGINE = URL('https://u:[HIDDEN]@h/x.csv', 'CSV')\n"
    )


def test_only_the_engine_arguments_are_masked() -> None:
    raw = (
        "CREATE TABLE seedbank.narrow (`id` UInt64) "
        "ENGINE = URL('https://u:pw@h/x(1).csv', 'CSV') "
        "COMMENT 'see https://a:b@c and Password=keep'"
    )

    assert ddl_module.normalize(raw) == (
        "CREATE TABLE seedbank.narrow (`id` UInt64) "
        "ENGINE = URL('https://u:[HIDDEN]@h/x(1).csv', 'CSV') "
        "COMMENT 'see https://a:b@c and Password=keep'\n"
    )


def test_an_engine_without_arguments_is_left_as_is() -> None:
    raw = "CREATE TABLE seedbank.narrow (`id` UInt64) ENGINE = MergeTree ORDER BY id"

    assert ddl_module.normalize(raw) == raw + "\n"


def test_an_escaped_quote_or_at_sign_in_an_engine_url_password_is_masked_whole() -> None:
    raw = r"CREATE TABLE seedbank.narrow (`id` UInt64) ENGINE = URL('https://u:a\'b@c@h/x.csv', 'CSV')"

    assert ddl_module.normalize(raw) == (
        "CREATE TABLE seedbank.narrow (`id` UInt64) ENGINE = URL('https://u:[HIDDEN]@h/x.csv', 'CSV')\n"
    )
