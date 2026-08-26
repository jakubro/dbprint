"""MockAdapter-specific tests - behavior the generic contract suite does not cover."""

from __future__ import annotations

import pytest

from dbprint.adapters import (
    CommentsMeta,
    MockAdapter,
    MockTable,
    TableCounts,
)
from dbprint.config import StatisticsConfig


def _empty_table() -> MockTable:
    return MockTable(
        type="table",
        namespace_path=("schema", "t"),
        ddl="CREATE TABLE schema.t (id int);\n",
        columns=[],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={},
        samples={},
    )


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

        with pytest.raises(KeyError, match="schema.missing"):
            adapter.extract_ddl("schema.missing")


class TestDeterminism:
    def test_repeated_calls_return_equal_data(self) -> None:
        adapter = MockAdapter({"schema.t": _empty_table()})
        adapter.connect()
        assert adapter.extract_ddl("schema.t") == adapter.extract_ddl("schema.t")
        assert adapter.introspect_columns("schema.t") == adapter.introspect_columns("schema.t")

    def test_sample_values_respects_n(self) -> None:
        tbl = MockTable(
            type="table",
            namespace_path=("schema", "t"),
            ddl="CREATE TABLE schema.t (id int);\n",
            columns=[],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={},
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
            {},
        )
