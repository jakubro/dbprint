"""A column's sum and mean near its type's limit: exact, or named unmeasured, and never wrapped."""

from __future__ import annotations

from typing import Any, LiteralString, cast

import pytest

from dbprint.adapters import ColumnStats, StatisticsConfig
from tests.adapters.conftest import SQL_PARAMS, _adapter_factory_for, _mysql_exec_many
from tests.conftest import pg_connect


_ROWS = 200
_INT64_BASE = 9223372036854775000
_INT64_TOTAL = 1844674407370955019900

_INT64 = {
    "postgres": "bigint",
    "redshift": "bigint",
    "mysql": "BIGINT",
    "duckdb": "BIGINT",
    "snowflake": "DECIMAL(19,0)",
    "clickhouse": "Int64",
    "databricks": "BIGINT",
    "bigquery": "INT64",
}


@pytest.mark.parametrize("vendor", SQL_PARAMS)
def test_a_64_bit_total_is_exact_and_its_mean_inside_the_range(
    vendor: str,
    request: pytest.FixtureRequest,
) -> None:
    seed_count = _measure(vendor, request, _INT64[vendor], _INT64_BASE)

    assert seed_count.range is not None
    assert seed_count.mean is not None

    if vendor == "bigquery":
        # The emulator decodes BIGNUMERIC as a float.
        assert seed_count.sum == pytest.approx(_INT64_TOTAL, rel=1e-15)
    else:
        assert seed_count.sum == _INT64_TOTAL

    assert float(seed_count.range.min) <= seed_count.mean <= float(seed_count.range.max)
    assert seed_count.mean == pytest.approx(_INT64_TOTAL / _ROWS, rel=1e-12)


@pytest.mark.parametrize(
    ("vendor", "sql_type", "base", "total"),
    [
        pytest.param(
            "duckdb",
            "HUGEINT",
            170141183460469231731687303715884105000,
            34028236692093846346337460743176821019900,
            id="duckdb-hugeint",
        ),
        pytest.param(
            "duckdb",
            "UHUGEINT",
            340282366920938463463374607431768211000,
            68056473384187692692674921486353642219900,
            id="duckdb-uhugeint",
        ),
        pytest.param(
            "clickhouse",
            "UInt64",
            18446744073709551000,
            3689348814741910219900,
            id="clickhouse-uint64",
        ),
        pytest.param(
            "clickhouse",
            "Int128",
            170141183460469231731687303715884105000,
            34028236692093846346337460743176821019900,
            id="clickhouse-int128",
        ),
        pytest.param(
            "clickhouse",
            "Decimal(38, 0)",
            99999999999999999999999999999999999000,
            19999999999999999999999999999999999819900,
            id="clickhouse-decimal38",
        ),
    ],
)
def test_a_total_past_the_columns_own_width_is_exact(
    vendor: str,
    sql_type: str,
    base: int,
    total: int,
    request: pytest.FixtureRequest,
) -> None:
    seed_count = _measure(vendor, request, sql_type, base)

    assert seed_count.sum == total
    assert seed_count.range is not None
    assert seed_count.unmeasured is None


@pytest.mark.parametrize("vendor", ["duckdb", "snowflake", "databricks"])
def test_a_total_no_exact_type_holds_costs_the_mean_and_sum_alone(
    vendor: str,
    request: pytest.FixtureRequest,
) -> None:
    stock = _measure(vendor, request, "DECIMAL(38,0)", 99999999999999999999999999999999999000)

    assert stock.unmeasured == ("mean", "sum")
    assert stock.unmeasured_cause is not None
    assert (stock.mean, stock.sum) == (None, None)
    assert stock.range is not None
    assert stock.percentiles
    assert stock.values


def test_a_null_total_under_spark_without_ansi_names_both_and_no_cause(
    request: pytest.FixtureRequest,
) -> None:
    cursor = request.getfixturevalue("databricks_test_schema")
    cursor.execute("SET spark.sql.ansi.enabled = false")

    try:
        stock = _measure(
            "databricks",
            request,
            "DECIMAL(38,0)",
            99999999999999999999999999999999999000,
        )
    finally:
        cursor.execute("SET spark.sql.ansi.enabled = true")

    assert stock.unmeasured == ("mean", "sum")
    assert stock.unmeasured_cause is None
    assert stock.range is not None


