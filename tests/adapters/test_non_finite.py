"""Numeric statistics read finite values only; NaN and the infinities are counted apart (SPEC 2.2.4)."""

from __future__ import annotations

import importlib
import math
from decimal import Decimal
from typing import Any, LiteralString, cast

import pytest

from dbprint.adapters import StatisticsConfig
from dbprint.adapters.base import numeric_block_from_row
from tests.adapters.conftest import _adapter_factory_for
from tests.conftest import pg_connect


_MEASURED = ["duckdb", "postgres", "clickhouse", "snowflake", "redshift"]

_FLOAT = {
    "duckdb": "DOUBLE",
    "snowflake": "DOUBLE",
    "postgres": "double precision",
    "redshift": "double precision",
    "clickhouse": "Nullable(Float64)",
}
_INT = {
    "duckdb": "INTEGER",
    "snowflake": "INTEGER",
    "postgres": "integer",
    "redshift": "integer",
    "clickhouse": "Int32",
}
_SPECIAL = {
    "duckdb": ("'nan'", "'inf'", "'-inf'"),
    "snowflake": ("'nan'", "'inf'", "'-inf'"),
    "postgres": ("'NaN'", "'Infinity'", "'-Infinity'"),
    "redshift": ("'NaN'", "'Infinity'", "'-Infinity'"),
    "clickhouse": ("'nan'", "'inf'", "'-inf'"),
}


@pytest.mark.parametrize("vendor", _MEASURED)
def test_nan_and_both_infinities_are_counted_apart_from_the_finite_statistics(
    vendor: str,
    request: pytest.FixtureRequest,
) -> None:
    _seed(vendor, request)
    adapter = _adapter_factory_for(request, vendor)()
    fqn = next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if t.fqn.endswith(".gauge")
    )
    _, stats = adapter.compute_statistics(
        fqn,
        adapter.introspect_columns(fqn),
        StatisticsConfig(),
        frozenset(),
    )
    adapter.close()
    level, n = stats["reading"], stats["n"]

    assert level.range is not None
    assert (level.range.min, level.range.max) == (-3, 199)
    assert level.sum == 19897
    assert level.mean == pytest.approx(19897 / 201)
    assert level.non_finite_count == 3
    assert level.negative_count == 1
    assert level.quantized_count == 201
    assert level.percentiles
    assert all(math.isfinite(v) and -3 <= v <= 199 for v in level.percentiles.values())
    assert n.non_finite_count is None


class TestFiniteValueText:
    """The predicate each adapter wraps a float column in, and the identity on every other type."""

    @pytest.mark.parametrize(
        ("vendor", "float_type", "expected"),
        [
            ("duckdb", "DOUBLE", "CASE WHEN ISFINITE(x) THEN x END"),
            (
                "postgres",
                "double precision",
                "CASE WHEN x NOT IN ('NaN', 'Infinity', '-Infinity') THEN x END",
            ),
            (
                "postgres",
                "numeric(10,2)",
                "CASE WHEN x::TEXT NOT IN ('NaN', 'Infinity', '-Infinity') THEN x END",
            ),
            (
                "redshift",
                "double precision",
                (
                    "CASE WHEN x NOT IN ('NaN'::FLOAT8, 'Infinity'::FLOAT8, '-Infinity'::FLOAT8) "
                    "THEN x END"
                ),
            ),
            (
                "snowflake",
                "FLOAT",
                "CASE WHEN x NOT IN ('NaN'::FLOAT, 'inf'::FLOAT, '-inf'::FLOAT) THEN x END",
            ),
            ("bigquery", "FLOAT64", "CASE WHEN NOT IS_NAN(x) AND NOT IS_INF(x) THEN x END"),
            ("clickhouse", "Float64", "CASE WHEN isFinite(x) THEN x END"),
            (
                "databricks",
                "DOUBLE",
                (
                    "CASE WHEN NOT ISNAN(x) AND x NOT IN (CAST('inf' AS DOUBLE), CAST('-inf' AS DOUBLE)) "
                    "THEN x END"
                ),
            ),
        ],
    )
    def test_a_float_column_reads_finite_values_only(
        self,
        vendor: str,
        float_type: str,
        expected: str,
    ) -> None:
        assert _finite_value(vendor)("x", float_type) == expected

    @pytest.mark.parametrize(
        ("vendor", "sql_type"),
        [
            ("duckdb", "INTEGER"),
            ("duckdb", "DECIMAL(10,2)"),
            ("postgres", "bigint"),
            ("redshift", "numeric(10,2)"),
            ("snowflake", "NUMBER(38,0)"),
            ("bigquery", "NUMERIC"),
            ("clickhouse", "Decimal(10, 2)"),
            ("databricks", "BIGINT"),
            ("mysql", "double"),
            ("mysql", "int"),
        ],
    )
    def test_a_type_holding_no_non_finite_value_keeps_its_sql_unchanged(
        self,
        vendor: str,
        sql_type: str,
    ) -> None:
        assert _finite_value(vendor)("x", sql_type) == "x"


