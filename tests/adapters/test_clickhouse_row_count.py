"""A ClickHouse table's size and narrowed `row_count` count the rows a lightweight DELETE left."""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.adapters import ClickhouseAdapter, StatisticsConfig, TableScope
from tests.adapters._clickhouse import Recorder


@pytest.fixture
def thinned(clickhouse_native_connection: Any) -> Any:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.field_log (id UInt64) ENGINE = MergeTree "
        "ORDER BY sipHash64(id) SAMPLE BY sipHash64(id)",
    )
    cursor.execute("INSERT INTO seedbank.field_log SELECT number FROM numbers(100000)")
    cursor.execute("DELETE FROM seedbank.field_log WHERE id >= 1000")
    cursor.execute("CREATE TABLE seedbank.seed_lot (id UInt64) ENGINE = TinyLog")
    cursor.execute("INSERT INTO seedbank.seed_lot SELECT number FROM numbers(10)")

    return cursor


def _adapter(cursor: Any) -> ClickhouseAdapter:
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    adapter.connect()
    adapter.list_tables(include=["*"], exclude=[])

    return adapter


def test_the_size_is_the_live_count(thinned: Any) -> None:
    assert _adapter(thinned).estimate_row_count("seedbank.field_log") == 1000


def test_a_narrowed_read_publishes_the_live_count_as_exact(thinned: Any) -> None:
    adapter = _adapter(thinned)
    counts, _ = adapter.compute_base_statistics(
        "seedbank.field_log",
        adapter.introspect_columns("seedbank.field_log"),
        StatisticsConfig(),
        TableScope(filter="id < 500"),
    )

    assert (counts.row_count, counts.row_count_method) == (1000, "exact")


def test_an_engine_with_no_cheap_count_has_no_size_and_is_never_counted(thinned: Any) -> None:
    recorder = Recorder(thinned)

    assert _adapter(recorder).estimate_row_count("seedbank.seed_lot") is None
    assert not [s for s in recorder.statements if "count()" in s]
