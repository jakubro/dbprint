"""A column whose type lacks an operator costs that column, never its table.

A declined column is only null-counted; one with a lossless comparable form is measured via it.
"""

from __future__ import annotations

import ast
import importlib
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, LiteralString, cast
from unittest.mock import patch

import duckdb
import psycopg
import pytest
import yaml

from dbprint.adapters import Adapter, ColumnMeta, DuckdbAdapter, MockAdapter, PostgresAdapter
from dbprint.adapters.base import BaseStats, ColumnStats, run_phase_a, run_phase_b
from dbprint.cli.adapter_registry import ADAPTERS
from dbprint.config import StatisticsConfig
from dbprint.config.project import ConnectionConfig
from dbprint.conformance import validate_print
from dbprint.engine import Engine, GenerateRequest
from tests.adapters.conftest import _adapter_factory_for
from tests.adapters.test_type_spellings import _DUCKDB_DECLARATIONS, _POSTGRES_SKIPPED
from tests.engine.test_orchestrator import _conn_config, _curator_fixture


_ADAPTERS_ROOT = Path(importlib.import_module("dbprint.adapters").__file__ or "").parent


def _column(name: str, sql_type: str = "integer") -> ColumnMeta:
    return ColumnMeta(name=name, sql_type=sql_type, nullable=True, default=None, ordinal=1)


class _Timeout(Exception):
    timed_out = True


class TestTheDrivers:
    def test_a_declined_column_reaches_only_the_null_count(self) -> None:
        seen: list[list[str]] = []

        def statement(batch: list[ColumnMeta]) -> tuple[int, dict[str, BaseStats]]:
            seen.append([c.name for c in batch])

            return 10, {
                c.name: BaseStats(null_count=0, cardinality=3, cardinality_method="exact")
                for c in batch
            }

        null_reads: list[list[str]] = []

        def null_counts(cols: list[ColumnMeta]) -> tuple[int, dict[str, int]]:
            null_reads.append([c.name for c in cols])

            return 10, {"spot": 4, "shape": 7}

        rows, phase_a = run_phase_a(
            [_column("id"), _column("spot", "point"), _column("shape", "point")],
            lambda _: 1,
            statement,
            null_counts,
            lambda batch: seen.append([c.name for c in batch]) or None,
            declines=lambda col: col.sql_type == "point",
        )

        assert rows == 10
        assert seen[0] == ["id"]
        assert all("spot" not in names and "shape" not in names for names in seen)
        assert null_reads == [["spot", "shape"]]
        assert phase_a.stats["spot"] == BaseStats(
            null_count=4,
            cardinality=0,
            cardinality_method="exact",
            supported=False,
        )
        assert phase_a.stats["shape"].null_count == 7

    def test_a_failing_column_is_named_and_its_siblings_are_measured(self) -> None:
        stats = ColumnStats(
            sql_type="integer",
            nullable=True,
            null_count=0,
            null_rate=0.0,
            cardinality=1,
            cardinality_ratio=1.0,
            cardinality_method="exact",
        )
        boom = RuntimeError("no equality operator")

        def measure(col: ColumnMeta) -> ColumnStats:
            if col.name == "doc":
                raise boom

            return stats

        result = run_phase_b([_column("id"), _column("doc", "json"), _column("n")], measure)

        assert list(result) == ["id", "n"]
        assert result.unmeasured == ("doc",)
        assert result.failures == (boom,)

    def test_a_timed_out_column_is_named_and_its_siblings_are_measured(self) -> None:
        stats = ColumnStats(
            sql_type="integer",
            nullable=True,
            null_count=0,
            null_rate=0.0,
            cardinality=1,
            cardinality_ratio=1.0,
            cardinality_method="exact",
        )
        timeout = _Timeout()

        def measure(col: ColumnMeta) -> ColumnStats:
            if col.name == "doc":
                raise timeout

            return stats

        result = run_phase_b([_column("id"), _column("doc"), _column("n")], measure)

        assert list(result) == ["id", "n"]
        assert (result.unmeasured, result.failures) == (("doc",), (timeout,))

    def test_a_failed_batch_falls_back_to_one_statement_per_column(self) -> None:
        measured: list[str] = []

        def batch(cols: list[ColumnMeta]) -> dict[str, ColumnStats]:
            raise RuntimeError("one column poisons the batch")

        def measure(col: ColumnMeta) -> ColumnStats:
            measured.append(col.name)

            if col.name == "doc":
                raise RuntimeError("no equality operator")

            return ColumnStats(
                sql_type="integer",
                nullable=True,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=1.0,
                cardinality_method="exact",
            )

        result = run_phase_b([_column("id"), _column("doc", "json")], measure, batch)

        assert measured == ["id", "doc"]
        assert result.unmeasured == ("doc",)


