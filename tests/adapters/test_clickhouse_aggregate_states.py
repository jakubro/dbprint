"""A SimpleAggregateFunction column is profiled as the type it stores; an AggregateFunction is not."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.adapters import ClickhouseAdapter
from dbprint.config.project import ConnectionConfig, DiffConfig, StatisticsConfig
from dbprint.conformance import validate_print
from dbprint.engine import Engine


@pytest.fixture
def rollup(clickhouse_native_connection: Any, tmp_path: Path) -> dict[str, Any]:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.reading_rollup ("
        "sensor_id UInt64, "
        "total SimpleAggregateFunction(sum, UInt64), "
        "last_seen SimpleAggregateFunction(max, DateTime64(3, 'UTC')), "
        "label SimpleAggregateFunction(anyLast, Nullable(String)), "
        "rank SimpleAggregateFunction(anyLast, LowCardinality(String)), "
        "tags SimpleAggregateFunction(groupArrayArray, Array(String)), "
        "distinct_state AggregateFunction(uniq, UInt64)"
        ") ENGINE = AggregatingMergeTree ORDER BY sensor_id",
    )
    cursor.execute(
        "INSERT INTO seedbank.reading_rollup SELECT number, number * 7, "
        "toDateTime64('2026-01-01 00:00:00', 3, 'UTC') + number, "
        "if(number % 5 = 0, NULL, concat('sensor-', toString(number))), "
        "['north', 'south', 'east', 'west'][1 + number % 4], ['a'], "
        "uniqState(number) FROM numbers(120) GROUP BY number",
    )
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _p: cursor,
    )
    adapter.connect()
    conn_config = ConnectionConfig(
        name="primary",
        adapter="clickhouse",
        auto=True,
        output=tmp_path,
        include=("*.reading_rollup",),
        exclude=(),
        max_age_days=7,
        statistics=StatisticsConfig(),
        diff=DiffConfig(),
    )

    try:
        Engine(adapter, conn_config, tmp_path).generate()
    finally:
        adapter.close()

    errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]
    (written,) = (tmp_path / "primary").rglob("statistics.yaml")

    assert errors == [], errors

    return yaml.safe_load(written.read_text())["columns"]


def test_a_pre_aggregated_sum_is_numeric(rollup: dict[str, Any]) -> None:
    total = rollup["total"]

    assert total["sql_type"] == "SimpleAggregateFunction(sum, UInt64)"
    assert total["classification"] == "numeric"
    assert total["range"] == {"min": 0, "max": 833}
    assert total["sum"] == 49980


def test_a_latest_instant_is_temporal_with_freshness(rollup: dict[str, Any]) -> None:
    last_seen = rollup["last_seen"]

    assert last_seen["classification"] == "temporal"
    assert "freshness" in last_seen


def test_a_nullable_last_value_is_published_nullable(rollup: dict[str, Any]) -> None:
    label = rollup["label"]

    assert label["nullable"] is True
    assert label["null_count"] == 24
    assert label["classification"] == "text"


def test_a_low_cardinality_last_value_lists_its_values(rollup: dict[str, Any]) -> None:
    rank = rollup["rank"]

    assert rank["classification"] == "categorical"
    assert sorted(v["value"] for v in rank["values"]) == ["east", "north", "south", "west"]


def test_an_array_inner_type_follows_array_and_an_aggregate_state_stays_unsupported(
    rollup: dict[str, Any],
) -> None:
    assert rollup["tags"]["classification"] == "composite"
    assert rollup["tags"]["parts"]["[*]"]["values"] == [{"value": "a", "count": 120}]
    assert rollup["distinct_state"]["classification"] == "unsupported"
    assert rollup["distinct_state"]["nullable"] is False
