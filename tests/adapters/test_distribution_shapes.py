"""Every distribution shape SPEC 2.2.5 defines, on a column that carries no value list.

Numeric and temporal columns are enumerated only as top-N frequencies, and the conformance
validator checks `distribution` against an exhaustive `values` map these columns never carry,
so nothing else covers their verdict. The Snowflake case runs on duckdb via
`SnowflakeDialectShim`, proving the adapter's own SQL, not duckdb's query planner.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import LiteralString, cast

import duckdb
import pytest

from dbprint.adapters import (
    Adapter,
    MysqlAdapter,
    PostgresAdapter,
    SnowflakeAdapter,
    StatisticsConfig,
)


# Small enough that the columns clear `enumeration_threshold` for the numeric/temporal branches.
CONFIG = StatisticsConfig(enumeration_threshold=3, top_n_values=5)

VENDORS = ["postgres", "mysql", "snowflake"]

# value -> occurrences, per shape: dominant is >= 95% of the non-null rows, uniform is every
# count within 2x, imbalanced is more than 2x, long_tail is past top_n_values with a top 5
# covering under 30%.
SHAPES: dict[str, list[int]] = {
    "dominant": [96, 1, 1, 1, 1],
    "uniform": [20, 20, 20, 20, 20],
    "imbalanced": [50, 10, 10, 10, 10],
    "long_tail": [3] * 40,
}

EXPECTED = {
    "dominant": "dominant_value",
    "uniform": "uniform",
    "imbalanced": "imbalanced",
    "long_tail": "long_tail",
}


def _rows(counts: list[int]) -> list[int]:
    """Bucket index per row, so bucket `i` occurs `counts[i]` times."""

    return [index for index, count in enumerate(counts) for _ in range(count)]


def _value_rows(counts: list[int]) -> str:
    """`(n, t)` literal pairs for one bucket list - one plain INSERT, every vendor."""

    return ", ".join(
        f"({b}, '{(date(2026, 1, 1) + timedelta(days=b)).isoformat()} 00:00:00')"
        for b in _rows(counts)
    )


def _seed_postgres(creds: dict[str, str], counts: list[int]) -> None:
    import psycopg

    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password="",
        autocommit=True,
    ) as conn:
        conn.execute("CREATE TABLE public.shaped (n int, t timestamp)")
        # Assembled at runtime so LiteralString does not apply; the values come from SHAPES.
        conn.execute(
            cast(LiteralString, f"INSERT INTO public.shaped (n, t) VALUES {_value_rows(counts)}"),
        )


def _seed_mysql(creds: dict[str, str], counts: list[int]) -> None:
    import mysql.connector

    conn = mysql.connector.connect(
        host=creds["host"],
        port=int(creds["port"]),
        database=creds["database"],
        user=creds["user"],
        password=creds.get("password", ""),
        autocommit=True,
    )

    try:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE shaped (n INT, t DATETIME)")
        cursor.execute(f"INSERT INTO shaped (n, t) VALUES {_value_rows(counts)}")
        cursor.close()
    finally:
        conn.close()


def _seed_snowflake(con: duckdb.DuckDBPyConnection, counts: list[int]) -> None:
    con.execute("CREATE TABLE shaped (n INTEGER, t TIMESTAMP)")
    con.execute(f"INSERT INTO shaped (n, t) VALUES {_value_rows(counts)}")


def _adapter(vendor: str, request: pytest.FixtureRequest, counts: list[int]) -> Adapter:
    """A connected adapter over a fresh `shaped(n, t)` table, seeded per vendor."""

    if vendor == "postgres":
        creds = request.getfixturevalue("postgres_test_db")
        _seed_postgres(creds, counts)
        adapter: Adapter = PostgresAdapter(creds)
    elif vendor == "mysql":
        creds = request.getfixturevalue("mysql_test_db")
        _seed_mysql(creds, counts)
        adapter = MysqlAdapter(creds)
    elif vendor == "snowflake":
        con = request.getfixturevalue("snowflake_duckdb_connection")
        _seed_snowflake(con, counts)
        sf_creds = {
            "account": "test-account",
            "user": "test-user",
            "password": "test-password",
            "warehouse": "test-warehouse",
            "database": "memory",
            "role": "test-role",
        }
        adapter = SnowflakeAdapter(sf_creds, cursor_factory=lambda _params: con)
    else:
        raise ValueError(f"unknown vendor: {vendor!r}")

    adapter.connect()

    return adapter


def _profile(adapter: Adapter) -> dict:
    """Phase A + B over `shaped`, found by name - vendors differ on schema depth."""

    table = next(iter(adapter.list_tables(include=["*.shaped"], exclude=[])))
    columns = adapter.introspect_columns(table.fqn)

    return adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]


class TestDistributionShapes:
    """The verdict follows the data, on every vendor's own top-N fetch."""

    @pytest.mark.parametrize("vendor", VENDORS)
    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_numeric_column_reports_its_shape(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
        shape: str,
    ) -> None:
        adapter = _adapter(vendor, request, SHAPES[shape])

        try:
            assert _profile(adapter)["n"].distribution == EXPECTED[shape]
        finally:
            adapter.close()

    @pytest.mark.parametrize("vendor", VENDORS)
    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_temporal_column_reports_its_shape(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
        shape: str,
    ) -> None:
        """The same four shapes over timestamps, which take the other branch."""

        adapter = _adapter(vendor, request, SHAPES[shape])

        try:
            assert _profile(adapter)["t"].distribution == EXPECTED[shape]
        finally:
            adapter.close()