def _stats_source(vendor: str) -> ast.Module:
    return ast.parse((_ADAPTERS_ROOT / vendor / "stats.py").read_text(encoding="utf-8"))


def _calls(tree: ast.Module, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


@pytest.mark.parametrize("vendor", sorted(ADAPTERS))
def test_every_adapter_runs_both_phases_through_the_drivers(vendor: str) -> None:
    tree = _stats_source(vendor)
    phase_a = _calls(tree, "run_phase_a")

    assert phase_a, vendor
    assert all(any(k.arg == "declines" for k in call.keywords) for call in phase_a), vendor
    assert _calls(tree, "run_phase_b") or _calls(tree, "measure_columns"), vendor


def test_a_money_and_a_json_column_are_measured_through_their_comparable_forms(
    postgres_test_db: dict[str, str],
) -> None:
    """`money` has no AVG and `json` no equality, so both are read through a cast."""

    with psycopg.connect(
        host=postgres_test_db["host"],
        port=int(postgres_test_db["port"]),
        dbname=postgres_test_db["database"],
        user=postgres_test_db["user"],
        autocommit=True,
    ) as conn:
        conn.execute("CREATE TABLE seedbank.type_probe (id int, price money, doc json)")
        conn.execute(
            "INSERT INTO seedbank.type_probe VALUES "
            "(1, 1.50, '{\"a\": 1}'), (2, 2.00, '{\"a\": 2}'), (3, 1.50, '{\"a\": 1}')",
        )

    adapter = PostgresAdapter(postgres_test_db)
    adapter.connect()

    try:
        fqn = next(
            t.fqn
            for t in adapter.list_tables(include=["*"], exclude=[])
            if t.fqn.endswith(".type_probe")
        )
        columns = adapter.introspect_columns(fqn)
        _, stats = adapter.compute_statistics(fqn, columns, StatisticsConfig(), frozenset())
    finally:
        adapter.close()

    assert (stats["price"].cardinality, stats["doc"].cardinality) == (2, 2)
    assert [(float(v.value), v.count) for v in stats["price"].values or ()] == [(1.5, 2), (2.0, 1)]


def test_a_planner_estimate_of_zero_is_unknown_not_zero() -> None:
    from dbprint.adapters.postgres import stats as pg_stats

    class _Row:
        def fetchone(self) -> tuple[float]:
            return (0.0,)

    identity: Any = type("I", (), {"addressed": ("public", "t")})()
    connection: Any = None

    with patch.object(pg_stats, "exec_query", lambda *_a, **_k: _Row()):
        assert pg_stats._approximate_cardinality(connection, identity, "doc", 1_200_000, 0) is None


def _viability(adapter: Adapter) -> tuple[str, list[ColumnMeta]]:
    fqn = next(
        t.fqn
        for t in adapter.list_tables(include=["*"], exclude=[])
        if t.fqn.rsplit(".", 1)[-1] == "viability_check"
    )

    return fqn, adapter.introspect_columns(fqn)


def test_a_failing_phase_b_statement_degrades_one_column_on_every_adapter(
    sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    empty_stats_config: StatisticsConfig,
) -> None:
    vendor, factory = sql_adapter_factory
    adapter = factory()
    fqn, columns = _viability(adapter)
    counts, phase_a = adapter.compute_base_statistics(fqn, columns, empty_stats_config)
    clean = adapter.compute_column_statistics(
        fqn,
        columns,
        empty_stats_config,
        counts,
        phase_a.stats,
        frozenset(),
    )
    stats_module = importlib.import_module(f"dbprint.adapters.{vendor}.stats")
    real = stats_module.exec_query

    def poisoned(cursor: Any, sql: str, *args: Any, **kwargs: Any) -> Any:
        if "label" in sql:
            raise RuntimeError("simulated operator failure")

        return real(cursor, sql, *args, **kwargs)

    with patch.object(stats_module, "exec_query", poisoned):
        degraded = adapter.compute_column_statistics(
            fqn,
            columns,
            empty_stats_config,
            counts,
            phase_a.stats,
            frozenset(),
        )

    assert degraded.unmeasured == ("label",)
    assert {name: degraded[name] for name in degraded} == {
        name: clean[name] for name in clean if name != "label"
    }


class _SampleFailing(MockAdapter):
    def __init__(self, fixture: Any, failing: str = "herbarium_id") -> None:
        super().__init__(fixture)
        self._failing = failing

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: Any = None,
        sql_type: str | None = None,
    ) -> list[Any]:
        if column == self._failing:
            raise RuntimeError("could not identify an equality operator")

        return super().sample_values(fqn, column, n, scope, sql_type)


