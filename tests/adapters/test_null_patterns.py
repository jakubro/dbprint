"""The null census each adapter measures, against its own engine (SPEC 2.2.10).

Phase A counts nulls one column at a time and the census counts them by combination, in
different statements over the same rows: summing the combinations that name a column has to
reproduce that column's own count, or the two came from different reads. duckdb accepting the
operator form of the group key is evidence of dialect shape only, never of live Snowflake.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from dbprint.adapters import Adapter, StatisticsConfig, TableScope


CONFIG = StatisticsConfig()

# A narrowing as far as the producer is concerned, but big enough to hold the fixture's nulls.
WHOLE = TableScope(sample=1.0)

# The sampling construct each vendor draws with; a census over a copied draw carries none.
DRAW_CLAUSES: dict[str, str] = {
    "postgres": "tablesample",
    "mysql": "rand(",
    "snowflake": "sample system (",
    "duckdb": "tablesample bernoulli(",
    "clickhouse": "sample ",
    "redshift": "random() <",
    "databricks": "tablesample (",
    "bigquery": "tablesample system (",
}


def _census(adapter: Adapter, table_glob: str):
    """Phase A's per-column counts and the census measured beside them."""

    table = next(t for t in adapter.list_tables(include=[table_glob], exclude=[]))
    columns = adapter.introspect_columns(table.fqn)
    counts, base = adapter.compute_base_statistics(table.fqn, columns, CONFIG)
    census = adapter.compute_null_patterns(table.fqn, columns, CONFIG, counts, base)

    return counts, base, census


