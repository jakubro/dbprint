"""Every distribution shape SPEC 2.2.5 defines, on numeric/temporal columns' top-N fetch -
never exhaustive there, so the `frequencies`-based check is their only route to `distribution`.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, ClassVar, LiteralString, cast

import duckdb
import pytest

from dbprint.adapters import (
    Adapter,
    BigqueryAdapter,
    ClickhouseAdapter,
    DatabricksAdapter,
    DuckdbAdapter,
    MysqlAdapter,
    PostgresAdapter,
    RedshiftAdapter,
    SnowflakeAdapter,
    StatisticsConfig,
)


# Small enough that the columns clear `enumeration_threshold` for the numeric/temporal branches.
CONFIG = StatisticsConfig(enumeration_threshold=3, top_n_values=5)

VENDORS = [
    "postgres",
    "mysql",
    "snowflake",
    "duckdb",
    "clickhouse",
    "redshift",
    "databricks",
    "bigquery",
]

_DATABRICKS_CREDS = {
    "server_hostname": "local",
    "http_path": "local",
    "access_token": "local",
    "catalog": "spark_catalog",
}

_BIGQUERY_PROJECT = "dbprint-test"

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


def _seed_duckdb(con: duckdb.DuckDBPyConnection, counts: list[int]) -> None:
    con.execute("CREATE TABLE shaped (n INTEGER, t TIMESTAMP)")
    con.execute(f"INSERT INTO shaped (n, t) VALUES {_value_rows(counts)}")


def _seed_clickhouse(cur: Any, counts: list[int]) -> None:
    cur.execute("CREATE TABLE seedbank.shaped (n Int32, t DateTime64(0)) ENGINE = Memory")
    cur.execute(f"INSERT INTO seedbank.shaped (n, t) VALUES {_value_rows(counts)}")


def _seed_redshift(shim: Any, counts: list[int]) -> None:
    shim.execute("CREATE TABLE public.shaped (n int, t timestamp)")
    shim.execute(f"INSERT INTO public.shaped (n, t) VALUES {_value_rows(counts)}")


def _value_rows_databricks(counts: list[int]) -> str:
    """Same pairs as `_value_rows`, with an explicit `TIMESTAMP` cast Spark's INSERT needs."""

    return ", ".join(
        f"({b}, TIMESTAMP '{(date(2026, 1, 1) + timedelta(days=b)).isoformat()} 00:00:00')"
        for b in _rows(counts)
    )


def _seed_databricks(cursor: Any, counts: list[int]) -> None:
    # TIMESTAMP_NTZ, since the cross-vendor agreement tests below compare rendered strings
    # verbatim and a tz-aware render carries a 'Z' suffix the naive ones never do.
    cursor.execute("CREATE TABLE shaped (n INT, t TIMESTAMP_NTZ) USING DELTA")
    cursor.execute(f"INSERT INTO shaped (n, t) VALUES {_value_rows_databricks(counts)}")


def _value_rows_bigquery(counts: list[int]) -> str:
    """Same pairs as `_value_rows`, with an explicit `DATETIME` literal - the agreement tests
    compare rendered strings verbatim, and a tz-aware render carries a 'Z' suffix (measured).
    """

    return ", ".join(
        f"({b}, DATETIME '{(date(2026, 1, 1) + timedelta(days=b)).isoformat()} 00:00:00')"
        for b in _rows(counts)
    )


def _seed_bigquery(cursor: Any, dataset: str, counts: list[int]) -> None:
    q = f"`{dataset}`"
    cursor.execute(f"CREATE TABLE {q}.shaped (n INT64, t DATETIME)")
    cursor.execute(f"INSERT INTO {q}.shaped (n, t) VALUES {_value_rows_bigquery(counts)}")


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
    elif vendor == "duckdb":
        con = request.getfixturevalue("duckdb_native_connection")
        _seed_duckdb(con, counts)
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
    elif vendor == "clickhouse":
        cur = request.getfixturevalue("clickhouse_native_connection")
        _seed_clickhouse(cur, counts)
        adapter = ClickhouseAdapter(
            {"host": "chdb", "database": "seedbank"},
            cursor_factory=lambda _params: cur,
        )
    elif vendor == "redshift":
        shim = request.getfixturevalue("redshift_postgres_connection")
        _seed_redshift(shim, counts)
        adapter = RedshiftAdapter(
            {"host": "redshift", "database": "seedbank", "user": "test", "password": "test"},
            cursor_factory=lambda _params: shim,
        )
    elif vendor == "databricks":
        cursor = request.getfixturevalue("databricks_test_schema")
        _seed_databricks(cursor, counts)
        adapter = DatabricksAdapter(_DATABRICKS_CREDS, cursor_factory=lambda _params: cursor)
    elif vendor == "bigquery":
        bq_cursor, dataset = request.getfixturevalue("bigquery_test_dataset")
        _seed_bigquery(bq_cursor, dataset, counts)
        adapter = BigqueryAdapter(
            {"project": _BIGQUERY_PROJECT, "dataset": dataset},
            cursor_factory=lambda _params: bq_cursor,
        )
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