class TestTheEngineContainsAColumn:
    def _columns(self, tmp_path: Path, adapter: MockAdapter) -> dict[str, Any]:
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()
        path = tmp_path / "primary" / "public" / "curator" / "statistics.yaml"

        return yaml.safe_load(path.read_text())["columns"]

    def test_a_failed_sample_draw_publishes_no_value_and_names_what_it_owed(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        clean = self._columns(tmp_path / "clean", MockAdapter(_curator_fixture()))

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            degraded = self._columns(tmp_path / "degraded", _SampleFailing(_curator_fixture()))

        column = degraded["herbarium_id"]

        assert "values" not in column
        assert "looks_like" not in (column.get("inferred") or {})
        assert column["unmeasured"]
        assert column["cardinality"] == clean["herbarium_id"]["cardinality"]
        assert degraded["id"] == clean["id"]
        assert "could not measure 1 column(s) of 'public.curator' (herbarium_id)" in caplog.text

    def test_a_phase_b_failure_keeps_the_table_and_the_phase_a_fields(self, tmp_path: Path) -> None:
        class _PhaseBFailing(MockAdapter):
            def compute_column_statistics(
                self,
                fqn: str,
                columns: list[ColumnMeta],
                *args: Any,
                **kwargs: Any,
            ):
                result = super().compute_column_statistics(fqn, columns, *args, **kwargs)
                stats = {n: s for n, s in result.stats.items() if n != "herbarium_id"}

                return replace(
                    result,
                    stats=stats,
                    unmeasured=("herbarium_id",),
                    failures=(RuntimeError("x"),),
                )

        clean = self._columns(tmp_path / "clean", MockAdapter(_curator_fixture()))
        degraded = self._columns(tmp_path / "degraded", _PhaseBFailing(_curator_fixture()))

        assert degraded["herbarium_id"]["classification"] == clean["herbarium_id"]["classification"]
        assert degraded["herbarium_id"]["cardinality"] == clean["herbarium_id"]["cardinality"]
        assert degraded["herbarium_id"]["unmeasured"]
        assert degraded["id"] == clean["id"]


class TestAnUndrawnSampleIsNotAFinding:
    """SPEC 2.2.4: a failed draw names the verdicts it owed, so no reader takes it for "none"."""

    def _run(self, tmp_path: Path, adapter: MockAdapter) -> tuple[dict[str, Any], list[Any]]:
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate(GenerateRequest(force=True))
        prints = tmp_path / "primary"
        stats = yaml.safe_load((prints / "public" / "curator" / "statistics.yaml").read_text())
        changes = yaml.safe_load((prints / "diff.yaml").read_text())["changes"]

        return stats["columns"]["id"], changes

    def test_the_column_names_both_verdicts_and_the_print_validates(self, tmp_path: Path) -> None:
        column, _ = self._run(tmp_path, _SampleFailing(_curator_fixture(), failing="id"))

        assert {"inferred.looks_like", "inferred.epoch_unit"} <= set(column["unmeasured"])
        assert [i for i in validate_print(tmp_path / "primary") if i.severity == "error"] == []

    def test_neither_the_failed_run_nor_the_next_reports_the_verdict_as_changed(
        self,
        tmp_path: Path,
    ) -> None:
        baseline, _ = self._run(tmp_path, MockAdapter(_curator_fixture()))
        _, failed = self._run(tmp_path, _SampleFailing(_curator_fixture(), failing="id"))
        _, healed = self._run(tmp_path, MockAdapter(_curator_fixture()))

        assert baseline["inferred"]["looks_like"] == "uuid"
        assert [c for c in [*failed, *healed] if c.get("stat") == "inferred.looks_like"] == []


def test_every_duckdb_type_leaves_its_table_profiled(tmp_path: Path) -> None:
    path = tmp_path / "sweep.duckdb"
    con = duckdb.connect(str(path), config={"storage_compatibility_version": "latest"})
    logical = con.execute(
        "SELECT DISTINCT logical_type FROM duckdb_types() "
        "WHERE database_name = 'system' AND logical_type NOT IN ('NULL', 'TYPE', 'INVALID')",
    ).fetchall()
    declarations = [_DUCKDB_DECLARATIONS.get(name, name) for (name,) in logical]
    columns = ", ".join(f"c{i} {decl}" for i, decl in enumerate(declarations))
    con.execute(f"CREATE TABLE sweep (id INTEGER, {columns})")
    con.execute("INSERT INTO sweep (id) SELECT i FROM range(3) t(i)")
    con.close()

    conn = ConnectionConfig(name="garden", adapter="duckdb", output=tmp_path / "prints")
    result = Engine(DuckdbAdapter({"database": str(path)}), conn, tmp_path).generate()

    assert [(t.fqn, t.error) for t in result.tables if t.status != "ok"] == []


def test_every_postgres_base_type_leaves_its_table_profiled(
    postgres_test_db: dict[str, str],
    tmp_path: Path,
) -> None:
    creds = postgres_test_db

    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password=creds["password"],
        autocommit=True,
    ) as conn:
        rows = conn.execute(
            """
            SELECT pg_catalog.format_type(t.oid, NULL)
            FROM pg_type t
            WHERE t.typtype = 'b'
              AND t.typnamespace = 'pg_catalog'::regnamespace
              AND t.typarray <> 0
            """,
        ).fetchall()
        types = [name for (name,) in rows if name not in _POSTGRES_SKIPPED]
        declared = ['"char"' if name == "char" else name for name in types]
        columns = ", ".join(f"c{i} {decl}" for i, decl in enumerate(declared))
        conn.execute(cast(LiteralString, f"CREATE TABLE public.sweep (id integer, {columns})"))
        conn.execute("INSERT INTO public.sweep (id) SELECT i FROM generate_series(1, 3) i")

    conn_config = ConnectionConfig(name="garden", adapter="postgres", output=tmp_path / "prints")
    result = Engine(PostgresAdapter(creds), conn_config, tmp_path).generate()

    assert [(t.fqn, t.error) for t in result.tables if t.status != "ok"] == []


def test_a_variant_column_leaves_the_databricks_grain_search_standing(
    databricks_test_schema: Any,
    request: pytest.FixtureRequest,
) -> None:
    databricks_test_schema.execute(
        "CREATE TABLE sown USING DELTA AS "
        "SELECT id, to_variant_object(named_struct('plot', id)) AS doc "
        "FROM range(20)",
    )
    adapter = _adapter_factory_for(request, "databricks")()
    fqn = next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if t.fqn.endswith(".sown")
    )
    columns = adapter.introspect_columns(fqn)
    counts, phase_a = adapter.compute_base_statistics(fqn, columns, StatisticsConfig())

    found = adapter.probe_grain(fqn, columns, counts, (("doc", "id"),))
    strengths = adapter.probe_dependencies(fqn, columns, counts, phase_a.stats, (("doc", "id"),))

    assert found == (("doc", "id"),)
    assert set(strengths) == {("doc", "id")}
