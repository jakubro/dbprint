"""What a failed temporal block costs a column, and that every adapter says so (SPEC 2.2.4).

The degrade is copied per adapter, so one adapter dropping the marker is what this catches. An
AST sweep reads that every handler names its loss; only running one reads what it named.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from dbprint.adapters import Adapter, PostgresAdapter, StatisticsConfig
from dbprint.adapters.base import temporal_block_unmeasured
from dbprint.cli.adapter_registry import ADAPTERS
from tests.adapters.test_dialect_guard import STATS_MODULES


_ADAPTERS_WITH_STATS = sorted(
    name
    for name in ADAPTERS
    if importlib.util.find_spec(f"dbprint.adapters.{name}.stats") is not None
)


def _stats_source(adapter: str) -> pathlib.Path:
    spec = importlib.util.find_spec(f"dbprint.adapters.{adapter}.stats")
    assert spec is not None and spec.origin is not None

    return pathlib.Path(spec.origin)


class TestTheSharedLossList:
    def test_a_timestamp_loses_the_whole_block_including_the_day_truncation(self) -> None:
        assert temporal_block_unmeasured("TIMESTAMP") == (
            "distribution",
            "frequencies",
            "freshness",
            "percentiles",
            "quantized_count",
            "range",
            "values",
        )

    def test_a_date_does_not_name_quantized_count(self) -> None:
        """It is its own day-truncation, so SPEC 2.2.3 never required it - and naming a field the
        matrix does not require is `stats.unmeasured-names-unrequired-field`.
        """

        assert "quantized_count" not in temporal_block_unmeasured("DATE")

    def test_a_time_does_not_name_it_either(self) -> None:
        assert "quantized_count" not in temporal_block_unmeasured("TIME")

    def test_the_names_are_sorted_and_unique(self) -> None:
        """SPEC 2.2.4 requires both of the emitted list, and the engine emits this one verbatim."""

        names = temporal_block_unmeasured("TIMESTAMP")

        assert list(names) == sorted(set(names))


@pytest.mark.parametrize("adapter", _ADAPTERS_WITH_STATS)
def test_no_degrade_returns_a_column_without_naming_what_it_lost(adapter: str) -> None:
    """Every `except` handler that hands back a rebuilt `ColumnStats` must set `unmeasured`.

    `return stats` untouched ships a column missing fields SPEC 2.2.3 marks REQUIRED.
    """

    tree = ast.parse(_stats_source(adapter).read_text(encoding="utf-8"))
    rebuilt = [
        (handler, last.value)
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        for last in [handler.body[-1]]
        if isinstance(last, ast.Return) and isinstance(last.value, ast.Call)
        if isinstance(last.value.func, ast.Name) and last.value.func.id.endswith("replace")
    ]

    assert rebuilt, f"{adapter} has no statistics degrade path at all"

    for handler, call in rebuilt:
        keywords = {kw.arg for kw in call.keywords}

        assert "unmeasured" in keywords, (
            f"{adapter}/stats.py:{handler.lineno} degrades without naming the loss"
        )


@pytest.mark.parametrize("adapter", _ADAPTERS_WITH_STATS)
def test_no_degrade_hands_back_the_untouched_stats_object(adapter: str) -> None:
    """A bare `return stats` is a silent loss: the column ships short with nothing saying so."""

    tree = ast.parse(_stats_source(adapter).read_text(encoding="utf-8"))
    bare = [
        handler.lineno
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        for last in [handler.body[-1]]
        if isinstance(last, ast.Return)
        if isinstance(last.value, ast.Name) and last.value.id == "stats"
    ]

    assert bare == [], f"{adapter}/stats.py degrades silently at line(s) {bare}"


# The statement each handler guards. `_approximate_distribution_via_top_n` wraps only the top-N,
# so the scalar bounds beside it survive there.
_GUARDED_HELPER: dict[str, str] = {
    "postgres": "_fetch_temporal_block",
    "mysql": "_fetch_temporal_block",
    "snowflake": "_fetch_temporal_block",
    "duckdb": "_fetch_temporal_block",
    "clickhouse": "_fetch_temporal_block",
    "databricks": "_fetch_temporal_block",
    "redshift": "_approximate_distribution_via_top_n",
    "bigquery": "_approximate_distribution_via_top_n",
}

# Hand-authored rather than taken from `temporal_block_unmeasured`: an adapter that recomputed
# its loss list through the same helper it degrades through could never disagree with it.
_WHOLE_BLOCK = (
    "distribution",
    "frequencies",
    "freshness",
    "percentiles",
    "quantized_count",
    "range",
    "values",
)
_TOP_N_ONLY = ("distribution", "frequencies", "values")
_DAY_ALIGNED = ("distribution", "frequencies", "freshness", "percentiles", "range", "values")

_EXPECTED: dict[str, tuple[str, ...]] = {
    vendor: _TOP_N_ONLY if vendor in ("redshift", "bigquery") else _WHOLE_BLOCK
    for vendor in _GUARDED_HELPER
}

# Two of the wide fixture's temporal columns: one made to fail, one left to measure normally.
_DEGRADED_COLUMN = "observed_at"
_SURVIVING_COLUMN = "within_day_at"


def _always_fails(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("Resources exceeded during query execution")


def _fail_for(column: str, original: Any) -> Any:
    """Fail only where `column` is the statement's subject, so its siblings still measure.

    Matched on the column record or rendered expression - an identity carrier's repr names them all.
    """

    def guarded(*args: Any, **kwargs: Any) -> Any:
        subjects = [getattr(arg, "name", None) for arg in args]
        expressions = [arg for arg in args if isinstance(arg, str)]

        if column in subjects or any(column in expr for expr in expressions):
            return _always_fails()

        return original(*args, **kwargs)

    return guarded


def _degrade(
    vendor: str,
    factory: Callable[[], Adapter],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Profile the contract fixture's wide table with one column's temporal read made to fail."""

    module = STATS_MODULES[vendor]
    helper = _GUARDED_HELPER[vendor]
    monkeypatch.setattr(module, helper, _fail_for(_DEGRADED_COLUMN, getattr(module, helper)))
    adapter = factory()

    try:
        table = next(
            t
            for t in adapter.list_tables(include=["*"], exclude=[])
            if t.fqn.endswith(".viability_check")
        )
        columns = adapter.introspect_columns(table.fqn)
        _counts, stats = adapter.compute_statistics(
            table.fqn,
            columns,
            StatisticsConfig(),
            frozenset(),
        )
    finally:
        adapter.close()

    return stats


