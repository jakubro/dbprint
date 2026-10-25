"""A `%%` in a name, a key, a filter or an assertion reaches BigQuery and ClickHouse as written."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any, cast

import pytest
from clickhouse_connect.dbapi.cursor import Cursor as ClickhouseCursor
from google.cloud.bigquery.dbapi.cursor import Cursor as BigqueryCursor

from dbprint.adapters import BigqueryAdapter, ClickhouseAdapter, StatisticsConfig
from dbprint.adapters.base import TableScope
from dbprint.adapters.bigquery import connection as bigquery_connection
from dbprint.adapters.clickhouse import connection as clickhouse_connection
from tests.adapters._composites import generate, parts_of


_AS_WRITTEN = [
    "SELECT `germinated%%` FROM t",
    "SELECT 1 WHERE tag <> '50%%'",
    "SELECT FORMAT('%d%%', 5)",
    "SELECT '%Y-%m-%d'",
    "SELECT `rate%` FROM t",
]


class TestTheDriverReceivesTheStatementAsWritten:
    """The real DB-API cursors, with only the network client stood in."""

    @pytest.mark.parametrize("sql", _AS_WRITTEN)
    def test_bigquery(self, sql: str) -> None:
        client = _Recording(
            types.SimpleNamespace(schema=[], total_rows=0, num_dml_affected_rows=None),
        )
        cursor: Any = BigqueryCursor(types.SimpleNamespace(_client=client))

        bigquery_connection.exec_query(cursor, sql)
        bigquery_connection.exec_query(cursor, sql, ())

        assert client.sent == [sql, sql]

    @pytest.mark.parametrize("sql", _AS_WRITTEN)
    def test_clickhouse(self, sql: str) -> None:
        client = _Recording(
            types.SimpleNamespace(
                result_set=[(1,)],
                column_names=["c"],
                column_types=[],
                summary={},
            ),
        )
        cursor: Any = ClickhouseCursor(cast(Any, client))

        clickhouse_connection.exec_query(cursor, sql)
        clickhouse_connection.exec_query(cursor, sql, ())

        assert client.sent == [sql, sql]


class TestPercentThroughTheSubstrate:
    """Each substrate cursor rewrites as its driver does, so seed SQL spells a `%%` as `%%%%`."""

    def test_clickhouse(self, clickhouse_native_connection: Any, tmp_path: Path) -> None:
        cursor = clickhouse_native_connection
        cursor.execute(
            "CREATE TABLE seedbank.germination_trial "
            "(trial_no UInt32, `germinated%%%%` String, reading JSON) ENGINE = Memory",
        )
        cursor.execute(
            "INSERT INTO seedbank.germination_trial SELECT number, ['low', 'mid', 'high'][number % 3 + 1], "
            "concat('{\"moisture%%%%\": \"', ['dry', 'damp', 'wet'][number % 3 + 1], '\"}') "
            "FROM numbers(60)",
        )
        cursor.execute(
            "CREATE TABLE seedbank.steward (steward_no UInt32, surname String) ENGINE = Memory",
        )
        cursor.execute(
            "INSERT INTO seedbank.steward SELECT number, ['Linden', 'Larch', '50%%'][number % 3 + 1] "
            "FROM numbers(60)",
        )

        def adapter() -> ClickhouseAdapter:
            return _connected(
                ClickhouseAdapter(
                    {"host": "chdb", "database": "seedbank"},
                    cursor_factory=lambda _params: cursor,
                ),
            )

        _assert_the_filter_and_assertion_read_as_written(adapter())
        _assert_the_name_and_key_read_as_written(
            generate(adapter(), "clickhouse", tmp_path, "*.germination_trial"),
        )

    def test_bigquery(self, bigquery_test_dataset: Any) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(
            f"CREATE TABLE `{dataset}`.germination_trial "
            "(trial_no INT64, `germinated%%%%` STRING, reading JSON)",
        )
        cursor.execute(
            f"INSERT INTO `{dataset}`.germination_trial (trial_no, `germinated%%%%`, reading) "
            "SELECT n, ['low', 'mid', 'high'][OFFSET(MOD(n, 3))], "
            "PARSE_JSON(CONCAT('{\"moisture%%%%\": \"', ['dry', 'damp', 'wet'][OFFSET(MOD(n, 3))], '\"}')) "
            "FROM UNNEST(GENERATE_ARRAY(0, 59)) n",
        )
        cursor.execute(f"CREATE TABLE `{dataset}`.steward (steward_no INT64, surname STRING)")
        cursor.execute(
            f"INSERT INTO `{dataset}`.steward (steward_no, surname) "
            "SELECT n, ['Linden', 'Larch', '50%%'][OFFSET(MOD(n, 3))] FROM UNNEST(GENERATE_ARRAY(0, 59)) n",
        )

        def adapter() -> BigqueryAdapter:
            return _connected(
                BigqueryAdapter(
                    {"project": "dbprint-test", "dataset": dataset},
                    cursor_factory=lambda _params: cursor,
                ),
            )

        _assert_the_filter_and_assertion_read_as_written(adapter())
        trial = adapter()
        fqn = next(
            t.fqn
            for t in trial.list_tables(include=["*"], exclude=[])
            if t.fqn.endswith(".germination_trial")
        )
        _, stats = trial.compute_statistics(
            fqn,
            trial.introspect_columns(fqn),
            StatisticsConfig(),
            frozenset(),
        )
        moisture = {p.path: p for p in parts_of(trial, "germination_trial", "reading").parts}

        # The emulator extracts no DDL, so `generate` writes nothing; the adapter is read directly.
        assert {v.value for v in stats.stats["germinated%%"].values or ()} == {"low", "mid", "high"}
        assert stats.stats["germinated%%"].unmeasured is None
        assert {v.value for v in moisture['["moisture%%"]'].stats.values or ()} == {
            "dry",
            "damp",
            "wet",
        }


def _connected[A: Any](adapter: A) -> A:
    adapter.connect()

    return adapter


class _Recording:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.sent: list[str] = []

    def query_and_wait(self, query: str, **_: Any) -> Any:
        self.sent.append(query)

        return self.result

    def query(self, query: str, *_: Any, **__: Any) -> Any:
        self.sent.append(query)

        return self.result


def _assert_the_name_and_key_read_as_written(trial: dict[str, Any]) -> None:
    germinated = trial["germinated%%"]

    assert {v["value"] for v in germinated["values"]} == {"low", "mid", "high"}
    assert "unmeasured" not in germinated
    assert {v["value"] for v in trial["reading"]["parts"]['["moisture%%"]']["values"]} == {
        "dry",
        "damp",
        "wet",
    }


def _assert_the_filter_and_assertion_read_as_written(adapter: Any) -> None:
    fqn = next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if t.fqn.endswith(".steward")
    )
    _, stats = adapter.compute_statistics(
        fqn,
        adapter.introspect_columns(fqn),
        StatisticsConfig(),
        frozenset(),
        scope=TableScope(filter="surname <> '50%%'"),
    )

    assert {v.value for v in stats.stats["surname"].values or ()} == {"Linden", "Larch", "50%"}
    assert adapter.execute_query("SELECT '50%%' AS s") == [("50%%",)]
