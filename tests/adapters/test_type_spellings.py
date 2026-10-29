"""Every spelling an engine's catalog can report is named, and classifies as its family does.

Three live sweeps enumerate the engines' own type lists, so a new engine type reddens the suite.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import duckdb
import pytest
import yaml

from dbprint.adapters import (
    Adapter,
    AdapterType,
    ClickhouseAdapter,
    ColumnMeta,
    DuckdbAdapter,
    MockAdapter,
    PostgresAdapter,
)
from dbprint.adapters.base import pre_classify
from dbprint.cli.adapter_registry import ADAPTERS
from dbprint.config import StatisticsConfig
from dbprint.config.project import ConnectionConfig
from dbprint.engine import Engine
from dbprint.spec.classification import base_type
from tests._curator import conn_config, curator_fixture
from tests._engine_run import conformance_errors
from tests.adapters._composites import psql, snowflake
from tests.adapters._type_spellings import DUCKDB_DECLARATIONS, POSTGRES_SKIPPED


_CATALOG: dict[str, dict[str, str]] = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "type_spellings.yaml").read_text(encoding="utf-8"),
)

_CASES = [
    (vendor, spelling, expected)
    for vendor, spellings in sorted(_CATALOG.items())
    for spelling, expected in spellings.items()
]

_MEASURED_CARDINALITY = 10_000


_NANOSECOND_PAIRS_SQL = (
    "CAST(CASE WHEN i % 2 = 0 THEN '2026-03-04 06:00:00' ELSE '2026-03-04 06:00:00.0000001' END "
    "AS TIMESTAMP_NS) AS t, "
    "CAST(CASE WHEN i % 2 = 0 THEN '2026-03-04 06:00:00+00' "
    "ELSE '2026-03-04 06:00:00.0000001+00' END AS TIMESTAMPTZ_NS) AS tz, "
    "CAST(CASE WHEN i % 2 = 0 THEN '06:00:00' ELSE '06:00:00.0000001' END AS TIME_NS) AS clock"
)
_NANOSECOND_PAIRS = {
    "t": [("2026-03-04T06:00:00", 20), ("2026-03-04T06:00:00.000000100", 20)],
    "tz": [("2026-03-04T06:00:00.000000100Z", 20), ("2026-03-04T06:00:00Z", 20)],
    "clock": [("06:00:00", 20), ("06:00:00.000000100", 20)],
}


def test_every_registered_adapter_has_a_spelling_catalog() -> None:
    assert set(ADAPTERS) - set(_CATALOG) == set()


@pytest.mark.parametrize(("vendor", "spelling", "expected"), _CASES)
def test_a_catalogued_spelling_classifies_as_its_family(
    vendor: str,
    spelling: str,
    expected: str,
) -> None:
    stats = _stats_module(ADAPTERS[vendor])
    column = ColumnMeta(name="c", sql_type=spelling, nullable=True, default=None, ordinal=1)
    adapter_side = pre_classify(
        column,
        _MEASURED_CARDINALITY,
        StatisticsConfig(),
        False,
        supported=not stats._is_unsupported(column.classified_type),
    )

    assert adapter_side == expected
    assert _bare(ADAPTERS[vendor]).recognises_type(spelling)


@pytest.mark.parametrize("vendor", sorted(ADAPTERS))
def test_a_spelling_nobody_names_is_not_recognised(vendor: str) -> None:
    assert not _bare(ADAPTERS[vendor]).recognises_type("seedtag")


def test_an_unrecognised_type_is_reported_once_per_column(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = curator_fixture()
    table = fixture["public.curator"]
    columns = [replace(table.columns[0], sql_type="seedtag"), *table.columns[1:]]
    fixture["public.curator"] = replace(table, columns=columns)

    with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
        Engine(MockAdapter(fixture), conn_config(tmp_path), tmp_path).generate()

    warnings = [r.getMessage() for r in caplog.records if "is not recognised" in r.getMessage()]
    assert warnings == [
        (
            "table 'public.curator', column 'id': SQL type 'seedtag' is not recognised; "
            "classified by measurement as text"
        ),
    ]


def test_every_duckdb_system_type_is_catalogued() -> None:
    con = duckdb.connect(":memory:")
    logical = con.execute(
        "SELECT DISTINCT logical_type FROM duckdb_types() "
        "WHERE database_name = 'system' AND logical_type NOT IN ('NULL', 'TYPE')",
    ).fetchall()
    declarations = {name: DUCKDB_DECLARATIONS.get(name, name) for (name,) in logical}
    columns = ", ".join(f"c{i} {decl}" for i, decl in enumerate(declarations.values()))
    con.execute(f"CREATE TABLE sweep ({columns}, j JSON)")
    reported = [row[1] for row in con.execute("DESCRIBE sweep").fetchall()]

    assert _uncatalogued("duckdb", reported) == []


def test_every_clickhouse_type_family_is_catalogued(clickhouse_native_connection: Any) -> None:
    cursor = clickhouse_native_connection
    cursor.execute("SELECT name FROM system.data_type_families WHERE alias_to = ''")
    families = sorted(row[0] for row in cursor.fetchall())
    declarations = [
        _CLICKHOUSE_DECLARATIONS.get(name, name)
        for name in families
        if name not in _CLICKHOUSE_SKIPPED
    ]
    columns = ", ".join(f"c{i} {decl}" for i, decl in enumerate(declarations))
    cursor.execute(f"CREATE TABLE seedbank.sweep ({columns}) ENGINE = Memory")
    cursor.execute(
        "SELECT type FROM system.columns WHERE database = 'seedbank' AND table = 'sweep'",
    )
    reported = [row[0] for row in cursor.fetchall()]

    assert _uncatalogued("clickhouse", reported) == []


def test_every_postgres_catalog_base_type_is_catalogued(
    postgres_test_db: dict[str, str],
) -> None:
    with psql(postgres_test_db) as conn:
        rows = conn.execute(
            """
            SELECT pg_catalog.format_type(t.oid, NULL)
            FROM pg_type t
            WHERE t.typtype = 'b'
              AND t.typnamespace = 'pg_catalog'::regnamespace
              AND t.typarray <> 0
            """,
        ).fetchall()

    reported = [name for (name,) in rows if name not in POSTGRES_SKIPPED]

    assert _uncatalogued("postgres", reported) == []


def test_postgres_declines_what_it_cannot_compare_and_resolves_user_defined_types(
    postgres_test_db: dict[str, str],
    tmp_path: Path,
) -> None:
    with psql(postgres_test_db) as conn:
        conn.execute("CREATE DOMAIN sown_at AS timestamptz")
        conn.execute("CREATE DOMAIN weight_g AS numeric(12,2)")
        conn.execute("CREATE DOMAIN rating AS smallint")
        conn.execute("CREATE DOMAIN sown_on AS date")
        conn.execute("CREATE TYPE stage AS ENUM ('seed', 'sprout')")
        conn.execute(
            """
            CREATE TABLE public.plot (
                id integer, spot point, note xml, sown sown_at, weight weight_g,
                phase stage, grade "char", ref regclass, cell tid,
                txn xid8, keys int2vector, oids oidvector, score rating, day sown_on
            )
            """,
        )
        conn.execute(
            """
            INSERT INTO public.plot
            SELECT i, point(i, i), '<a/>'::xml,
                   '2024-01-01'::timestamptz + i * interval '1 hour', i * 1.25,
                   CASE WHEN i % 2 = 0 THEN 'seed'::stage ELSE 'sprout'::stage END,
                   chr(65 + i % 3)::"char", 'pg_class'::regclass, '(0,1)'::tid,
                   (i + 1000)::text::xid8, '1 2'::int2vector, '1 2'::oidvector,
                   i % 5, '2024-01-01'::date + i
            FROM generate_series(1, 300) i
            """,
        )

    classes = _generated_classes(PostgresAdapter(postgres_test_db), "postgres", tmp_path, "plot")

    assert classes == {
        "id": "numeric",
        "spot": "unsupported",
        "note": "unsupported",
        "sown": "temporal",
        "weight": "numeric",
        "phase": "categorical",
        "grade": "categorical",
        "ref": "categorical",
        "cell": "categorical",
        "txn": "text",
        "keys": "categorical",
        "oids": "categorical",
        "score": "categorical",
        "day": "temporal",
    }


def test_postgres_resolves_a_domain_enum_and_range_to_what_classification_reads(
    postgres_test_db: dict[str, str],
) -> None:
    with psql(postgres_test_db) as conn:
        conn.execute("CREATE DOMAIN sown_at AS timestamptz")
        conn.execute("CREATE TYPE stage AS ENUM ('seed', 'sprout')")
        conn.execute("CREATE TYPE bed_ref AS (row_no int, bay text)")
        conn.execute(
            "CREATE TABLE public.season "
            "(sown sown_at, phase stage, span tstzrange, n int, bed bed_ref)",
        )

    adapter = PostgresAdapter(postgres_test_db)
    adapter.connect()
    tables = adapter.list_tables(include=["*"], exclude=[])
    fqn = next(t.fqn for t in tables if t.fqn.endswith(".public.season"))

    columns = {c.name: (c.sql_type, c.classify_as) for c in adapter.introspect_columns(fqn)}
    adapter.close()

    assert columns == {
        "sown": ("sown_at", "timestamp with time zone"),
        "phase": ("stage", "anyenum"),
        "span": ("tstzrange", "anyrange"),
        "n": ("integer", None),
        "bed": ("bed_ref", "record"),
    }


def test_clickhouse_profiles_its_clock_and_bfloat_types_and_keeps_each_nanosecond(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.plot (id Int32, clock Time, fine Time64(3), mass BFloat16, "
        "sown DateTime64(9, 'UTC'), span IntervalDay) "
        "ENGINE = MergeTree ORDER BY id",
    )
    cursor.execute(
        "INSERT INTO seedbank.plot SELECT number, toTime(number * 7), toTime64(number * 7, 3), "
        "toBFloat16(number / 3), "
        "toDateTime64('2024-01-01 00:00:00', 9, 'UTC') + toIntervalMicrosecond(intDiv(number, 3)) "
        "+ toIntervalNanosecond(number % 3), "
        "toIntervalDay(number) FROM numbers(300)",
    )
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )

    payload = _generated(adapter, "clickhouse", tmp_path, "plot")

    assert {name: col["classification"] for name, col in payload["columns"].items()} == {
        "id": "numeric",
        "clock": "temporal",
        "fine": "temporal",
        "mass": "numeric",
        "sown": "temporal",
        "span": "text",
    }
    assert "unmeasured" not in payload["columns"]["clock"]
    assert payload["columns"]["fine"]["range"] == {
        "min": "00:00:00",
        "max": "00:34:53",
        "span_days": 0,
    }
    sown = payload["columns"]["sown"]["values"]
    assert [entry["value"] for entry in sown[:3]] == [
        "2024-01-01T00:00:00.000000001Z",
        "2024-01-01T00:00:00.000000002Z",
        "2024-01-01T00:00:00.000001001Z",
    ]
    assert {entry["count"] for entry in sown} == {1}


def test_duckdb_renders_sub_microsecond_and_wide_types_exactly(tmp_path: Path) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute(
        "CREATE TABLE plot (id INTEGER, sown TIMESTAMP_NS, planted TIMESTAMP_S, "
        "logged TIMESTAMP_MS, tick TIME_NS, big BIGNUM, wide UHUGEINT)",
    )
    con.execute(
        "INSERT INTO plot SELECT i, make_timestamp_ns(1704067200000000000 + i // 3 * 1000 + i % 3), "
        "CASE WHEN i = 0 THEN TIMESTAMP_S '9999-12-31 23:59:59' "
        "ELSE CAST(TIMESTAMP '2000-01-01' + to_seconds(i) AS TIMESTAMP_S) END, "
        "CAST(TIMESTAMP '2024-01-01 00:00:00.123' + to_milliseconds(i) AS TIMESTAMP_MS), "
        "CAST(CAST(TIME '12:00:00' + to_microseconds(i) AS VARCHAR) AS TIME_NS), "
        "CAST(CAST('1152921504606846977' AS HUGEINT) + i AS BIGNUM), CAST(i AS UHUGEINT) "
        "FROM range(300) r(i)",
    )
    con.execute("INSERT INTO plot (id, sown) VALUES (1000, make_timestamp_ns(-1))")
    con.close()

    payload = _generated(DuckdbAdapter({"database": str(database)}), "duckdb", tmp_path, "plot")
    columns = payload["columns"]

    assert {name: col["classification"] for name, col in columns.items()} == {
        "id": "numeric",
        "sown": "temporal",
        "planted": "temporal",
        "logged": "temporal",
        "tick": "temporal",
        "big": "numeric",
        "wide": "numeric",
    }
    assert columns["planted"]["range"]["max"] == "9999-12-31T23:59:59"
    assert "unmeasured" not in columns["planted"]
    assert columns["sown"]["range"]["min"] == "1969-12-31T23:59:59.999999999"
    assert [entry["value"] for entry in columns["sown"]["values"][:3]] == [
        "1969-12-31T23:59:59.999999999",
        "2024-01-01T00:00:00",
        "2024-01-01T00:00:00.000000001",
    ]
    assert columns["big"]["range"] == {"min": 1152921504606846977, "max": 1152921504606847276}


@pytest.mark.parametrize(
    ("sql_type", "micro", "zone"),
    [("TIMESTAMP_NS", "TIMESTAMP", ""), ("TIMESTAMPTZ_NS", "TIMESTAMPTZ", "Z")],
)
def test_duckdb_counts_a_nanosecond_instant_in_the_day_its_published_bound_names(
    tmp_path: Path,
    sql_type: str,
    micro: str,
    zone: str,
) -> None:
    offset = "+00" if zone else ""
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute(f"CREATE TABLE plot (sown {sql_type})")
    con.execute(
        f"INSERT INTO plot SELECT CAST({micro} '2026-03-04 00:01:00{offset}' + to_minutes(i) "
        f"AS {sql_type}) FROM range(599) r(i)",
    )
    con.execute(
        f"INSERT INTO plot VALUES (CAST('2026-03-04 00:00:00.000000600{offset}' AS {sql_type})), "
        f"(CAST('2026-03-04 23:59:59.999999999{offset}' AS {sql_type})), "
        f"(CAST('2026-03-05 00:00:00.000000400{offset}' AS {sql_type}))",
    )
    con.close()
    adapter = DuckdbAdapter({"database": str(database)})
    adapter.connect()

    try:
        fqn = next(t.fqn for t in adapter.list_tables(include=["*"], exclude=[]))
        columns = adapter.introspect_columns(fqn)
        counts, stats = adapter.compute_statistics(
            fqn,
            columns,
            StatisticsConfig(enumeration_threshold=0),
            frozenset(),
        )
        days = adapter.probe_timeline(fqn, columns, counts, "sown", "day")
    finally:
        adapter.close()

    assert stats.stats["sown"].range is not None
    assert stats.stats["sown"].range.min == f"2026-03-04T00:00:00.000000600{zone}"
    assert stats.stats["sown"].range.max == f"2026-03-05T00:00:00.000000400{zone}"
    assert stats.stats["sown"].range.span_days == 0
    assert days == ((f"2026-03-04T00:00:00{zone}", 601), (f"2026-03-05T00:00:00{zone}", 1))


def test_duckdb_publishes_a_timestamptz_ns_column_as_its_microsecond_sibling(
    tmp_path: Path,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE TABLE reading (id INTEGER, taken TIMESTAMPTZ_NS, plain TIMESTAMPTZ)")
    con.execute(
        "INSERT INTO reading SELECT i, CAST(TIMESTAMPTZ '2026-03-01 08:00:00+00' + to_hours(i) "
        "AS TIMESTAMPTZ_NS), TIMESTAMPTZ '2026-03-01 08:00:00+00' + to_hours(i) FROM range(1, 400) r(i)",
    )
    con.execute(
        "INSERT INTO reading VALUES (400, CAST('2026-03-20 06:30:00.123456789+00' AS TIMESTAMPTZ_NS), "
        "TIMESTAMPTZ '2026-03-20 06:30:00.123456+00')",
    )
    con.close()

    payload = _generated(DuckdbAdapter({"database": str(database)}), "duckdb", tmp_path, "reading")
    taken, plain = payload["columns"]["taken"], payload["columns"]["plain"]

    assert taken["classification"] == "temporal"
    assert taken["range"] == {
        "min": "2026-03-01T09:00:00Z",
        "max": "2026-03-20T06:30:00.123456789Z",
        "span_days": 18,
    }
    assert "unmeasured" not in taken
    assert "sketch" in taken

    for field in ("percentiles", "quantized_count", "freshness", "values"):
        assert taken[field] == plain[field], field


@pytest.mark.parametrize(
    ("sql_type", "micro", "offset"),
    [("TIMESTAMP_NS", "TIMESTAMP", ""), ("TIMESTAMPTZ_NS", "TIMESTAMPTZ", "+00")],
)
def test_duckdb_publishes_an_infinite_nanosecond_instant_as_its_microsecond_sibling(
    tmp_path: Path,
    sql_type: str,
    micro: str,
    offset: str,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))

    for table, column_type in (("nano", sql_type), ("micro", micro)):
        con.execute(f"CREATE TABLE {table} (sown {column_type})")
        con.execute(
            f"INSERT INTO {table} SELECT CAST({micro} '2026-03-01 08:00:00{offset}' "
            f"+ to_minutes(i * 7) AS {column_type}) FROM range(400) r(i)",
        )
        con.execute(f"INSERT INTO {table} VALUES ('-infinity'), ('infinity')")

    con.close()

    nano = _generated(DuckdbAdapter({"database": str(database)}), "duckdb", tmp_path, "nano")
    sibling_path = next((tmp_path / "prints").rglob("micro/statistics.yaml"))
    sibling = yaml.safe_load(sibling_path.read_text(encoding="utf-8"))
    sown = nano["columns"]["sown"]

    assert (sown["range"]["min"], sown["range"]["max"]) == ("-infinity", "infinity")
    assert sown["unrepresentable"] == ["min", "max"]
    assert {**sown, "sql_type": None} == {**sibling["columns"]["sown"], "sql_type": None}
    assert nano["timeline"] == sibling["timeline"]


def test_duckdb_keeps_timestamptz_ns_values_a_nanosecond_apart(tmp_path: Path) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE TABLE pulse (t TIMESTAMPTZ_NS)")
    con.execute(
        "INSERT INTO pulse SELECT CASE WHEN i % 2 = 0 "
        "THEN CAST('2026-03-04 06:00:00+00' AS TIMESTAMPTZ_NS) "
        "ELSE CAST('2026-03-04 06:00:00.0000001+00' AS TIMESTAMPTZ_NS) END FROM range(40) r(i)",
    )
    con.close()
    adapter = DuckdbAdapter({"database": str(database)})
    adapter.connect()

    try:
        fqn = next(t.fqn for t in adapter.list_tables(include=["*"], exclude=[]))
        columns = adapter.introspect_columns(fqn)
        _, stats = adapter.compute_statistics(fqn, columns, StatisticsConfig(), frozenset())
    finally:
        adapter.close()

    values = stats.stats["t"].values

    assert values is not None
    assert [(v.value, v.count) for v in values] == [
        ("2026-03-04T06:00:00.000000100Z", 20),
        ("2026-03-04T06:00:00Z", 20),
    ]


def test_duckdb_publishes_each_value_a_nanosecond_apart_and_its_own_print_passes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute(
        "CREATE TABLE pulse (t TIMESTAMP_NS, tz TIMESTAMPTZ_NS, clock TIME_NS, whole TIMESTAMP_NS, "
        "tz_whole TIMESTAMPTZ_NS, clock_whole TIME_NS)",
    )
    con.execute(
        f"INSERT INTO pulse SELECT {_NANOSECOND_PAIRS_SQL}, "
        "CAST('2026-03-04 06:00:00.5' AS TIMESTAMP_NS), "
        "CAST('2026-03-04 06:00:00.5+00' AS TIMESTAMPTZ_NS), "
        "CAST('06:00:00.5' AS TIME_NS) FROM range(40) r(i)",
    )
    con.close()

    payload = _generated(DuckdbAdapter({"database": str(database)}), "duckdb", tmp_path, "pulse")

    assert _published(payload) == {
        **_NANOSECOND_PAIRS,
        "whole": [("2026-03-04T06:00:00.500000", 40)],
        "tz_whole": [("2026-03-04T06:00:00.500000Z", 40)],
        "clock_whole": [("06:00:00.500000", 40)],
    }


def test_duckdb_publishes_a_time_ns_bound_with_every_digit(tmp_path: Path) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE TABLE shift (clock TIME_NS)")
    con.execute(
        "INSERT INTO shift SELECT CAST(TIME '00:01:00' + to_minutes(i) AS TIME_NS) "
        "FROM range(597) r(i)",
    )
    con.execute(
        "INSERT INTO shift VALUES (CAST('00:00:00.000000600' AS TIME_NS)), "
        "(CAST('23:59:59.999999999' AS TIME_NS)), (CAST('12:00:00.123456789' AS TIME_NS))",
    )
    con.close()

    clock = _generated(DuckdbAdapter({"database": str(database)}), "duckdb", tmp_path, "shift")[
        "columns"
    ]["clock"]

    assert clock["range"] == {
        "min": "00:00:00.000000600",
        "max": "23:59:59.999999999",
        "span_days": 0,
    }


def test_clickhouse_publishes_each_value_a_nanosecond_apart_in_any_zone(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.pulse (id Int32, t DateTime64(9, 'UTC'), "
        "tzk DateTime64(9, 'Asia/Kathmandu'), clock Time64(9), whole DateTime64(9, 'UTC')) "
        "ENGINE = MergeTree ORDER BY id",
    )
    cursor.execute(
        "INSERT INTO seedbank.pulse SELECT number, "
        "fromUnixTimestamp64Nano(toInt64(1772604000000000000 + number % 2 * 100), 'UTC'), "
        "fromUnixTimestamp64Nano(toInt64(1772604000000000000 + number % 2 * 100), 'Asia/Kathmandu'), "
        "toTime64(fromUnixTimestamp64Nano(toInt64(21600000000000 + number % 2 * 100), 'UTC'), 9), "
        "fromUnixTimestamp64Nano(toInt64(1772604000500000000), 'UTC') FROM numbers(40)",
    )
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )

    published = _published(_generated(adapter, "clickhouse", tmp_path, "pulse"))
    del published["id"]

    assert published == {
        "t": [("2026-03-04T06:00:00.000000100Z", 20), ("2026-03-04T06:00:00Z", 20)],
        "tzk": [("2026-03-04T06:00:00.000000100Z", 20), ("2026-03-04T06:00:00Z", 20)],
        "clock": [("06:00:00", 20), ("06:00:00.000000100", 20)],
        "whole": [("2026-03-04T06:00:00.500000Z", 40)],
    }


def test_snowflake_publishes_each_value_a_nanosecond_apart(tmp_path: Path) -> None:
    adapter = snowflake(
        "CREATE TABLE seedbank.pulse (t TIMESTAMP_NS, tz TIMESTAMPTZ_NS, clock TIME_NS, "
        "whole TIMESTAMP_NS)",
        f"INSERT INTO seedbank.pulse SELECT {_NANOSECOND_PAIRS_SQL}, "
        "CAST('2026-03-04 06:00:00.5' AS TIMESTAMP_NS) FROM range(40) r(i)",
        column_types={
            "t": "TIMESTAMP_NTZ",
            "tz": "TIMESTAMP_TZ",
            "clock": "TIME",
            "whole": "TIMESTAMP_NTZ",
        },
    )

    payload = _generated(adapter, "snowflake", tmp_path, "pulse")

    assert _published(payload) == {
        **_NANOSECOND_PAIRS,
        "whole": [("2026-03-04T06:00:00.500000", 40)],
    }


def test_clickhouse_publishes_a_seven_digit_instant_past_the_nanosecond_range(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.vault_door (id Int32, sealed DateTime64(7, 'UTC')) "
        "ENGINE = MergeTree ORDER BY id",
    )
    cursor.execute(
        "INSERT INTO seedbank.vault_door SELECT number, "
        "if(number = 199, toDateTime64('2270-07-19 00:00:00.0000003', 7, 'UTC'), "
        "toDateTime64('2270-01-01 00:00:00', 7, 'UTC') + toIntervalDay(number)) FROM numbers(200)",
    )
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )

    sealed = _generated(adapter, "clickhouse", tmp_path, "vault_door")["columns"]["sealed"]

    assert sealed["range"] == {
        "min": "2270-01-01T00:00:00Z",
        "max": "2270-07-19T00:00:00.000000300Z",
        "span_days": 199,
    }
    assert "unmeasured" not in sealed


def test_a_nanosecond_instant_hashes_alike_on_duckdb_and_clickhouse(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE TABLE beat (id INTEGER, t TIMESTAMPTZ_NS)")
    con.execute(
        "INSERT INTO beat SELECT i, make_timestamp_ns(1772604000000000000 + i * 100) "
        "AT TIME ZONE 'UTC' FROM range(50) r(i)",
    )
    con.close()
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.beat (id Int32, t DateTime64(9, 'UTC')) ENGINE = MergeTree ORDER BY id",
    )
    cursor.execute(
        "INSERT INTO seedbank.beat SELECT number, "
        "fromUnixTimestamp64Nano(toInt64(1772604000000000000 + number * 100), 'UTC') FROM numbers(50)",
    )
    clickhouse = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )

    on_duckdb = _generated(
        DuckdbAdapter({"database": str(database)}),
        "duckdb",
        tmp_path / "d",
        "beat",
    )
    on_clickhouse = _generated(clickhouse, "clickhouse", tmp_path / "c", "beat")

    listed = {entry["value"] for entry in on_duckdb["columns"]["t"]["values"]}

    assert "2026-03-04T06:00:00.000004900Z" in listed
    assert on_duckdb["columns"]["t"]["sketch"] == on_clickhouse["columns"]["t"]["sketch"]


def test_a_duckdb_tuple_column_leaves_the_grain_to_the_measured_search(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE TABLE plot (bed INTEGER, pos INTEGER, spot TUPLE(INTEGER, VARCHAR))")
    con.execute("INSERT INTO plot SELECT i // 10, i % 10, (i, 'p' || i) FROM range(500) r(i)")
    con.close()

    with caplog.at_level(logging.WARNING, logger="dbprint"):
        payload = _generated(DuckdbAdapter({"database": str(database)}), "duckdb", tmp_path, "plot")

    assert payload["columns"]["spot"]["classification"] == "unsupported"
    assert payload["grain"]["keys"] == [{"columns": ["bed", "pos"], "detection": "measured"}]
    assert not [r for r in caplog.records if "suppressed" in r.getMessage()]


_CLICKHOUSE_DECLARATIONS = {
    "AggregateFunction": "AggregateFunction(uniq, UInt64)",
    "Array": "Array(String)",
    "DateTime64": "DateTime64(9, 'UTC')",
    "Decimal": "Decimal(10, 2)",
    "Decimal32": "Decimal32(2)",
    "Decimal64": "Decimal64(2)",
    "Decimal128": "Decimal128(2)",
    "Decimal256": "Decimal256(2)",
    "Enum": "Enum8('a' = 1)",
    "Enum8": "Enum8('a' = 1)",
    "Enum16": "Enum16('a' = 1)",
    "FixedString": "FixedString(4)",
    "Map": "Map(String, UInt8)",
    "Nested": "Nested(a String)",
    "QBit": "QBit(Float32, 8)",
    "SimpleAggregateFunction": "SimpleAggregateFunction(sum, UInt64)",
    "Time64": "Time64(3)",
    "Tuple": "Tuple(String, UInt8)",
    "Variant": "Variant(String, UInt8)",
}

_CLICKHOUSE_SKIPPED = {
    "LowCardinality": "a wrapper; the wrapped family is swept on its own",
    "Nullable": "a wrapper; the wrapped family is swept on its own",
    "Nothing": "cannot be a table column",
}


def _generated(adapter: Adapter, vendor: AdapterType, tmp_path: Path, table: str) -> dict[str, Any]:
    conn = ConnectionConfig(name="garden", adapter=vendor, output=tmp_path / "prints")
    result = Engine(adapter, conn, tmp_path).generate()
    issues = conformance_errors(tmp_path / "prints" / "garden")

    assert [t.error for t in result.tables if t.status != "ok"] == []
    assert issues == []

    path = next((tmp_path / "prints").rglob(f"{table}/statistics.yaml"))

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _published(payload: dict[str, Any]) -> dict[str, list[tuple[str, int]]]:
    return {
        name: [(entry["value"], entry["count"]) for entry in column.get("values", [])]
        for name, column in payload["columns"].items()
    }


def _generated_classes(
    adapter: Adapter,
    vendor: AdapterType,
    tmp_path: Path,
    table: str,
) -> dict[str, str]:
    columns = _generated(adapter, vendor, tmp_path, table)["columns"]

    return {name: col["classification"] for name, col in columns.items()}


def _uncatalogued(vendor: str, reported: list[str]) -> list[str]:
    catalogued = {base_type(spelling) for spelling in _CATALOG[vendor]}

    return sorted({spelling for spelling in reported if base_type(spelling) not in catalogued})


def _stats_module(adapter_cls: type[Adapter]) -> ModuleType:
    return importlib.import_module(f"{adapter_cls.__module__.rsplit('.', 1)[0]}.stats")


def _bare(adapter_cls: type[Adapter]) -> Adapter:
    return adapter_cls.__new__(adapter_cls)
