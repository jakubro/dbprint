"""Size rules and a narrowed `row_count` read the rows a table holds, not rows deleted from it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import chdb.dbapi
import duckdb
import yaml

from dbprint.adapters import ClickhouseAdapter, DuckdbAdapter
from dbprint.config import ConnectionConfig, ProfileConfig, RelationshipsProfile
from dbprint.config.project import AdapterName, RuleConfig, RuleMatch
from dbprint.engine import Engine
from tests.adapters._clickhouse import PyformatCursor


CEILING = RuleConfig(profile={"read": {"tables": {"mode": "sample", "max_rows": 20000}}})
LARGE_ONLY = RuleConfig(
    match=RuleMatch(min_rows=50000),
    profile={"read": {"tables": {"mode": "sample", "fraction": 0.5}}},
)

CLICKHOUSE_TABLES = [
    "CREATE TABLE seedbank.field_log (id UInt64) ENGINE = MergeTree ORDER BY id SAMPLE BY id",
    "INSERT INTO seedbank.field_log SELECT number FROM numbers(100000)",
    "DELETE FROM seedbank.field_log WHERE id >= 1000",
    "CREATE TABLE seedbank.field_round (id UInt64) ENGINE = MergeTree ORDER BY id SAMPLE BY id",
    "INSERT INTO seedbank.field_round SELECT number FROM numbers(200000)",
]


def _conn(tmp_path: Path, adapter: AdapterName, *rules: RuleConfig) -> ConnectionConfig:
    return ConnectionConfig(
        name="main",
        adapter=adapter,
        output=tmp_path / "prints",
        rules=rules,
        profile=ProfileConfig(relationships=RelationshipsProfile(infer=False)),
    )


def _statistics(tmp_path: Path, table: str) -> dict[str, Any]:
    path = next((tmp_path / "prints").rglob(f"{table}/statistics.yaml"))

    return yaml.safe_load(path.read_text())


def _duckdb(tmp_path: Path, *rules: RuleConfig) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE TABLE field_log AS SELECT i AS id FROM range(100000) r(i)")
    con.execute("DELETE FROM field_log WHERE id >= 1000")
    con.execute("CREATE TABLE field_round AS SELECT i AS id FROM range(200000) r(i)")
    con.execute("CHECKPOINT")
    con.close()
    adapter = DuckdbAdapter({"database": str(database)})
    Engine(adapter, _conn(tmp_path, "duckdb", *rules), tmp_path).generate()


class TestDuckdb:
    def test_a_mostly_deleted_table_under_a_ceiling_is_read_whole(self, tmp_path: Path) -> None:
        _duckdb(tmp_path, CEILING)
        thinned = _statistics(tmp_path, "field_log")

        assert "scope" not in thinned
        assert (thinned["row_count"], thinned["row_count_method"]) == (1000, "exact")

    def test_a_size_rule_does_not_govern_it(self, tmp_path: Path) -> None:
        _duckdb(tmp_path, LARGE_ONLY)

        assert "scope" not in _statistics(tmp_path, "field_log")

    def test_a_sampled_table_publishes_its_count_as_exact(self, tmp_path: Path) -> None:
        _duckdb(tmp_path, CEILING)
        sampled = _statistics(tmp_path, "field_round")

        assert sampled["scope"]["sample"] <= 0.1
        assert (sampled["row_count"], sampled["row_count_method"]) == (200000, "exact")


class TestClickhouse:
    def _generate(self, tmp_path: Path, statements: list[str], *rules: RuleConfig) -> None:
        connection = chdb.dbapi.connect()
        cursor = connection.cursor()
        cursor.execute("CREATE DATABASE seedbank")

        for statement in statements:
            cursor.execute(statement)

        adapter = ClickhouseAdapter(
            {"host": "chdb", "database": "seedbank"},
            cursor_factory=lambda _params: PyformatCursor(connection.cursor()),
        )

        try:
            Engine(adapter, _conn(tmp_path, "clickhouse", *rules), tmp_path).generate()
        finally:
            connection.close()

    def test_a_lightweight_delete_leaves_a_table_read_whole_under_a_ceiling(
        self,
        tmp_path: Path,
    ) -> None:
        self._generate(tmp_path, CLICKHOUSE_TABLES, CEILING)
        thinned = _statistics(tmp_path, "field_log")

        assert "scope" not in thinned
        assert (thinned["row_count"], thinned["row_count_method"]) == (1000, "exact")

    def test_a_size_rule_does_not_govern_it(self, tmp_path: Path) -> None:
        self._generate(tmp_path, CLICKHOUSE_TABLES, LARGE_ONLY)

        assert "scope" not in _statistics(tmp_path, "field_log")

    def test_a_sampled_table_publishes_its_count_as_exact(self, tmp_path: Path) -> None:
        self._generate(tmp_path, CLICKHOUSE_TABLES, CEILING)
        sampled = _statistics(tmp_path, "field_round")

        assert sampled["scope"]["sample"] <= 0.1
        assert (sampled["row_count"], sampled["row_count_method"]) == (200000, "exact")

    def test_rows_left_below_the_estimate_threshold_are_counted_exactly(
        self,
        tmp_path: Path,
    ) -> None:
        self._generate(
            tmp_path,
            [
                (
                    "CREATE TABLE seedbank.seed_lot (id UInt64, rank String) "
                    "ENGINE = MergeTree ORDER BY id"
                ),
                (
                    "INSERT INTO seedbank.seed_lot "
                    "SELECT number, concat('r', toString(number % 1000)) FROM numbers(1500000)"
                ),
                "DELETE FROM seedbank.seed_lot WHERE id >= 300000",
            ],
        )
        rank = _statistics(tmp_path, "seed_lot")["columns"]["rank"]

        assert (rank["cardinality"], rank["cardinality_method"]) == (1000, "exact")
