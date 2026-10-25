"""SPEC 2.2.4/2.2.16: day truncation and day counts never move with the session or server zone."""

from __future__ import annotations

from typing import Any, LiteralString, cast

import pytest

from dbprint.adapters import StatisticsConfig
from tests.adapters.conftest import _adapter_factory_for, _mysql_exec_many
from tests.conftest import pg_connect


_ZONE = "America/New_York"
_VENDORS = ["duckdb", "postgres", "redshift", "snowflake", "mysql", "clickhouse", "databricks"]
# Six-hourly instants from 2026-01-01T00:00Z: four UTC midnights, none a New York midnight.
_INSTANTS = [f"2026-01-0{1 + h // 24} {h % 24:02d}:00:00" for h in range(0, 96, 6)]
_DAY_BUCKETS = (
    ("2026-01-01T00:00:00Z", 4),
    ("2026-01-02T00:00:00Z", 4),
    ("2026-01-03T00:00:00Z", 4),
    ("2026-01-04T00:00:00Z", 4),
)
# A naive span across New York's 2026-03-08 daylight-saving change: 30 days of wall clock.
_SPAN = ("2026-03-01 00:00:00", "2026-03-31 00:00:00")
# The same two New York midnights as instants: 29 days 23 hours elapse between them.
_INSTANT_SPAN = ("2026-03-01 05:00:00", "2026-03-31 04:00:00")

_TYPES = {
    "duckdb": ("TIMESTAMPTZ", "TIMESTAMP", "DATE"),
    "snowflake": ("TIMESTAMPTZ", "TIMESTAMP", "DATE"),
    "postgres": ("timestamptz", "timestamp", "date"),
    "redshift": ("timestamptz", "timestamp", "date"),
    "mysql": ("TIMESTAMP NULL", "DATETIME", "DATE"),
    "clickhouse": ("Nullable(DateTime64(6))", "Nullable(DateTime64(6))", "Nullable(Date)"),
    "databricks": ("TIMESTAMP", "TIMESTAMP_NTZ", "DATE"),
}


@pytest.mark.parametrize("vendor", _VENDORS)
def test_a_non_utc_session_cuts_and_counts_days_as_a_utc_one(
    vendor: str,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGTZ", _ZONE)
    _seed(vendor, request)
    adapter = _adapter_factory_for(request, vendor)()
    _set_session_zone(vendor, adapter, request)
    fqn = next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if t.fqn.endswith(".clock")
    )
    columns = adapter.introspect_columns(fqn)
    counts, stats = adapter.compute_statistics(
        fqn,
        columns,
        StatisticsConfig(enumeration_threshold=0),
        frozenset(),
    )
    buckets = adapter.probe_timeline(fqn, columns, counts, "at_", "day")
    weeks = adapter.probe_timeline(fqn, columns, counts, "at_", "week")
    months = adapter.probe_timeline(fqn, columns, counts, "at_", "month")
    adapter.close()

    if vendor != "snowflake":  # The duckdb shim holds no TIMESTAMP_LTZ, only an own-offset type.
        assert stats.stats["at_"].quantized_count == 4

    # MySQL publishes a TIMESTAMP with no `Z` (SPEC 2.2.14).
    z = "" if vendor == "mysql" else "Z"

    assert buckets == tuple((start.removesuffix("Z") + z, n) for start, n in _DAY_BUCKETS)
    assert weeks == ((f"2025-12-29T00:00:00{z}", 16),)
    assert months == ((f"2026-01-01T00:00:00{z}", 16),)
    assert stats.stats["dst_at"].range is not None
    assert stats.stats["dst_at"].range.span_days == 29
    assert stats.stats["dst_day"].range is not None
    assert stats.stats["dst_day"].range.span_days == 30

    if vendor != "clickhouse":  # ClickHouse holds no zoneless timestamp.
        assert stats.stats["dst_naive"].range is not None
        assert stats.stats["dst_naive"].range.span_days == 30


def _set_session_zone(vendor: str, adapter: Any, request: pytest.FixtureRequest) -> None:
    # MySQL is absent: its adapter pins UTC, and its substrate server runs in New York.
    if vendor == "duckdb":
        request.getfixturevalue("duckdb_native_connection").execute(f"SET TimeZone = '{_ZONE}'")
    elif vendor == "snowflake":
        request.getfixturevalue("snowflake_duckdb_connection").execute(
            f"SET TimeZone = '{_ZONE}'",
        )
    elif vendor == "redshift":
        request.getfixturevalue("redshift_postgres_connection").execute(
            f"SET TIME ZONE '{_ZONE}'",
        )
    elif vendor == "clickhouse":
        request.getfixturevalue("clickhouse_native_connection").execute(
            f"SET session_timezone = '{_ZONE}'",
        )


def _rows(vendor: str) -> list[str]:
    tz, naive, day = _TYPES[vendor]

    def lit(text: str, kind: str) -> str:
        if vendor == "clickhouse" and kind != day:
            return f"toDateTime64('{text}', 6, 'UTC')"

        if vendor == "databricks" and kind == tz:
            return f"CAST('{text.replace(' ', 'T')}Z' AS TIMESTAMP)"

        if vendor in ("duckdb", "snowflake", "postgres", "redshift") and kind == tz:
            return f"CAST('{text}+00' AS {kind})"

        if vendor == "mysql" and kind == tz:
            return f"TIMESTAMP '{text}'"

        return f"CAST('{text}' AS {kind})"

    span = [
        (lit(_SPAN[0], naive), lit(_SPAN[0][:10], day), lit(_INSTANT_SPAN[0], tz)),
        (lit(_SPAN[1], naive), lit(_SPAN[1][:10], day), lit(_INSTANT_SPAN[1], tz)),
    ]

    return [f"({lit(at, tz)}, {', '.join(span[i % 2])})" for i, at in enumerate(_INSTANTS)]


