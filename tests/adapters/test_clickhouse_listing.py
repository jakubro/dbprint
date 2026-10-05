"""ClickHouse lists only objects whose rows a read returns without consuming, losing or never ending."""

from __future__ import annotations

import contextlib
from typing import Any

from dbprint.adapters import ClickhouseAdapter
from dbprint.adapters.clickhouse.introspect import list_tables


class _Cursor:
    """Answers the listing read with no rows and records what it was bound with."""

    def __init__(self) -> None:
        self.bound: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.bound.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []

    def fetchone(self) -> Any:
        """Part of the `Cursor` protocol; the listing fetches all rows."""

        return None

    def close(self) -> None:
        """Part of the `Cursor` protocol; this fixture holds nothing to release."""


def test_every_engine_chdb_cannot_create_is_still_refused_by_name() -> None:
    cursor = _Cursor()
    list_tables(cursor, ("seedbank",), include=["*"], exclude=[])
    (_sql, params), *_ = cursor.bound

    assert {"RabbitMQ", "NATS", "FileLog", "S3Queue", "AzureQueue", "LiveView"} <= set(params)
    assert {"Kafka", "Null", "WindowView", "GenerateRandom", "RedisStreams"} <= set(params)


def _listed(cursor: Any) -> set[str]:
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _p: cursor,
    )
    adapter.connect()

    try:
        return {meta.fqn for meta in adapter.list_tables(["*"], [])}
    finally:
        adapter.close()


def test_stream_null_window_and_random_tables_are_left_out(
    clickhouse_native_connection: Any,
) -> None:
    cursor = clickhouse_native_connection

    for statement in (
        "CREATE TABLE seedbank.field_log (id UInt64) ENGINE = MergeTree ORDER BY id",
        "CREATE TABLE seedbank.field_round (id UInt64) ENGINE = Null",
        "CREATE TABLE seedbank.sample (id UInt64) ENGINE = MergeTree ORDER BY id",
        (
            "CREATE MATERIALIZED VIEW seedbank.field_round_mv TO seedbank.sample "
            "AS SELECT id FROM seedbank.field_round"
        ),
        "CREATE TABLE seedbank.wide (id UInt64) ENGINE = GenerateRandom",
        "CREATE TABLE seedbank.narrow (id UInt64) ENGINE = URL('http://127.0.0.1:1/x.csv', CSV)",
        "SET allow_experimental_analyzer = 0",
        "SET allow_experimental_window_view = 1",
        (
            "CREATE WINDOW VIEW seedbank.field_log_v ENGINE = Memory AS "
            "SELECT count(id) AS c, tumbleStart(w) AS ws FROM seedbank.field_log "
            "GROUP BY tumble(toDateTime(id), INTERVAL '10' SECOND) AS w"
        ),
    ):
        cursor.execute(statement)

    # chdb registers a Kafka table and then reports its consumer shutting down.
    with contextlib.suppress(Exception):
        cursor.execute(
            "CREATE TABLE seedbank.batch (id UInt64) ENGINE = Kafka SETTINGS "
            "kafka_broker_list = '127.0.0.1:1', kafka_topic_list = 't', "
            "kafka_group_name = 'g', kafka_format = 'JSONEachRow'",
        )

    listed = _listed(cursor)

    assert {"seedbank.field_log", "seedbank.sample", "seedbank.field_round_mv"} <= listed
    assert "seedbank.narrow" in listed
    assert not {"seedbank.field_round", "seedbank.wide", "seedbank.batch"} & listed
    assert "seedbank.field_log_v" not in listed
    assert not {fqn for fqn in listed if ".inner" in fqn}