class TestDominantValueIsNamed:
    """A `dominant_value` verdict names the literal it describes, not only its share (SPEC 2.2.3)
    - the top-N fetch's own ordering is what proves the value, not the fixture's build order.
    """

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_numeric_names_the_dominant_value(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        adapter = _adapter(vendor, request, SHAPES["dominant"])

        try:
            stats = _profile(adapter)["n"]
            assert stats.distribution == "dominant_value"
            assert stats.values
            assert stats.values[0].value == 0
            assert stats.values[0].count == 96
        finally:
            adapter.close()

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_temporal_names_the_dominant_instant(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        adapter = _adapter(vendor, request, SHAPES["dominant"])

        try:
            stats = _profile(adapter)["t"]
            assert stats.distribution == "dominant_value"
            assert stats.values
            assert str(stats.values[0].value).startswith("2026-01-01")
            assert stats.values[0].count == 96
        finally:
            adapter.close()

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_the_list_is_the_frequencies_fetch_truncated_at_the_cap(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        """The top-N fetch already grouped by value; naming it costs nothing further."""

        adapter = _adapter(vendor, request, SHAPES["long_tail"])

        try:
            stats = _profile(adapter)["n"]
            assert stats.values
            assert stats.frequencies is not None
            assert len(stats.values) == stats.frequencies.listed
        finally:
            adapter.close()


class TestNoStatementIsAddedForValues:
    """The top-N fetch already grouped by value; naming it must not cost a second roundtrip."""

    def test_the_top_n_fetch_runs_exactly_once(
        self,
        request: pytest.FixtureRequest,
    ) -> None:
        """Counting every statement is fragile against unrelated adapter-internal queries, so
        what gets asserted is the top-N fetch itself never running twice.
        """

        from tests.adapters.test_dialect_guard import _install_recorder

        con = request.getfixturevalue("duckdb_native_connection")
        con.execute("CREATE TABLE lone (n INTEGER)")
        values = ", ".join(f"({b})" for b in _rows(SHAPES["dominant"]))
        con.execute(f"INSERT INTO lone (n) VALUES {values}")
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
        adapter.connect()
        recorder = _install_recorder(adapter)

        try:
            table = next(iter(adapter.list_tables(include=["*.lone"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]

            assert stats["n"].values
            assert stats["n"].values[0].count == 96
            top_n_fetches = [s for s in recorder.statements if "ORDER BY cnt DESC" in s]
            assert len(top_n_fetches) == 1
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


class TestMeanAndSumPublishCentreOfMass:
    """`mean`/`sum` ride the same MIN/MAX/percentile statement as `range` (SPEC 2.2.4)."""

    # Five distinct values, one row each: mean and sum are hand-computed, not re-derived
    # from the adapter's own rounding.
    _COUNTS: ClassVar[list[int]] = [1, 1, 1, 1, 1]

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_numeric_mean_and_sum_match_an_independent_computation(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        adapter = _adapter(vendor, request, self._COUNTS)

        try:
            stats = _profile(adapter)["n"]
            assert stats.mean == pytest.approx(2.0)
            assert stats.sum == pytest.approx(10.0)
        finally:
            adapter.close()

    def test_no_statement_is_added_for_the_aggregates(
        self,
        request: pytest.FixtureRequest,
    ) -> None:
        """The MIN/MAX/percentile statement already runs once per column; two more
        expressions on it must not become a second roundtrip.
        """

        from tests.adapters.test_dialect_guard import _install_recorder

        con = request.getfixturevalue("duckdb_native_connection")
        con.execute("CREATE TABLE lone (n INTEGER)")
        values = ", ".join(f"({b})" for b in _rows(self._COUNTS))
        con.execute(f"INSERT INTO lone (n) VALUES {values}")
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
        adapter.connect()
        recorder = _install_recorder(adapter)

        try:
            table = next(iter(adapter.list_tables(include=["*.lone"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]

            assert stats["n"].mean is not None
            aggregate_fetches = [s for s in recorder.statements if "AVG(" in s and "SUM(" in s]
            assert len(aggregate_fetches) == 1
        finally:
            adapter.close()


class TestExactIntegerTotals:
    """`sum`/`mean`/`range` over an integral total stay exact above float64's 2**53 boundary -
    proven on Postgres, where `SUM(bigint)` returns `numeric`, decoded by the driver as `Decimal`.
    """

    def test_a_bigint_total_above_2_53_publishes_exact(
        self,
        request: pytest.FixtureRequest,
    ) -> None:
        import psycopg

        creds = request.getfixturevalue("postgres_test_db")
        # Five DISTINCT values (cardinality must clear `enumeration_threshold`, or the column
        # classifies categorical and carries no `sum`) summing past 2**53, where float64 rounds.
        base = 2_000_000_000_000_000
        values = [base + i for i in range(5)]
        exact_total = sum(values)

        with psycopg.connect(
            host=creds["host"],
            port=int(creds["port"]),
            dbname=creds["database"],
            user=creds["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.big (n bigint)")
            rows = ", ".join(f"({v})" for v in values)
            conn.execute(cast(LiteralString, f"INSERT INTO public.big (n) VALUES {rows}"))

        adapter = PostgresAdapter(creds)
        adapter.connect()

        try:
            table = next(iter(adapter.list_tables(include=["*.big"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]
        finally:
            adapter.close()

        assert stats["n"].sum == exact_total
        assert isinstance(stats["n"].sum, int)


class TestDegenerateCensus:
    """`zero_count`/`negative_count`/`empty_count` (SPEC 2.2.x): a census, not a judgement."""

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_numeric_zero_count_matches_an_independent_computation(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        """Bucket 0 of `SHAPES["dominant"]` is the literal value 0 - its count (96) is
        known from the fixture definition, not re-derived from the adapter's own arithmetic.
        """

        adapter = _adapter(vendor, request, SHAPES["dominant"])

        try:
            stats = _profile(adapter)["n"]
            assert stats.zero_count == 96
        finally:
            adapter.close()

    def test_negative_and_empty_counts_match_an_independent_computation(
        self,
        request: pytest.FixtureRequest,
    ) -> None:
        """DuckDB-only: the shared bucket harness has no negative value and no text column."""

        con = request.getfixturevalue("duckdb_native_connection")
        con.execute("CREATE TABLE census (amount INTEGER, note VARCHAR)")
        rows = (
            [(-1, "")] * 6
            + [(-2, "a")] * 2
            + [(3, "")] * 2
            + [(i, f"note-{i}") for i in range(4, 4 + 40)]
        )
        values = ", ".join(f"({n}, '{t}')" for n, t in rows)
        con.execute(f"INSERT INTO census (amount, note) VALUES {values}")
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
        adapter.connect()

        try:
            table = next(iter(adapter.list_tables(include=["*.census"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]

            assert stats["amount"].negative_count == 8
            assert stats["amount"].zero_count == 0
            assert stats["note"].empty_count == 8
        finally:
            adapter.close()

    def test_no_statement_is_added_for_the_census(
        self,
        request: pytest.FixtureRequest,
    ) -> None:
        """The batched Phase A statement already runs once per table; the census rides it."""

        from tests.adapters.test_dialect_guard import _install_recorder

        con = request.getfixturevalue("duckdb_native_connection")
        con.execute("CREATE TABLE census (amount INTEGER, note VARCHAR)")
        con.execute("INSERT INTO census (amount, note) VALUES (-1, ''), (0, 'a'), (2, 'b')")
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
        adapter.connect()
        recorder = _install_recorder(adapter)

        try:
            table = next(iter(adapter.list_tables(include=["*.census"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())

            phase_a_fetches = [s for s in recorder.statements if "COUNT_IF" in s and "note" in s]
            assert len(phase_a_fetches) == 1
        finally:
            adapter.close()


# Three of five whole, three of five midnight - independently counted, not re-derived from
# the adapter's own truncation expression. Cardinality (5) clears CONFIG's threshold (3).
_QUANTIZED_NUMERIC_VALUES = ("1", "2", "3.5", "4.25", "5")
_QUANTIZED_TEMPORAL_VALUES = (
    "2026-01-01 00:00:00",
    "2026-01-02 00:00:00",
    "2026-01-03 12:00:00",
    "2026-01-04 08:30:00",
    "2026-01-05 00:00:00",
)


def _seed_quantized_postgres(creds: dict[str, str]) -> None:
    import psycopg

    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password="",
        autocommit=True,
    ) as conn:
        conn.execute("CREATE TABLE public.quantized (n numeric, t timestamp)")
        rows = ", ".join(
            f"({n}, '{t}')" for n, t in zip(_QUANTIZED_NUMERIC_VALUES, _QUANTIZED_TEMPORAL_VALUES)
        )
        conn.execute(f"INSERT INTO public.quantized (n, t) VALUES {rows}")


def _seed_quantized_mysql(creds: dict[str, str]) -> None:
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
        cursor.execute("CREATE TABLE quantized (n DECIMAL(10,2), t DATETIME)")
        rows = ", ".join(
            f"({n}, '{t}')" for n, t in zip(_QUANTIZED_NUMERIC_VALUES, _QUANTIZED_TEMPORAL_VALUES)
        )
        cursor.execute(f"INSERT INTO quantized (n, t) VALUES {rows}")
        cursor.close()
    finally:
        conn.close()


def _seed_quantized_snowflake(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE TABLE quantized (n NUMERIC, t TIMESTAMP)")
    rows = ", ".join(
        f"({n}, '{t}')" for n, t in zip(_QUANTIZED_NUMERIC_VALUES, _QUANTIZED_TEMPORAL_VALUES)
    )
    con.execute(f"INSERT INTO quantized (n, t) VALUES {rows}")


def _seed_quantized_duckdb(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE TABLE quantized (n NUMERIC, t TIMESTAMP)")
    rows = ", ".join(
        f"({n}, '{t}')" for n, t in zip(_QUANTIZED_NUMERIC_VALUES, _QUANTIZED_TEMPORAL_VALUES)
    )
    con.execute(f"INSERT INTO quantized (n, t) VALUES {rows}")


def _seed_quantized_clickhouse(cur: Any) -> None:
    cur.execute(
        "CREATE TABLE seedbank.quantized (n Decimal(10,2), t DateTime64(0)) ENGINE = Memory",
    )
    rows = ", ".join(
        f"({n}, '{t}')" for n, t in zip(_QUANTIZED_NUMERIC_VALUES, _QUANTIZED_TEMPORAL_VALUES)
    )
    cur.execute(f"INSERT INTO seedbank.quantized (n, t) VALUES {rows}")


def _seed_quantized_redshift(shim: Any) -> None:
    shim.execute("CREATE TABLE public.quantized (n numeric, t timestamp)")
    rows = ", ".join(
        f"({n}, '{t}')" for n, t in zip(_QUANTIZED_NUMERIC_VALUES, _QUANTIZED_TEMPORAL_VALUES)
    )
    shim.execute(f"INSERT INTO public.quantized (n, t) VALUES {rows}")


def _seed_quantized_databricks(cursor: Any) -> None:
    cursor.execute("CREATE TABLE quantized (n DECIMAL(10,2), t TIMESTAMP_NTZ) USING DELTA")
    rows = ", ".join(
        f"({n}, TIMESTAMP '{t}')"
        for n, t in zip(_QUANTIZED_NUMERIC_VALUES, _QUANTIZED_TEMPORAL_VALUES)
    )
    cursor.execute(f"INSERT INTO quantized (n, t) VALUES {rows}")


def _seed_quantized_bigquery(cursor: Any, dataset: str) -> None:
    q = f"`{dataset}`"
    cursor.execute(f"CREATE TABLE {q}.quantized (n NUMERIC, t DATETIME)")
    rows = ", ".join(
        f"({n}, DATETIME '{t}')"
        for n, t in zip(_QUANTIZED_NUMERIC_VALUES, _QUANTIZED_TEMPORAL_VALUES)
    )
    cursor.execute(f"INSERT INTO {q}.quantized (n, t) VALUES {rows}")


def _quantized_adapter(vendor: str, request: pytest.FixtureRequest) -> Adapter:
    """A connected adapter over a fresh `quantized(n, t)` table, seeded per vendor."""

    if vendor == "postgres":
        creds = request.getfixturevalue("postgres_test_db")
        _seed_quantized_postgres(creds)
        adapter: Adapter = PostgresAdapter(creds)
    elif vendor == "mysql":
        creds = request.getfixturevalue("mysql_test_db")
        _seed_quantized_mysql(creds)
        adapter = MysqlAdapter(creds)
    elif vendor == "snowflake":
        con = request.getfixturevalue("snowflake_duckdb_connection")
        _seed_quantized_snowflake(con)
        sf_creds = {
            "account": "test-account",
            "user": "test-user",
            "password": "test-password",
            "warehouse": "test-warehouse",
            "database": "memory",
            "role": "test-role",
        }
        adapter = SnowflakeAdapter(sf_creds, cursor_factory=lambda _params: con)
    elif vendor == "duckdb":
        con = request.getfixturevalue("duckdb_native_connection")
        _seed_quantized_duckdb(con)
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
    elif vendor == "clickhouse":
        cur = request.getfixturevalue("clickhouse_native_connection")
        _seed_quantized_clickhouse(cur)
        adapter = ClickhouseAdapter(
            {"host": "chdb", "database": "seedbank"},
            cursor_factory=lambda _params: cur,
        )
    elif vendor == "redshift":
        shim = request.getfixturevalue("redshift_postgres_connection")
        _seed_quantized_redshift(shim)
        adapter = RedshiftAdapter(
            {"host": "redshift", "database": "seedbank", "user": "test", "password": "test"},
            cursor_factory=lambda _params: shim,
        )
    elif vendor == "databricks":
        cursor = request.getfixturevalue("databricks_test_schema")
        _seed_quantized_databricks(cursor)
        adapter = DatabricksAdapter(_DATABRICKS_CREDS, cursor_factory=lambda _params: cursor)
    elif vendor == "bigquery":
        bq_cursor, dataset = request.getfixturevalue("bigquery_test_dataset")
        _seed_quantized_bigquery(bq_cursor, dataset)
        adapter = BigqueryAdapter(
            {"project": _BIGQUERY_PROJECT, "dataset": dataset},
            cursor_factory=lambda _params: bq_cursor,
        )
    else:
        raise ValueError(f"unknown vendor: {vendor!r}")

    adapter.connect()

    return adapter


class TestQuantizedCount:
    """`quantized_count` (SPEC 2.2.3/2.2.4): a date-in-a-timestamp or a whole-number-in-a-decimal
    census, on the same batched read every other type-driven Phase A/B field already rides.
    """

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_numeric_matches_an_independent_computation(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        adapter = _quantized_adapter(vendor, request)

        try:
            table = next(iter(adapter.list_tables(include=["*.quantized"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]["n"]

            assert stats.quantized_count == 3, stats.quantized_count
        finally:
            adapter.close()

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_temporal_matches_an_independent_computation(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        adapter = _quantized_adapter(vendor, request)

        try:
            table = next(iter(adapter.list_tables(include=["*.quantized"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]["t"]

            assert stats.quantized_count == 3, stats.quantized_count
        finally:
            adapter.close()

    def test_a_native_date_column_carries_no_field(self, request: pytest.FixtureRequest) -> None:
        """DATE is always its own day-truncation (SPEC 2.2.3): a count would be a truism."""

        con = request.getfixturevalue("duckdb_native_connection")
        con.execute("CREATE TABLE dates (d DATE)")
        con.execute(
            "INSERT INTO dates (d) VALUES ('2026-01-01'), ('2026-01-02'), ('2026-01-03'), "
            "('2026-01-04'), ('2026-01-05')",
        )
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
        adapter.connect()

        try:
            table = next(iter(adapter.list_tables(include=["*.dates"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]["d"]

            assert stats.quantized_count is None
        finally:
            adapter.close()

    def test_a_native_time_column_carries_no_field(self, request: pytest.FixtureRequest) -> None:
        """TIME carries no date at all (SPEC 2.2.3): there is nothing to truncate to."""

        con = request.getfixturevalue("duckdb_native_connection")
        con.execute("CREATE TABLE clocks (c TIME)")
        con.execute(
            "INSERT INTO clocks (c) VALUES ('00:00:00'), ('08:30:00'), ('12:00:00'), "
            "('16:45:00'), ('23:59:59')",
        )
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
        adapter.connect()

        try:
            table = next(iter(adapter.list_tables(include=["*.clocks"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]["c"]

            assert stats.quantized_count is None
        finally:
            adapter.close()

    def test_no_statement_is_added_for_the_numeric_census(
        self,
        request: pytest.FixtureRequest,
    ) -> None:
        """The batched Phase A statement already runs once per table; the census rides it."""

        from tests.adapters.test_dialect_guard import _install_recorder

        con = request.getfixturevalue("duckdb_native_connection")
        _seed_quantized_duckdb(con)
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
        adapter.connect()
        recorder = _install_recorder(adapter)

        try:
            table = next(iter(adapter.list_tables(include=["*.quantized"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())

            phase_a_fetches = [s for s in recorder.statements if "TRUNC(" in s]
            assert len(phase_a_fetches) == 1
        finally:
            adapter.close()


# Five distinct string lengths, one row each: min/max/avg/p95 are hand-computed, not
# re-derived from the adapter's own rounding. Cardinality (5) clears CONFIG's threshold (3).
# The 5-character entry is multi-byte (7 UTF-8 bytes), so a byte-length function disagrees with
# a character-length one: every adapter MUST measure 5, the character count (SPEC 2.2.4).
_LENGTH_VALUES = ("a", "bb", "ccc", "dddd", "Grüße")


def _seed_length_postgres(creds: dict[str, str]) -> None:
    import psycopg

    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password="",
        autocommit=True,
    ) as conn:
        conn.execute("CREATE TABLE public.lengths (s text)")
        values = ", ".join(f"('{v}')" for v in _LENGTH_VALUES)
        conn.execute(f"INSERT INTO public.lengths (s) VALUES {values}")


def _seed_length_mysql(creds: dict[str, str]) -> None:
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
        cursor.execute("CREATE TABLE lengths (s VARCHAR(16))")
        values = ", ".join(f"('{v}')" for v in _LENGTH_VALUES)
        cursor.execute(f"INSERT INTO lengths (s) VALUES {values}")
        cursor.close()
    finally:
        conn.close()


def _seed_length_snowflake(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE TABLE lengths (s VARCHAR)")
    values = ", ".join(f"('{v}')" for v in _LENGTH_VALUES)
    con.execute(f"INSERT INTO lengths (s) VALUES {values}")


def _seed_length_duckdb(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE TABLE lengths (s VARCHAR)")
    values = ", ".join(f"('{v}')" for v in _LENGTH_VALUES)
    con.execute(f"INSERT INTO lengths (s) VALUES {values}")


def _seed_length_clickhouse(cur: Any) -> None:
    cur.execute("CREATE TABLE seedbank.lengths (s String) ENGINE = Memory")
    values = ", ".join(f"('{v}')" for v in _LENGTH_VALUES)
    cur.execute(f"INSERT INTO seedbank.lengths (s) VALUES {values}")


def _seed_length_redshift(shim: Any) -> None:
    shim.execute("CREATE TABLE public.lengths (s varchar(16))")
    values = ", ".join(f"('{v}')" for v in _LENGTH_VALUES)
    shim.execute(f"INSERT INTO public.lengths (s) VALUES {values}")


def _seed_length_databricks(cursor: Any) -> None:
    cursor.execute("CREATE TABLE lengths (s STRING) USING DELTA")
    values = ", ".join(f"('{v}')" for v in _LENGTH_VALUES)
    cursor.execute(f"INSERT INTO lengths (s) VALUES {values}")


def _seed_length_bigquery(cursor: Any, dataset: str) -> None:
    q = f"`{dataset}`"
    cursor.execute(f"CREATE TABLE {q}.lengths (s STRING)")
    values = ", ".join(f"('{v}')" for v in _LENGTH_VALUES)
    cursor.execute(f"INSERT INTO {q}.lengths (s) VALUES {values}")


def _length_adapter(vendor: str, request: pytest.FixtureRequest) -> Adapter:
    """A connected adapter over a fresh `lengths(s)` table, seeded per vendor."""

    if vendor == "postgres":
        creds = request.getfixturevalue("postgres_test_db")
        _seed_length_postgres(creds)
        adapter: Adapter = PostgresAdapter(creds)
    elif vendor == "mysql":
        creds = request.getfixturevalue("mysql_test_db")
        _seed_length_mysql(creds)
        adapter = MysqlAdapter(creds)
    elif vendor == "snowflake":
        con = request.getfixturevalue("snowflake_duckdb_connection")
        _seed_length_snowflake(con)
        sf_creds = {
            "account": "test-account",
            "user": "test-user",
            "password": "test-password",
            "warehouse": "test-warehouse",
            "database": "memory",
            "role": "test-role",
        }
        adapter = SnowflakeAdapter(sf_creds, cursor_factory=lambda _params: con)
    elif vendor == "duckdb":
        con = request.getfixturevalue("duckdb_native_connection")
        _seed_length_duckdb(con)
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
    elif vendor == "clickhouse":
        cur = request.getfixturevalue("clickhouse_native_connection")
        _seed_length_clickhouse(cur)
        adapter = ClickhouseAdapter(
            {"host": "chdb", "database": "seedbank"},
            cursor_factory=lambda _params: cur,
        )
    elif vendor == "redshift":
        shim = request.getfixturevalue("redshift_postgres_connection")
        _seed_length_redshift(shim)
        adapter = RedshiftAdapter(
            {"host": "redshift", "database": "seedbank", "user": "test", "password": "test"},
            cursor_factory=lambda _params: shim,
        )
    elif vendor == "databricks":
        cursor = request.getfixturevalue("databricks_test_schema")
        _seed_length_databricks(cursor)
        adapter = DatabricksAdapter(_DATABRICKS_CREDS, cursor_factory=lambda _params: cursor)
    elif vendor == "bigquery":
        bq_cursor, dataset = request.getfixturevalue("bigquery_test_dataset")
        _seed_length_bigquery(bq_cursor, dataset)
        adapter = BigqueryAdapter(
            {"project": _BIGQUERY_PROJECT, "dataset": dataset},
            cursor_factory=lambda _params: bq_cursor,
        )
    else:
        raise ValueError(f"unknown vendor: {vendor!r}")

    adapter.connect()

    return adapter


class TestLength:
    """`length` (SPEC 2.2.4): min/max/avg ride the batched statement, while MySQL's and
    BigQuery's p95 take their own and do not interpolate - see the vendor branch below.
    """

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_min_max_avg_p95_match_an_independent_computation(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        adapter = _length_adapter(vendor, request)

        try:
            table = next(iter(adapter.list_tables(include=["*.lengths"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]["s"]
            assert stats.length is not None
            assert stats.length.min == 1
            assert stats.length.max == 5
            assert stats.length.avg == pytest.approx(3.0)

            if vendor in ("mysql", "bigquery"):
                # Neither interpolates: MySQL ranks CEIL(0.95 * 5) and BigQuery's
                # `APPROX_QUANTILES` returns the same boundary (measured on this five-element set).
                assert stats.length.p95 == pytest.approx(5.0)
            else:
                # PERCENTILE_CONT interpolates: index 0.95 * (5 - 1) = 3.8 -> 4 + 0.8 * (5 - 4).
                # The same function every other adapter's own numeric percentiles already use.
                assert stats.length.p95 == pytest.approx(4.8)
        finally:
            adapter.close()

    def test_no_statement_is_added_outside_mysql(self, request: pytest.FixtureRequest) -> None:
        """Postgres/Snowflake/duckdb add expressions to the batched statement; MySQL alone
        issues one more, since it has no ordered-set aggregate for a percentile (SPEC 2.2.4).
        """

        from tests.adapters.test_dialect_guard import _install_recorder

        con = request.getfixturevalue("duckdb_native_connection")
        _seed_length_duckdb(con)
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
        adapter.connect()
        recorder = _install_recorder(adapter)

        try:
            table = next(iter(adapter.list_tables(include=["*.lengths"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]

            assert stats["s"].length is not None
            length_fetches = [s for s in recorder.statements if "LENGTH(" in s.upper()]
            assert len(length_fetches) == 1
        finally:
            adapter.close()

    def test_a_numeric_typed_column_carries_no_length(
        self,
        request: pytest.FixtureRequest,
    ) -> None:
        """`length` is gated by type alone in Phase A, ahead of classification - an integer
        column MUST NOT carry it, whatever its cardinality resolves to (SPEC 3.2 priority 4).
        """

        con = request.getfixturevalue("duckdb_native_connection")
        con.execute("CREATE TABLE bucketed (n INTEGER)")
        con.execute("INSERT INTO bucketed (n) VALUES (1), (1), (2)")
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
        adapter.connect()

        try:
            table = next(iter(adapter.list_tables(include=["*.bucketed"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]

            assert stats["n"].cardinality == 2
            assert stats["n"].length is None
        finally:
            adapter.close()


_FOLDED_VALUES = ("Alice", "alice", "ALICE ", "Bob", "carol")
_FOLDED_CARDINALITY = 5  # every raw spelling is distinct
_FOLDED_NORMALIZED_CARDINALITY = 3  # trim + fold: alice, bob, carol


def _seed_folded_postgres(creds: dict[str, str]) -> None:
    import psycopg

    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password="",
        autocommit=True,
    ) as conn:
        conn.execute("CREATE TABLE public.folded (s VARCHAR(16))")
        values = ", ".join(f"('{v}')" for v in _FOLDED_VALUES)
        conn.execute(f"INSERT INTO public.folded (s) VALUES {values}")


def _seed_folded_mysql(creds: dict[str, str]) -> None:
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
        cursor.execute("CREATE TABLE folded (s VARCHAR(16))")
        values = ", ".join(f"('{v}')" for v in _FOLDED_VALUES)
        cursor.execute(f"INSERT INTO folded (s) VALUES {values}")
        cursor.close()
    finally:
        conn.close()


def _seed_folded_snowflake(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE TABLE folded (s VARCHAR)")
    values = ", ".join(f"('{v}')" for v in _FOLDED_VALUES)
    con.execute(f"INSERT INTO folded (s) VALUES {values}")


def _seed_folded_duckdb(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE TABLE folded (s VARCHAR)")
    values = ", ".join(f"('{v}')" for v in _FOLDED_VALUES)
    con.execute(f"INSERT INTO folded (s) VALUES {values}")


def _seed_folded_clickhouse(cur: Any) -> None:
    cur.execute("CREATE TABLE seedbank.folded (s String) ENGINE = Memory")
    values = ", ".join(f"('{v}')" for v in _FOLDED_VALUES)
    cur.execute(f"INSERT INTO seedbank.folded (s) VALUES {values}")


def _seed_folded_redshift(shim: Any) -> None:
    shim.execute("CREATE TABLE public.folded (s varchar(16))")
    values = ", ".join(f"('{v}')" for v in _FOLDED_VALUES)
    shim.execute(f"INSERT INTO public.folded (s) VALUES {values}")


def _seed_folded_databricks(cursor: Any) -> None:
    cursor.execute("CREATE TABLE folded (s STRING) USING DELTA")
    values = ", ".join(f"('{v}')" for v in _FOLDED_VALUES)
    cursor.execute(f"INSERT INTO folded (s) VALUES {values}")


def _seed_folded_bigquery(cursor: Any, dataset: str) -> None:
    q = f"`{dataset}`"
    cursor.execute(f"CREATE TABLE {q}.folded (s STRING)")
    values = ", ".join(f"('{v}')" for v in _FOLDED_VALUES)
    cursor.execute(f"INSERT INTO {q}.folded (s) VALUES {values}")


def _folded_adapter(vendor: str, request: pytest.FixtureRequest) -> Adapter:
    """A connected adapter over a fresh `folded(s)` table, seeded per vendor."""

    if vendor == "postgres":
        creds = request.getfixturevalue("postgres_test_db")
        _seed_folded_postgres(creds)
        adapter: Adapter = PostgresAdapter(creds)
    elif vendor == "mysql":
        creds = request.getfixturevalue("mysql_test_db")
        _seed_folded_mysql(creds)
        adapter = MysqlAdapter(creds)
    elif vendor == "snowflake":
        con = request.getfixturevalue("snowflake_duckdb_connection")
        _seed_folded_snowflake(con)
        sf_creds = {
            "account": "test-account",
            "user": "test-user",
            "password": "test-password",
            "warehouse": "test-warehouse",
            "database": "memory",
            "role": "test-role",
        }
        adapter = SnowflakeAdapter(sf_creds, cursor_factory=lambda _params: con)
    elif vendor == "duckdb":
        con = request.getfixturevalue("duckdb_native_connection")
        _seed_folded_duckdb(con)
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
    elif vendor == "clickhouse":
        cur = request.getfixturevalue("clickhouse_native_connection")
        _seed_folded_clickhouse(cur)
        adapter = ClickhouseAdapter(
            {"host": "chdb", "database": "seedbank"},
            cursor_factory=lambda _params: cur,
        )
    elif vendor == "redshift":
        shim = request.getfixturevalue("redshift_postgres_connection")
        _seed_folded_redshift(shim)
        adapter = RedshiftAdapter(
            {"host": "redshift", "database": "seedbank", "user": "test", "password": "test"},
            cursor_factory=lambda _params: shim,
        )
    elif vendor == "databricks":
        cursor = request.getfixturevalue("databricks_test_schema")
        _seed_folded_databricks(cursor)
        adapter = DatabricksAdapter(_DATABRICKS_CREDS, cursor_factory=lambda _params: cursor)
    elif vendor == "bigquery":
        bq_cursor, dataset = request.getfixturevalue("bigquery_test_dataset")
        _seed_folded_bigquery(bq_cursor, dataset)
        adapter = BigqueryAdapter(
            {"project": _BIGQUERY_PROJECT, "dataset": dataset},
            cursor_factory=lambda _params: bq_cursor,
        )
    else:
        raise ValueError(f"unknown vendor: {vendor!r}")

    adapter.connect()

    return adapter


class TestNormalizedCardinality:
    """`normalized_cardinality` (SPEC 2.2.4): trim + case-fold, called directly - the
    orchestrator gates it to the join-key population, so it is not part of Phase A/B.
    """

    @pytest.mark.parametrize("vendor", VENDORS)
    def test_merges_case_and_whitespace_variants(
        self,
        request: pytest.FixtureRequest,
        vendor: str,
    ) -> None:
        adapter = _folded_adapter(vendor, request)

        try:
            table = next(iter(adapter.list_tables(include=["*.folded"], exclude=[])))
            columns = adapter.introspect_columns(table.fqn)
            stats = adapter.compute_statistics(table.fqn, columns, CONFIG, frozenset())[1]["s"]
            normalized = adapter.compute_normalized_cardinality(table.fqn, "s")

            if vendor == "mysql":
                # MariaDB's default collation is case-insensitive and PAD SPACE, so `cardinality`
                # already merges every variant (SPEC 2.2.2) and folding again changes nothing.
                assert stats.cardinality == _FOLDED_NORMALIZED_CARDINALITY
                assert normalized == _FOLDED_NORMALIZED_CARDINALITY
            else:
                assert stats.cardinality == _FOLDED_CARDINALITY
                assert normalized == _FOLDED_NORMALIZED_CARDINALITY
        finally:
            adapter.close()

    def test_a_clean_column_reports_equal_counts(self, request: pytest.FixtureRequest) -> None:
        """A column already normalized reads as portable: the two counts agree exactly."""

        con = request.getfixturevalue("duckdb_native_connection")
        con.execute("CREATE TABLE clean (s VARCHAR)")
        con.execute("INSERT INTO clean (s) VALUES ('usa'), ('mex'), ('can')")
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
        adapter.connect()

        try:
            table = next(iter(adapter.list_tables(include=["*.clean"], exclude=[])))
            normalized = adapter.compute_normalized_cardinality(table.fqn, "s")
            assert normalized == 3
        finally:
            adapter.close()
