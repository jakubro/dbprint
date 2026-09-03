"""How cardinality is measured must not depend on which adapter is asking.

A catalog row count describes the table, not a scope-narrowed read of it, so approximating
against that figure once a rule narrows the read reports `approximate` for a slice small
enough to have been counted exactly.
"""

from __future__ import annotations

from unittest.mock import patch

import duckdb
import pytest

from dbprint.adapters import (
    Adapter,
    ColumnStats,
    SnowflakeAdapter,
    StatisticsConfig,
    TableScope,
)
from dbprint.adapters.snowflake import stats as snowflake_stats
from tests.adapters.conftest import SnowflakeDialectShim


CREDS = {
    "account": "a",
    "user": "u",
    "password": "p",
    "warehouse": "w",
    "database": "memory",
    "role": "r",
}
ROWS = 300


@pytest.fixture
def seeded() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("SET threads = 1")
    con.execute("CREATE SCHEMA seedbank")
    con.execute("CREATE TABLE seedbank.curation_event (id INTEGER, bucket INTEGER)")
    con.execute(f"INSERT INTO seedbank.curation_event SELECT i, i % 4 FROM range({ROWS}) t(i)")

    return con


def _profile(
    con: duckdb.DuckDBPyConnection,
    scope: TableScope | None,
    threshold: int,
) -> dict[str, ColumnStats]:
    adapter = SnowflakeAdapter(CREDS, cursor_factory=lambda _: SnowflakeDialectShim(con))
    adapter.connect()

    try:
        adapter.list_tables(include=["*"], exclude=[])
        columns = adapter.introspect_columns("memory.seedbank.curation_event")

        with patch.object(snowflake_stats, "APPROXIMATE_THRESHOLD", threshold):
            return adapter.compute_statistics(
                "memory.seedbank.curation_event",
                columns,
                StatisticsConfig(),
                frozenset(),
                None,
                scope,
            )[1]
    finally:
        adapter.close()


class TestNarrowedReadsAreCountedExactly:
    """Asserted on `bucket`, the low-cardinality column.

    `id` is unique, so the near-unique re-probe re-counts it exactly whichever path ran;
    `bucket`'s four values stay with whatever the approximation decided.
    """

    def test_a_narrowed_read_reports_exact(self, seeded: duckdb.DuckDBPyConnection) -> None:
        """The catalog figure describes the table, not the rows being measured."""

        stats = _profile(seeded, TableScope(filter='"bucket" = 0'), threshold=10)

        assert stats["bucket"].cardinality_method == "exact"

    def test_an_unnarrowed_large_table_still_approximates(
        self,
        seeded: duckdb.DuckDBPyConnection,
    ) -> None:
        """The control: without the narrowing the same table takes the estimate."""

        stats = _profile(seeded, None, threshold=10)

        assert stats["bucket"].cardinality_method == "approximate"

    def test_a_small_table_is_unchanged(self, seeded: duckdb.DuckDBPyConnection) -> None:
        stats = _profile(seeded, None, threshold=100_000)

        assert stats["bucket"].cardinality_method == "exact"

    def test_a_sampled_read_reports_exact_too(self, seeded: duckdb.DuckDBPyConnection) -> None:
        """A fraction narrows the read as much as a predicate does."""

        stats = _profile(seeded, TableScope(sample=0.5), threshold=10)

        assert stats["bucket"].cardinality_method == "exact"


class TestAdaptersAgreeOnHowTheyMeasured:
    """Two adapters profiling the same narrowed table must not answer differently - except
    BigQuery, whose `cardinality_method` is unconditionally `approximate`.
    """

    def test_a_narrowed_read_is_exact_on_every_adapter(
        self,
        all_sql_adapters: dict[str, Adapter],
    ) -> None:
        from dbprint.adapters.clickhouse import stats as clickhouse_stats
        from dbprint.adapters.postgres import stats as postgres_stats

        methods = {}

        for vendor, adapter in all_sql_adapters.items():
            table = next(t for t in adapter.list_tables(include=["*.viability_check"], exclude=[]))
            columns = adapter.introspect_columns(table.fqn)

            with (
                patch.object(snowflake_stats, "APPROXIMATE_THRESHOLD", 10),
                patch.object(postgres_stats, "APPROXIMATE_THRESHOLD", 10),
                patch.object(clickhouse_stats, "APPROXIMATE_THRESHOLD", 10),
            ):
                stats = adapter.compute_statistics(
                    table.fqn,
                    columns,
                    StatisticsConfig(),
                    frozenset(),
                    None,
                    TableScope(filter="score < 10"),
                )[1]

            methods[vendor] = stats["rank"].cardinality_method
            adapter.close()

        narrowed = {v: m for v, m in methods.items() if v != "bigquery"}
        assert set(narrowed.values()) == {"exact"}, f"adapters disagree: {narrowed}"
        assert methods.get("bigquery") == "approximate", methods


class TestMysqlNeverEstimates:
    """MySQL's `exact` is unconditional, which a narrowed read cannot show - every vendor agrees
    there by coincidence, so the distinction only appears unnarrowed above the threshold.
    """

    def test_mysql_stays_exact_where_the_others_switch_to_approximate(
        self,
        all_sql_adapters: dict[str, Adapter],
    ) -> None:
        from dbprint.adapters.clickhouse import stats as clickhouse_stats
        from dbprint.adapters.postgres import stats as postgres_stats

        methods = {}

        for vendor, adapter in all_sql_adapters.items():
            table = next(t for t in adapter.list_tables(include=["*.viability_check"], exclude=[]))
            columns = adapter.introspect_columns(table.fqn)

            with (
                patch.object(snowflake_stats, "APPROXIMATE_THRESHOLD", 10),
                patch.object(postgres_stats, "APPROXIMATE_THRESHOLD", 10),
                patch.object(clickhouse_stats, "APPROXIMATE_THRESHOLD", 10),
            ):
                stats = adapter.compute_statistics(
                    table.fqn,
                    columns,
                    StatisticsConfig(),
                    frozenset(),
                )[1]

            methods[vendor] = stats["rank"].cardinality_method
            adapter.close()

        assert methods["postgres"] == "approximate", methods
        assert methods["snowflake"] == "approximate", methods
        assert methods["clickhouse"] == "approximate", methods
        assert methods["mysql"] == "exact", methods


class TestTheReprobeCannotExceedTheTable:
    """A second statement sees a different read than the row count it lands beside."""

    def test_an_over_counting_reprobe_is_clamped(
        self,
        seeded: duckdb.DuckDBPyConnection,
    ) -> None:
        """Conformance rejects a cardinality above the non-null scanned count.

        The re-probe runs after phase A, so concurrent writes or an independent draw can hand
        it more distinct values than were counted.
        """

        original = snowflake_stats.exec_query
        seen: dict[str, int] = {}

        def inflate(cursor, sql, params=None):
            result = original(cursor, sql, params) if params else original(cursor, sql)

            if "COUNT(DISTINCT" in sql and "card_id" in sql:
                row = result.fetchone()
                seen["row_count"] = ROWS

                class _Inflated:
                    @staticmethod
                    def fetchone():
                        return (int(row[0]) + 10_000,)

                return _Inflated()

            return result

        with patch.object(snowflake_stats, "exec_query", inflate):
            stats = _profile(seeded, None, threshold=10)

        assert seen, "the re-probe never ran; the assertion would be vacuous"

        cardinality = stats["id"].cardinality

        assert cardinality is not None
        assert cardinality <= ROWS, (
            f"cardinality {cardinality} exceeds the {ROWS} rows the table holds, "
            "which conformance rejects as stats.cardinality-exceeds-row-count"
        )