class TestTheDegradeRunsAndNamesWhatItLost:
    """The sweep above reads the keyword; only executing the handler reads its value.

    Every case drives the adapter's own path against its substrate, so a hardcoded loss list fails.
    """

    def test_the_column_names_exactly_the_fields_its_read_cost(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vendor, factory = sql_adapter_factory
        stat = _degrade(vendor, factory, monkeypatch)[_DEGRADED_COLUMN]

        assert stat.unmeasured == _EXPECTED[vendor]
        # SPEC 2.2.4: a named field the column also emits is a contradiction, not a marker.
        # `freshness` is the engine's own derivation from `range`, so no adapter record holds it.
        carried = [name for name in stat.unmeasured if name != "freshness"]

        assert all(getattr(stat, name) is None for name in carried)

    def test_the_statements_that_did_answer_survive(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vendor, factory = sql_adapter_factory
        stat = _degrade(vendor, factory, monkeypatch)[_DEGRADED_COLUMN]

        assert stat.cardinality is not None
        assert stat.cardinality_method is not None

        if _EXPECTED[vendor] == _TOP_N_ONLY:
            assert stat.range is not None
            assert stat.percentiles

    def test_a_sibling_temporal_column_still_measures(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vendor, factory = sql_adapter_factory
        sibling = _degrade(vendor, factory, monkeypatch)[_SURVIVING_COLUMN]

        assert sibling.unmeasured is None
        assert sibling.range is not None


class TestADayAlignedTypeNamesNoDayTruncation:
    """`quantized_count` is not required on a type already truncated to a day (SPEC 2.2.3).

    Postgres alone: the six adapters losing the whole block derive the list from the column's type.
    """

    def test_a_date_columns_loss_list_drops_quantized_count(
        self,
        postgres_test_db: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_date_probe(postgres_test_db)
        monkeypatch.setattr(STATS_MODULES["postgres"], "_fetch_temporal_block", _always_fails)
        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()
        # The adapter binds the spelling `list_tables` observed, so enumeration comes first.
        adapter.list_tables(include=["*"], exclude=[])

        try:
            columns = adapter.introspect_columns("fixture.date_probe")
            _counts, stats = adapter.compute_statistics(
                "fixture.date_probe",
                columns,
                StatisticsConfig(),
                frozenset(),
            )
        finally:
            adapter.close()

        assert stats["a"].unmeasured == _DAY_ALIGNED


def _seed_date_probe(creds: dict[str, str]) -> None:
    """One DATE column holding more distinct values than the enumeration threshold.

    Below it the column pre-classifies `categorical` and never reaches the temporal branch.
    """

    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password=creds["password"],
        autocommit=True,
    ) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS fixture")
        conn.execute("CREATE TABLE fixture.date_probe (a date NOT NULL)")
        conn.execute(
            "INSERT INTO fixture.date_probe (a) "
            "SELECT DATE '2026-01-01' + i FROM generate_series(0, 59) AS i",
        )