def test_a_256_bit_total_that_could_wrap_is_unmeasured_without_a_failure(
    request: pytest.FixtureRequest,
) -> None:
    base = 57896044618658097711785492504343953926634992332820282019728792003956564819000
    seed_count = _measure("clickhouse", request, "Int256", base)

    assert seed_count.unmeasured == ("mean", "sum")
    assert seed_count.unmeasured_cause is None
    assert seed_count.range is not None


def test_a_256_bit_total_far_from_the_limit_is_exact(request: pytest.FixtureRequest) -> None:
    rows = [f"CAST('{(i + 1) * 10**24}' AS UInt256)" for i in range(_ROWS)]
    seed_count = _measure("clickhouse", request, "UInt256", rows=rows)

    assert seed_count.sum == 20100000000000000000000000000


def _measure(
    vendor: str,
    request: pytest.FixtureRequest,
    sql_type: str,
    base: int = 0,
    *,
    rows: list[str] | None = None,
) -> ColumnStats:
    values = rows or [f"CAST('{base + i}' AS {sql_type})" for i in range(_ROWS)]
    _SEEDERS[vendor](request, sql_type, values)
    adapter = _adapter_factory_for(request, vendor)()

    try:
        fqn = next(
            t.fqn
            for t in adapter.list_tables(include=["*"], exclude=[])
            if t.fqn.endswith(".tally")
        )
        columns = adapter.introspect_columns(fqn)
        _, stats = adapter.compute_statistics(fqn, columns, StatisticsConfig(), frozenset())
    finally:
        adapter.close()

    return stats["seed_count"]


def _statements(table: str, sql_type: str, values: list[str], suffix: str = "") -> list[str]:
    rows = ", ".join(f"({value})" for value in values)

    return [
        f"CREATE TABLE {table} (seed_count {sql_type}){suffix}",
        f"INSERT INTO {table} VALUES {rows}",
    ]


def _seed_duckdb(request: pytest.FixtureRequest, sql_type: str, values: list[str]) -> None:
    con = request.getfixturevalue("duckdb_native_connection")

    for statement in _statements("seedbank.tally", sql_type, values):
        con.execute(statement)


def _seed_snowflake(request: pytest.FixtureRequest, sql_type: str, values: list[str]) -> None:
    shim = request.getfixturevalue("snowflake_duckdb_connection")

    for statement in _statements("seedbank.tally", sql_type, values):
        shim.execute(statement)


def _seed_postgres(request: pytest.FixtureRequest, sql_type: str, values: list[str]) -> None:
    with pg_connect(request.getfixturevalue("postgres_test_db")) as conn:
        for statement in [*_statements("seedbank.tally", sql_type, values), "ANALYZE"]:
            conn.execute(cast(LiteralString, statement))


def _seed_redshift(request: pytest.FixtureRequest, sql_type: str, values: list[str]) -> None:
    shim = request.getfixturevalue("redshift_postgres_connection")

    for statement in [*_statements("seedbank.tally", sql_type, values), "ANALYZE"]:
        shim.execute(statement)


def _seed_mysql(request: pytest.FixtureRequest, sql_type: str, values: list[str]) -> None:
    creds = request.getfixturevalue("mysql_test_db")
    plain = [value.replace(f" AS {sql_type})", " AS SIGNED)") for value in values]
    statements = [*_statements("tally", sql_type, plain), "ANALYZE TABLE tally"]
    _mysql_exec_many(int(creds["port"]), creds["database"], statements)


def _seed_clickhouse(request: pytest.FixtureRequest, sql_type: str, values: list[str]) -> None:
    cursor = request.getfixturevalue("clickhouse_native_connection")

    for statement in _statements("seedbank.tally", sql_type, values, " ENGINE = Memory"):
        cursor.execute(statement)


def _seed_databricks(request: pytest.FixtureRequest, sql_type: str, values: list[str]) -> None:
    cursor = request.getfixturevalue("databricks_test_schema")

    for statement in _statements("tally", sql_type, values, " USING DELTA"):
        cursor.execute(statement)


def _seed_bigquery(request: pytest.FixtureRequest, sql_type: str, values: list[str]) -> None:
    cursor, dataset = request.getfixturevalue("bigquery_test_dataset")

    for statement in _statements(f"`dbprint-test`.`{dataset}`.tally", sql_type, values):
        cursor.execute(statement)


_SEEDERS: dict[str, Any] = {
    "bigquery": _seed_bigquery,
    "clickhouse": _seed_clickhouse,
    "databricks": _seed_databricks,
    "duckdb": _seed_duckdb,
    "mysql": _seed_mysql,
    "postgres": _seed_postgres,
    "redshift": _seed_redshift,
    "snowflake": _seed_snowflake,
}