class TestTheOverFetchBoundary:
    """Exactly `top_n_values` distinct values must read exhaustive, one more must not - telling
    a whole list from a truncated one needs the over-fetch row.
    """

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_cardinality_at_the_cap_reads_exhaustive(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        counts = [2] * CONFIG.top_n_values
        adapter = _adapter(vendor, request, counts)

        try:
            stats = _profile(adapter)["n"]
            assert stats.cardinality == CONFIG.top_n_values
            assert stats.frequencies.listed == stats.cardinality
        finally:
            adapter.close()

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_cardinality_one_past_the_cap_reads_truncated(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        counts = [2] * (CONFIG.top_n_values + 1)
        adapter = _adapter(vendor, request, counts)

        try:
            stats = _profile(adapter)["n"]
            assert stats.cardinality == CONFIG.top_n_values + 1
            assert stats.frequencies.listed == CONFIG.top_n_values
            assert stats.frequencies.listed != stats.cardinality
        finally:
            adapter.close()

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_a_share_just_under_the_long_tail_threshold_is_long_tail(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        """17 equal buckets is the smallest cardinality whose top 5 cover under 30% - the
        tightest margin `is exhaustive` can get wrong. `n` routes to `numeric` once its
        cardinality clears the threshold, regardless of uniqueness (SPEC 4.2).
        """

        counts = [3] * 17
        adapter = _adapter(vendor, request, counts)

        try:
            assert _profile(adapter)["n"].distribution == "long_tail"
        finally:
            adapter.close()


class TestAdaptersAgreeOnTheSameData:
    """Three adapters profiling one shape must not answer three different ways.

    `imbalanced`'s verdict turns on the top-to-smallest spread, so agreement here means the
    three top-N fetches agree rather than all falling through to one default.
    """

    @pytest.mark.parametrize("column", ["n", "t"])
    def test_the_shape_reads_the_same_verdict_everywhere(
        self,
        request: pytest.FixtureRequest,
        column: str,
    ) -> None:
        verdicts = {}

        for vendor in VENDORS:
            adapter = _adapter(vendor, request, SHAPES["imbalanced"])

            try:
                verdicts[vendor] = _profile(adapter)[column].distribution
            finally:
                adapter.close()

        assert len(set(verdicts.values())) == 1, f"adapters disagree on {column}: {verdicts}"
        assert verdicts[VENDORS[0]] == EXPECTED["imbalanced"]


class TestPercentileAgreement:
    """Three adapters compute `percentiles` three different ways and must still agree.

    `imbalanced` is the fixture because every default percentile lands deep inside a bucket, so
    linear interpolation and nearest-rank selection resolve to the same value; a uniform fixture
    passes under any rank rule and would prove nothing.
    """

    @pytest.mark.parametrize("column", ["n", "t"])
    def test_the_default_percentiles_agree_everywhere(
        self,
        request: pytest.FixtureRequest,
        column: str,
    ) -> None:
        percentile_sets = {}

        for vendor in VENDORS:
            adapter = _adapter(vendor, request, SHAPES["imbalanced"])

            try:
                percentile_sets[vendor] = _profile(adapter)[column].percentiles
            finally:
                adapter.close()

        first = percentile_sets[VENDORS[0]]
        assert all(other == first for other in percentile_sets.values()), percentile_sets
        # Guards the fixture: one constant value would pass the agreement above vacuously.
        assert len(set(first.values())) > 1, "the shape resolved to one constant value"