def _ddl(vendor: str, table: str, suffix: str = "") -> list[str]:
    tz, naive, day = _TYPES[vendor]

    return [
        f"CREATE TABLE {table} (at_ {tz}, dst_naive {naive}, dst_day {day}, dst_at {tz}){suffix}",
        f"INSERT INTO {table} VALUES {', '.join(_rows(vendor))}",
    ]


def _seed(vendor: str, request: pytest.FixtureRequest) -> None:
    if vendor in ("duckdb", "snowflake"):
        fixture = (
            "duckdb_native_connection" if vendor == "duckdb" else "snowflake_duckdb_connection"
        )
        con = request.getfixturevalue(fixture)

        for statement in _ddl(vendor, "seedbank.clock"):
            con.execute(statement)
    elif vendor == "postgres":
        with pg_connect(request.getfixturevalue("postgres_test_db")) as conn:
            for statement in [*_ddl(vendor, "seedbank.clock"), "ANALYZE"]:
                conn.execute(cast(LiteralString, statement))
    elif vendor == "redshift":
        shim = request.getfixturevalue("redshift_postgres_connection")

        for statement in [*_ddl(vendor, "seedbank.clock"), "ANALYZE"]:
            shim.execute(statement)
    elif vendor == "mysql":
        creds = request.getfixturevalue("mysql_test_db")
        statements = ["SET time_zone = '+00:00'", *_ddl(vendor, "clock"), "ANALYZE TABLE clock"]
        _mysql_exec_many(int(creds["port"]), creds["database"], statements)
    elif vendor == "clickhouse":
        cursor = request.getfixturevalue("clickhouse_native_connection")

        for statement in _ddl(vendor, "seedbank.clock", " ENGINE = Memory"):
            cursor.execute(statement)
    else:
        cursor = request.getfixturevalue("databricks_test_schema")

        for statement in _ddl(vendor, "clock", " USING DELTA"):
            cursor.execute(statement)


class TestAClickhouseDeclaredZone:
    """A `DateTime('zone')` is cut into days in that zone and published as UTC instants."""

    def test_its_own_midnights_count_and_bucket_at_their_utc_instant(
        self,
        request: pytest.FixtureRequest,
    ) -> None:
        stats, days = _clickhouse_clock(
            request,
            "DateTime('Asia/Kolkata')",
            [
                f"toDateTime('2026-01-0{d} {h:02d}:00:00', 'Asia/Kolkata')"
                for d in (1, 2)
                for h in (0, 12)
            ],
        )

        assert stats.quantized_count == 2
        assert stats.range is not None
        assert (stats.range.min, stats.range.max) == (
            "2025-12-31T18:30:00Z",
            "2026-01-02T06:30:00Z",
        )
        assert days == (("2025-12-31T18:30:00Z", 2), ("2026-01-01T18:30:00Z", 2))

    def test_a_daylight_saving_change_shortens_its_day_bucket(
        self,
        request: pytest.FixtureRequest,
    ) -> None:
        _, days = _clickhouse_clock(
            request,
            "DateTime('America/New_York')",
            [f"toDateTime('2026-03-0{d} 12:00:00', 'America/New_York')" for d in (7, 8, 9)],
        )

        assert [start for start, _ in days] == [
            "2026-03-07T05:00:00Z",
            "2026-03-08T05:00:00Z",
            "2026-03-09T04:00:00Z",
        ]

    def test_a_listed_value_is_its_utc_instant(self, request: pytest.FixtureRequest) -> None:
        stats, _ = _clickhouse_clock(
            request,
            "DateTime('Asia/Kolkata')",
            ["toDateTime('2026-01-01 00:00:00', 'Asia/Kolkata')"] * 3,
            enumeration_threshold=10,
        )

        assert [v.value for v in stats.values or ()] == ["2025-12-31T18:30:00Z"]

    def test_a_sub_second_past_midnight_is_not_a_midnight(
        self,
        request: pytest.FixtureRequest,
    ) -> None:
        stats, _ = _clickhouse_clock(
            request,
            "DateTime64(6)",
            [
                "toDateTime64('2026-01-01 00:00:00.5', 6, 'UTC')",
                "toDateTime64('2026-01-02 00:00:00', 6, 'UTC')",
            ],
        )

        assert stats.quantized_count == 1


def _clickhouse_clock(
    request: pytest.FixtureRequest,
    sql_type: str,
    values: list[str],
    enumeration_threshold: int = 0,
) -> tuple[Any, tuple[tuple[str, int], ...]]:
    cursor = request.getfixturevalue("clickhouse_native_connection")
    cursor.execute(f"CREATE TABLE seedbank.zoned (at_ {sql_type}) ENGINE = Memory")
    cursor.execute(f"INSERT INTO seedbank.zoned VALUES {', '.join(f'({v})' for v in values)}")
    adapter = _adapter_factory_for(request, "clickhouse")()
    fqn = next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if t.fqn.endswith(".zoned")
    )
    columns = adapter.introspect_columns(fqn)
    counts, phase_b = adapter.compute_statistics(
        fqn,
        columns,
        StatisticsConfig(enumeration_threshold=enumeration_threshold),
        frozenset(),
    )
    days = adapter.probe_timeline(fqn, columns, counts, "at_", "day")
    adapter.close()

    return phase_b.stats["at_"], days