class TestTheNumericBlock:
    """What the shared assembly publishes from an adapter's finite-only row."""

    def test_an_overflowing_sum_is_unmeasured_never_published(self) -> None:
        config = StatisticsConfig()
        row = (1.0e308, 1.5e308, 1.25e308, math.inf)
        quantiles = [1.25e308] * len(config.percentiles)

        rng, _, _, _, _, mean, total, non_finite, unmeasured, _ = numeric_block_from_row(
            row,
            quantiles,
            config,
            None,
            finite=2,
            non_null=2,
        )

        assert rng is not None
        assert (rng.max, mean, total, non_finite, unmeasured) == (
            1.5e308,
            1.25e308,
            None,
            None,
            ("sum",),
        )

    def test_a_non_finite_percentile_drops_the_percentiles(self) -> None:
        config = StatisticsConfig()
        quantiles = [1.0] * (len(config.percentiles) - 1) + [math.nan]

        _, percentiles, *_, unmeasured, _ = numeric_block_from_row(
            (1.0, 2.0, 1.5, 3.0),
            quantiles,
            config,
            None,
            finite=2,
            non_null=2,
        )

        assert (percentiles, unmeasured) == (None, ("percentiles",))

    def test_a_column_with_no_finite_value_publishes_no_bounds_mean_or_sum(self) -> None:
        config = StatisticsConfig()

        rng, percentiles, *_, mean, total, non_finite, unmeasured, _ = numeric_block_from_row(
            (None, None, None, None),
            [None] * len(config.percentiles),
            config,
            None,
            finite=0,
            non_null=3,
        )

        assert (rng, percentiles, mean, total, non_finite, unmeasured) == (
            None,
            None,
            None,
            None,
            3,
            ("percentiles", "range"),
        )

    def test_a_non_finite_decimal_mean_is_unmeasured(self) -> None:
        config = StatisticsConfig()

        *_, mean, _, _, unmeasured, _ = numeric_block_from_row(
            (Decimal(1), Decimal(2), Decimal("NaN"), Decimal(3)),
            [Decimal(1)] * len(config.percentiles),
            config,
            None,
        )

        assert (mean, unmeasured) == (None, ("mean",))

    def test_a_null_mean_and_sum_beside_a_measured_minimum_are_unmeasured(self) -> None:
        config = StatisticsConfig()

        *_, mean, total, _, unmeasured, _ = numeric_block_from_row(
            (1, 2, None, None),
            [1] * len(config.percentiles),
            config,
            None,
        )

        assert (mean, total, unmeasured) == (None, None, ("mean", "sum"))

    def test_an_empty_population_names_nothing(self) -> None:
        *_, unmeasured, _ = numeric_block_from_row(
            (None, None, None, None),
            [],
            StatisticsConfig(),
            None,
        )

        assert unmeasured == ()

    def test_the_failure_comes_back_in_the_last_slot(self) -> None:
        failure = RuntimeError("Overflow in HUGEINT addition")

        *_, cause = numeric_block_from_row(
            (1, 2, None, None),
            [1] * len(StatisticsConfig().percentiles),
            StatisticsConfig(),
            None,
            failure=failure,
        )

        assert cause is failure


def _finite_value(vendor: str) -> Any:
    return importlib.import_module(f"dbprint.adapters.{vendor}.rendering").finite_value


def _rows(vendor: str) -> str:
    nan, inf, ninf = _SPECIAL[vendor]
    t = _FLOAT[vendor].removeprefix("Nullable(").removesuffix(")")
    finite = [-3, *range(200)]
    cells = [str(v) for v in finite] + [f"CAST({s} AS {t})" for s in (nan, inf, ninf)] + ["NULL"]

    return ", ".join(f"({cell}, {i})" for i, cell in enumerate(cells))


def _ddl(vendor: str, table: str, suffix: str = "") -> list[str]:
    return [
        f"CREATE TABLE {table} (reading {_FLOAT[vendor]}, n {_INT[vendor]}){suffix}",
        f"INSERT INTO {table} VALUES {_rows(vendor)}",
    ]


def _seed(vendor: str, request: pytest.FixtureRequest) -> None:
    if vendor == "duckdb":
        con = request.getfixturevalue("duckdb_native_connection")

        for statement in _ddl(vendor, "seedbank.gauge"):
            con.execute(statement)
    elif vendor == "snowflake":
        shim = request.getfixturevalue("snowflake_duckdb_connection")

        for statement in _ddl(vendor, "seedbank.gauge"):
            shim.execute(statement)
    elif vendor == "postgres":
        with pg_connect(request.getfixturevalue("postgres_test_db")) as conn:
            for statement in [*_ddl(vendor, "seedbank.gauge"), "ANALYZE"]:
                conn.execute(cast(LiteralString, statement))
    elif vendor == "redshift":
        shim = request.getfixturevalue("redshift_postgres_connection")

        for statement in [*_ddl(vendor, "seedbank.gauge"), "ANALYZE"]:
            shim.execute(statement)
    else:
        cursor = request.getfixturevalue("clickhouse_native_connection")

        for statement in _ddl(vendor, "seedbank.gauge", " ENGINE = Memory"):
            cursor.execute(statement)
