"""ClickHouse DDL is read with the server's secret masking on, whatever the session says."""

from __future__ import annotations

from typing import Any

from dbprint.adapters import ClickhouseAdapter
from dbprint.adapters.clickhouse import ddl as ddl_module
from tests.adapters._clickhouse import Recorder


def _ddl(cursor: Any) -> tuple[str, list[str]]:
    cursor.execute(
        "CREATE TABLE seedbank.narrow (id UInt64) ENGINE = MySQL('h:3306', 'db', 't', 'u', 'pw')",
    )
    recorder = Recorder(cursor)
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


def test_an_engine_without_arguments_is_left_as_is() -> None:
    raw = "CREATE TABLE seedbank.narrow (`id` UInt64) ENGINE = MergeTree ORDER BY id"

    assert ddl_module.normalize(raw) == raw + "\n"
