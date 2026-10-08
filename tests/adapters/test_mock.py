"""MockAdapter-specific tests - behavior the generic contract suite does not cover."""

from __future__ import annotations

import re

import pytest

from dbprint.adapters import (
    ColumnStats,
    MockAdapter,
    MockTable,
    TableCounts,
)
from dbprint.adapters.base import PhaseB
from dbprint.config import StatisticsConfig
from tests._prints import mock_table


def _empty_table() -> MockTable:
    return mock_table("schema.t", (), {}, ddl="CREATE TABLE schema.t (id int);\n")


class TestLifecycle:
    def test_requires_connect_before_use(self) -> None:
        adapter = MockAdapter({})

        with pytest.raises(RuntimeError, match="connect"):
            adapter.list_tables(include=["*"], exclude=[])

    def test_close_idempotent(self) -> None:
        adapter = MockAdapter({})
        adapter.connect()
        adapter.close()
        adapter.close()  # second call must not raise

    def test_close_invalidates_subsequent_use(self) -> None:
        adapter = MockAdapter({})
        adapter.connect()
        adapter.close()

        with pytest.raises(RuntimeError, match="connect"):
            adapter.list_tables(include=["*"], exclude=[])


class TestFixtureRoundTrip:
    def test_empty_fixture(self) -> None:
        adapter = MockAdapter({})
        adapter.connect()
        assert adapter.list_tables(include=["*"], exclude=[]) == []

    def test_single_table_fixture(self) -> None:
        adapter = MockAdapter({"schema.t": _empty_table()})
        adapter.connect()
        tables = adapter.list_tables(include=["*"], exclude=[])
        assert len(tables) == 1
        assert tables[0].fqn == "schema.t"

    def test_unknown_fqn_raises_keyerror(self) -> None:
        adapter = MockAdapter({"schema.t": _empty_table()})
        adapter.connect()

        with pytest.raises(KeyError, match=re.escape("schema.missing")):
            adapter.extract_ddl("schema.missing")


class TestDeterminism:
    def test_sample_values_respects_n(self) -> None:
        tbl = mock_table(
            "schema.t",
            (),
            {},
            ddl="CREATE TABLE schema.t (id int);\n",
            samples={"id": list(range(100))},
        )
        adapter = MockAdapter({"schema.t": tbl})
        adapter.connect()
        assert adapter.sample_values("schema.t", "id", n=5) == [0, 1, 2, 3, 4]
        assert len(adapter.sample_values("schema.t", "id", n=200)) == 100

    def test_sample_values_missing_column_returns_empty(self) -> None:
        adapter = MockAdapter({"schema.t": _empty_table()})
        adapter.connect()
        assert adapter.sample_values("schema.t", "no_such_col", n=10) == []


class TestStatisticsContract:
    def test_compute_statistics_ignores_columns_and_config(self) -> None:
        """Mock returns the fixture verbatim regardless of `columns` / `config`."""

        adapter = MockAdapter({"schema.t": _empty_table()})
        adapter.connect()
        # Canned stats describe every row, so an unstated fixture reads as an exact full scan.
        assert adapter.compute_statistics("schema.t", [], StatisticsConfig(), frozenset()) == (
            TableCounts(row_count=0, rows_scanned=0, row_count_method="exact"),
            PhaseB({}),
        )


def _stats_table(
    cardinality: int,
    normalized_cardinalities: dict[str, int] | None = None,
) -> MockTable:
    return mock_table(
        "schema.t",
        (),
        {
            "s": ColumnStats(
                sql_type="text",
                nullable=True,
                null_count=0,
                null_rate=0.0,
                cardinality=cardinality,
                cardinality_ratio=0.5,
                cardinality_method="exact",
            ),
        },
        ddl="CREATE TABLE schema.t (s text);\n",
        normalized_cardinalities=normalized_cardinalities or {},
    )


class TestViewDependencies:
    """`introspect_view_dependencies`: connection-scoped, stated on the adapter itself - not
    per-MockTable, the same way `default_collation` never rides the fixture dict either.
    """

    def test_defaults_to_none(self) -> None:
        """Unstated means "not asked", the same default a real adapter's own failure produces."""

        adapter = MockAdapter({"schema.v": _empty_table()})
        adapter.connect()
        assert adapter.introspect_view_dependencies() is None


class TestNormalizedCardinality:
    """`compute_normalized_cardinality`: stated, never derived - the fixture's own merge
    count, defaulting to `cardinality` (no case/whitespace merges) when unstated.
    """

    def test_defaults_to_cardinality_when_unstated(self) -> None:
        adapter = MockAdapter({"schema.t": _stats_table(cardinality=5)})
        adapter.connect()

        assert adapter.compute_normalized_cardinality("schema.t", "s", "text") == 5

    def test_uses_the_stated_merge_count_when_present(self) -> None:
        adapter = MockAdapter(
            {"schema.t": _stats_table(cardinality=5, normalized_cardinalities={"s": 3})},
        )
        adapter.connect()

        assert adapter.compute_normalized_cardinality("schema.t", "s", "text") == 3

    def test_missing_column_falls_back_to_zero(self) -> None:
        adapter = MockAdapter({"schema.t": _empty_table()})
        adapter.connect()

        assert adapter.compute_normalized_cardinality("schema.t", "no_such_col", "text") == 0