class TestTheCensusReconciles:
    def test_each_column_null_total_matches_phase_a(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        """The one arithmetic identity crossing a table-level object to a column."""

        _, factory = sql_adapter_factory
        adapter = factory()

        try:
            counts, base, census = _census(adapter, "*.curator")
        finally:
            adapter.close()

        assert census is not None, "the fixture carries nulls, so a census is owed"
        assert census.coverage == 1.0, "a full scan of a small table lists every combination"

        implied: dict[str, int] = {}

        for pattern in census.patterns:
            for name in pattern.columns:
                implied[name] = implied.get(name, 0) + pattern.count

        for name, stats in base.items():
            assert implied.get(name, 0) == stats.null_count, (
                f"{name}: the combinations account for {implied.get(name, 0)} null rows, "
                f"Phase A counted {stats.null_count}"
            )

        assert sum(p.count for p in census.patterns) == counts.rows_scanned

    def test_entries_are_ordered_and_disjoint(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()

        try:
            _, _, census = _census(adapter, "*.curator")
        finally:
            adapter.close()

        assert census is not None
        keys = [(-p.count, p.columns) for p in census.patterns]

        assert keys == sorted(keys)
        assert len({p.columns for p in census.patterns}) == len(census.patterns)


class TestTheCensusIsSkippedWhenItWouldSayNothing:
    def test_a_table_without_nulls_measures_nothing(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        """SPEC 2.2.10 reads an absent block as "no nulls", so the scan is not issued."""

        from tests.adapters.test_dialect_guard import _install_recorder

        vendor, factory = sql_adapter_factory
        adapter = factory()

        try:
            table = next(t for t in adapter.list_tables(include=["*.herbarium"], exclude=[]))
            columns = adapter.introspect_columns(table.fqn)
            counts, base = adapter.compute_base_statistics(table.fqn, columns, CONFIG)

            assert not any(s.null_count for s in base.values()), (
                f"{vendor}: the fixture's herbarium table grew a null; pick another table"
            )

            recorder = _install_recorder(adapter)
            census = adapter.compute_null_patterns(table.fqn, columns, CONFIG, counts, base)
            issued = list(recorder.flattened())
        finally:
            adapter.close()

        assert census is None
        assert issued == [], f"{vendor}: a census was skipped but statements were still sent"


class TestAWidthBeyondTheFunctionArgumentLimit:
    """A width past PostgreSQL's 100-argument ceiling, where the group key must still compile.

    PostgreSQL rejects a function call past 100 arguments, so only the operator form is a
    single statement there, while MySQL must keep the function because it reads `||` as OR.
    """

    WIDTH = 150

    def test_the_group_key_compiles_and_still_costs_one_statement(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        from tests.adapters.test_dialect_guard import _install_recorder

        vendor, factory = sql_adapter_factory
        adapter = factory()
        names = [f"c{i:03d}" for i in range(self.WIDTH)]

        try:
            seed = next(t for t in adapter.list_tables(include=["*.curator"], exclude=[]))
            namespace = seed.fqn.rsplit(".", 1)[0]
            _execute(adapter, _wide_ddl(vendor, namespace, names))
            _execute(adapter, _wide_row(vendor, namespace, populated=True, width=self.WIDTH))
            _execute(adapter, _wide_row(vendor, namespace, populated=False, width=self.WIDTH))

            table = next(t for t in adapter.list_tables(include=["*.wide"], exclude=[]))
            columns = adapter.introspect_columns(table.fqn)
            counts, base = adapter.compute_base_statistics(table.fqn, columns, CONFIG)
            recorder = _install_recorder(adapter)
            census = adapter.compute_null_patterns(table.fqn, columns, CONFIG, counts, base)
            issued = list(recorder.flattened())
        finally:
            adapter.close()

        assert len(columns) == self.WIDTH, f"{vendor}: the fixture is not {self.WIDTH} columns wide"
        assert len(issued) == 1, f"{vendor}: {self.WIDTH} columns took {len(issued)} statements"
        assert census is not None
        assert {p.columns for p in census.patterns} == {(), tuple(names)}


class TestOneStatementPerTable:
    def test_the_census_costs_a_single_grouped_scan(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        """The cost argument the measurement rests on, asserted rather than assumed."""

        from tests.adapters.test_dialect_guard import _install_recorder

        vendor, factory = sql_adapter_factory
        adapter = factory()

        try:
            table = next(t for t in adapter.list_tables(include=["*.curator"], exclude=[]))
            columns = adapter.introspect_columns(table.fqn)
            counts, base = adapter.compute_base_statistics(table.fqn, columns, CONFIG)
            recorder = _install_recorder(adapter)
            adapter.compute_null_patterns(table.fqn, columns, CONFIG, counts, base)
            issued = list(recorder.flattened())
        finally:
            adapter.close()

        assert len(issued) == 1, f"{vendor}: the census took {len(issued)} statements: {issued}"
        assert "group by" in issued[0]

    def test_a_sampled_table_counts_combinations_over_the_copied_draw(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        """The census must read the materialized copy, or its combinations describe a different
        population than the `null_count` they check. Databricks has no local temp table (measured).
        """

        from tests.adapters.test_dialect_guard import _install_recorder

        vendor, factory = sql_adapter_factory

        if vendor == "databricks":
            pytest.skip("Databricks has no local `CREATE TEMPORARY TABLE`; see test docstring.")

        adapter = factory()

        try:
            table = next(t for t in adapter.list_tables(include=["*.curator"], exclude=[]))
            columns = adapter.introspect_columns(table.fqn)
            scope = adapter.materialize_scope(table.fqn, WHOLE)

            assert scope.materialized is not None, f"{vendor}: the adapter declined to copy"

            counts, base = adapter.compute_base_statistics(table.fqn, columns, CONFIG, scope)
            recorder = _install_recorder(adapter)
            census = adapter.compute_null_patterns(table.fqn, columns, CONFIG, counts, base, scope)
            issued = list(recorder.flattened())
            adapter.release_scope(table.fqn, scope)
        finally:
            adapter.close()

        assert census is not None
        assert len(issued) == 1, f"{vendor}: the census took {len(issued)} statements"
        assert DRAW_CLAUSES[vendor] not in issued[0], (
            f"{vendor}: the census drew its own sample instead of reading the copy: {issued[0]}"
        )
        assert sum(p.count for p in census.patterns) == counts.rows_scanned

        implied: dict[str, int] = {}

        for pattern in census.patterns:
            for name in pattern.columns:
                implied[name] = implied.get(name, 0) + pattern.count

        for name, stats in base.items():
            assert implied.get(name, 0) == stats.null_count, (
                f"{name}: the combinations and the per-column count describe different rows"
            )

    def test_two_runs_agree(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()

        try:
            first = _census(adapter, "*.curator")[2]
            second = _census(adapter, "*.curator")[2]
        finally:
            adapter.close()

        assert first == second


def _quote(vendor: str, name: str) -> str:
    return (
        f"`{name}`" if vendor in ("mysql", "clickhouse", "databricks", "bigquery") else f'"{name}"'
    )


def _wide_ddl(vendor: str, namespace: str, names: list[str]) -> str:
    qualified = ".".join(_quote(vendor, part) for part in [*namespace.split("."), "wide"])

    if vendor == "clickhouse":
        # ClickHouse has no default engine and no implicit nullability - both are load-bearing
        # here: the width test needs an all-NULL row, which a bare `Int32` would refuse or coerce.
        columns = ", ".join(f"{_quote(vendor, name)} Nullable(Int32)" for name in names)

        return f"CREATE TABLE {qualified} ({columns}) ENGINE = Memory"

    columns = ", ".join(f"{_quote(vendor, name)} INTEGER" for name in names)

    if vendor == "databricks":
        return f"CREATE TABLE {qualified} ({columns}) USING DELTA"

    return f"CREATE TABLE {qualified} ({columns})"


def _wide_row(vendor: str, namespace: str, *, populated: bool, width: int) -> str:
    qualified = ".".join(_quote(vendor, part) for part in [*namespace.split("."), "wide"])
    values = ", ".join(["1" if populated else "NULL"] * width)

    return f"INSERT INTO {qualified} VALUES ({values})"


def _execute(adapter: Any, sql: str) -> None:
    """Run DDL on the adapter's own session, bypassing the read-only query seam.

    `execute_query` opens a read-only transaction on PostgreSQL, so it cannot build a fixture.
    The cursor comes first because MySQL keeps a connection object that cannot execute.
    """

    connection = adapter._connection

    for attribute in ("_cursor", "_conn"):
        target = getattr(connection, attribute, None)

        if target is not None and hasattr(target, "execute"):
            target.execute(sql)

            return

    raise AssertionError("no cursor or connection to run fixture DDL through")
