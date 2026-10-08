"""Every statement run after the statistics pass renders a column type the way the others do.

A live sweep pins each adapter's key sketch to `canonical_form`; two structural guards hold the rest.
"""

from __future__ import annotations

import ast
import datetime as dt
import decimal
import itertools
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_args

import pytest

import dbprint.adapters as adapters_package
from dbprint.adapters import Adapter, ColumnMeta, StatisticsConfig
from dbprint.adapters.base import Adapter as AdapterBase
from dbprint.adapters.base import TemporalShape
from dbprint.spec.absence import SAMPLED_CLASSIFICATIONS
from dbprint.spec.classification import (
    classify,
    has_calendar_component,
    is_string_like_type,
    is_temporal_type,
)
from dbprint.spec.sketch import K, SketchKind, canonical_form, low64_md5, sketch_kind
from tests.adapters.conftest import SQL_PARAMS, _adapter_factory_for, _mysql_exec_many
from tests.conftest import pg_connect


_UTC_PLUS_2 = dt.timezone(dt.timedelta(hours=2))

_VALUES: dict[str, tuple[Any, Any]] = {
    "opens_at": (dt.time(0, 0, 37), dt.time(12, 30, 0, 250000)),
    "opens_at_tz": (
        dt.time(1, 0, 0, 250000, tzinfo=_UTC_PLUS_2),
        dt.time(2, 0, 0, tzinfo=_UTC_PLUS_2),
    ),
    "fee": (decimal.Decimal("4.0000"), decimal.Decimal("1.2500")),
    "is_open": (True, False),
    "season": (1990, 1991),
    "sown_on": (dt.date(2024, 1, 1), dt.date(2024, 2, 29)),
    "sown_at": (
        dt.datetime(2024, 1, 1, 0, 0, 0, 500000),
        dt.datetime(2024, 6, 1, 12, 0),
    ),
    "sown_at_tz": (
        dt.datetime(2024, 1, 1, 0, 0, 0, 500000, tzinfo=dt.UTC),
        dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.UTC),
    ),
    "label": ("plot-1", "plot-2"),
    "plots": (3, 7),
    "payload": (b"\x0a\xff", b"\x00"),
    "id": (1, 2),
    # A zoneless ClickHouse DateTime64 is an instant shown in the server zone (UTC here).
    "sown_at_server": (
        dt.datetime(2024, 1, 1, 0, 0, 0, 500000, tzinfo=dt.UTC),
        dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.UTC),
    ),
}

_SWEPT = frozenset(
    {
        "compute_key_sketch",
        "compute_normalized_cardinality",
        "compute_null_patterns",
        "compute_populated_windows",
        "probe_dependencies",
        "probe_grain",
        "probe_timeline",
        "sample_values",
    },
)
_NOT_SECONDARY = frozenset(
    {
        "close",
        "compute_base_statistics",
        "compute_column_statistics",
        "connect",
        "default_collation",
        "estimate_row_count",
        "execute_query",
        "extract_comments",
        "extract_ddl",
        "introspect_columns",
        "introspect_indexes",
        "introspect_physical_layout",
        "introspect_relationships",
        "introspect_unique_keys",
        "introspect_view_dependencies",
        "list_tables",
        "new_session",
    },
)

_PACKAGE = Path(adapters_package.__file__).parent


def test_every_abstract_adapter_method_is_swept_or_allowlisted() -> None:
    abstract = set(AdapterBase.__abstractmethods__)

    assert abstract - _SWEPT - _NOT_SECONDARY == set()
    assert (_SWEPT | _NOT_SECONDARY) - abstract == set()


def test_no_adapter_module_but_rendering_holds_a_temporal_rule() -> None:
    hits = [
        f"{path.relative_to(_PACKAGE)}:{line} {what}"
        for path in sorted(_PACKAGE.glob("*/*.py"))
        if path.name != "rendering.py"
        for line, what in _temporal_rules(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert hits == []


def test_the_structural_guard_flags_what_it_exists_to_catch() -> None:
    snippet = ast.parse(
        '_TZ_TYPES = ("timestamp with time zone",)\n'
        "def _render_calendar_bound(expr, sql_type):\n"
        "    return expr\n",
    )

    assert {what for _, what in _temporal_rules(snippet)} == {
        "temporal type tuple _TZ_TYPES",
        "renderer _render_calendar_bound",
    }


def test_every_substrate_catalogues_every_shape_and_kind() -> None:
    logical = set(get_args(TemporalShape)) | set(get_args(SketchKind))

    assert set(_CATALOGUE) == set(SQL_PARAMS)
    assert {vendor: set(entries) for vendor, entries in _CATALOGUE.items()} == dict.fromkeys(
        _CATALOGUE,
        logical,
    )


@pytest.mark.parametrize("vendor", SQL_PARAMS)
def test_every_reached_secondary_statement_renders_each_type(
    vendor: str,
    request: pytest.FixtureRequest,
) -> None:
    seeded = _SEEDERS[vendor](request)
    catalogued = {c for c in _CATALOGUE[vendor].values() if not c.startswith("absent:")}
    assert catalogued <= set(seeded), catalogued - set(seeded)
    adapter = _adapter_factory_for(request, vendor)()
    fqn = next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if t.fqn.endswith(".slot")
    )
    columns = adapter.introspect_columns(fqn)
    counts, phase_a = adapter.compute_base_statistics(fqn, columns, StatisticsConfig())
    by_name = {c.name: c for c in columns}

    for name in seeded:
        _sweep_column(adapter, fqn, by_name[name], _VALUES[name])

    calendar = [c.name for c in columns if has_calendar_component(c.classified_type)]
    pairs = tuple(itertools.combinations([c.name for c in columns], 2))

    ranges = _ranges(adapter, fqn, columns, counts, phase_a.stats, calendar)

    for anchor in calendar:
        low, high = ranges[anchor]

        for unit in ("day", "week", "month"):
            buckets = adapter.probe_timeline(fqn, columns, counts, anchor, unit)
            assert buckets, f"{vendor}.{anchor} {unit}"

        days = adapter.probe_timeline(fqn, columns, counts, anchor, "day")
        assert (days[0][0][:10], days[-1][0][:10]) == (low[:10], high[:10]), f"{vendor}.{anchor}"

        subjects = tuple(c.name for c in columns if c.name != anchor)
        windows = adapter.compute_populated_windows(fqn, columns, counts, anchor, subjects)
        assert windows == dict.fromkeys(subjects, (low, high)), f"{vendor}.{anchor}"

    adapter.probe_grain(fqn, columns, counts, pairs)
    adapter.probe_dependencies(fqn, columns, counts, phase_a.stats, pairs)
    adapter.compute_null_patterns(fqn, columns, StatisticsConfig(), counts, phase_a.stats)
    adapter.close()


def _ranges(
    adapter: Adapter,
    fqn: str,
    columns: list[ColumnMeta],
    counts: Any,
    base: Any,
    names: list[str],
) -> dict[str, tuple[str, str]]:
    temporal = [c for c in columns if c.name in names]
    config = StatisticsConfig(enumeration_threshold=0)
    stats = adapter.compute_column_statistics(fqn, temporal, config, counts, base, frozenset())

    ranges = {name: stats[name].range for name in names}
    assert all(rng is not None for rng in ranges.values()), ranges

    return {name: (rng.min, rng.max) for name, rng in ranges.items() if rng is not None}


def _sweep_column(adapter: Adapter, fqn: str, col: ColumnMeta, held: tuple[Any, Any]) -> None:
    classification = classify(col.classified_type, 2, False, 50)

    if (kind := sketch_kind(col.classified_type)) is not None:
        hashes = adapter.compute_key_sketch(fqn, col.name, col.classified_type, kind, K)
        expected = {low64_md5(canonical_form(value, kind)) for value in held}

        assert set(hashes) == expected, f"{col.name} ({col.sql_type})"

    if is_string_like_type(col.classified_type) and not is_temporal_type(col.classified_type):
        assert adapter.compute_normalized_cardinality(fqn, col.name, col.classified_type) == 2

    if classification in SAMPLED_CLASSIFICATIONS:
        assert len(adapter.sample_values(fqn, col.name, 10)) == 2


def _seed_duckdb(request: pytest.FixtureRequest) -> tuple[str, ...]:
    con = request.getfixturevalue("duckdb_native_connection")
    _seed_duckdb_family(con.execute)

    return _DUCKDB_COLUMNS


def _seed_snowflake(request: pytest.FixtureRequest) -> tuple[str, ...]:
    shim = request.getfixturevalue("snowflake_duckdb_connection")
    _seed_duckdb_family(shim.execute)

    return ("opens_at", "fee", "is_open", "sown_on", "sown_at", "label", "plots", "payload")


_DUCKDB_COLUMNS = (
    "opens_at",
    "opens_at_tz",
    "fee",
    "is_open",
    "sown_on",
    "sown_at",
    "sown_at_tz",
    "label",
    "plots",
    "payload",
)


def _seed_duckdb_family(execute: Callable[..., Any]) -> None:
    execute(
        "CREATE TABLE seedbank.slot (opens_at TIME, opens_at_tz TIMETZ, fee DECIMAL(12,4), "
        "is_open BOOLEAN, sown_on DATE, sown_at TIMESTAMP, sown_at_tz TIMESTAMPTZ, label VARCHAR, "
        "plots INTEGER, payload BLOB)",
    )
    execute(
        "INSERT INTO seedbank.slot VALUES "
        "('00:00:37', '01:00:00.25+02', 4, true, '2024-01-01', '2024-01-01 00:00:00.5', "
        "'2024-01-01 00:00:00.5+00', 'plot-1', 3, '\\x0A\\xFF'::BLOB), "
        "('12:30:00.25', '02:00:00+02', 1.25, false, '2024-02-29', '2024-06-01 12:00:00', "
        "'2024-06-01 12:00:00+00', 'plot-2', 7, '\\x00'::BLOB)",
    )


def _seed_postgres(request: pytest.FixtureRequest) -> tuple[str, ...]:
    creds = request.getfixturevalue("postgres_test_db")
    _seed_postgres_family(creds)

    return _DUCKDB_COLUMNS


def _seed_redshift(request: pytest.FixtureRequest) -> tuple[str, ...]:
    shim = request.getfixturevalue("redshift_postgres_connection")
    shim.execute(
        "CREATE TABLE seedbank.slot (opens_at time, opens_at_tz timetz, fee numeric(12,4), "
        "is_open boolean, sown_on date, sown_at timestamp, sown_at_tz timestamptz, "
        "label varchar(20), plots integer, payload bytea)",
    )
    shim.execute(
        "INSERT INTO seedbank.slot VALUES "
        "('00:00:37', '01:00:00.25+02', 4, true, '2024-01-01', '2024-01-01 00:00:00.5', "
        "'2024-01-01 00:00:00.5+00', 'plot-1', 3, '\\x0aff'::bytea), ('12:30:00.25', "
        "'02:00:00+02', 1.25, false, '2024-02-29', '2024-06-01 12:00:00', '2024-06-01 12:00:00+00', "
        "'plot-2', 7, '\\x00'::bytea)",
    )
    shim.execute("ANALYZE")

    return _DUCKDB_COLUMNS


def _seed_postgres_family(creds: dict[str, str]) -> None:
    with pg_connect(creds) as conn:
        conn.execute(
            "CREATE TABLE seedbank.slot (opens_at time, opens_at_tz timetz, fee numeric(12,4), "
            "is_open boolean, sown_on date, sown_at timestamp, sown_at_tz timestamptz, "
            "label varchar(20), plots integer, payload bytea)",
        )
        conn.execute(
            "INSERT INTO seedbank.slot VALUES "
            "('00:00:37', '01:00:00.25+02', 4, true, '2024-01-01', '2024-01-01 00:00:00.5', "
            "'2024-01-01 00:00:00.5+00', 'plot-1', 3, '\\x0aff'::bytea), "
            "('12:30:00.25', '02:00:00+02', 1.25, false, '2024-02-29', '2024-06-01 12:00:00', "
            "'2024-06-01 12:00:00+00', 'plot-2', 7, '\\x00'::bytea)",
        )
        conn.execute("ANALYZE")


def _seed_mysql(request: pytest.FixtureRequest) -> tuple[str, ...]:
    creds = request.getfixturevalue("mysql_test_db")
    _mysql_exec_many(
        int(creds["port"]),
        creds["database"],
        [
            (
                "CREATE TABLE slot (id int primary key, opens_at time(6), fee decimal(12,4), "
                "is_open tinyint(1), season year, sown_on date, sown_at datetime(6), "
                "sown_at_tz timestamp(6) NULL, label varchar(20), payload varbinary(4))"
            ),
            "SET time_zone = '+00:00'",
            (
                "INSERT INTO slot VALUES "
                "(1, '00:00:37', 4, 1, 1990, '2024-01-01', '2024-01-01 00:00:00.5', "
                "'2024-01-01 00:00:00.5', 'plot-1', X'0AFF'), "
                "(2, '12:30:00.25', 1.25, 0, 1991, '2024-02-29', '2024-06-01 12:00:00', "
                "'2024-06-01 12:00:00', 'plot-2', X'00')"
            ),
            "ANALYZE TABLE slot",
        ],
    )

    return (
        "id",
        "opens_at",
        "fee",
        "is_open",
        "season",
        "sown_on",
        "sown_at",
        "sown_at_tz",
        "label",
        "payload",
    )


def _seed_clickhouse(request: pytest.FixtureRequest) -> tuple[str, ...]:
    cursor = request.getfixturevalue("clickhouse_native_connection")
    cursor.execute(
        "CREATE TABLE seedbank.slot (id Int32, opens_at Time64(6), fee Decimal(12, 4), is_open Bool, "
        "sown_on Date, sown_at_server DateTime64(6), sown_at_tz DateTime64(6, 'UTC'), label String) "
        "ENGINE = MergeTree ORDER BY id SETTINGS enable_time_time64_type = 1",
    )
    cursor.execute(
        "INSERT INTO seedbank.slot SELECT * FROM values('id Int32, opens_at String, "
        "fee Decimal(12, 4), is_open Bool, sown_on Date, sown_at_server DateTime64(6), "
        "sown_at_tz DateTime64(6, ''UTC''), label String', (1, '00:00:37', 4, true, '2024-01-01', "
        "'2024-01-01 00:00:00.5', '2024-01-01 00:00:00.5', 'plot-1'), (2, '12:30:00.25', 1.25, "
        "false, '2024-02-29', '2024-06-01 12:00:00', '2024-06-01 12:00:00', 'plot-2')) "
        "SETTINGS enable_time_time64_type = 1",
    )

    return ("id", "opens_at", "fee", "is_open", "sown_on", "sown_at_server", "sown_at_tz", "label")


def _seed_bigquery(request: pytest.FixtureRequest) -> tuple[str, ...]:
    cursor, dataset = request.getfixturevalue("bigquery_test_dataset")
    table = f"`dbprint-test`.`{dataset}`.slot"
    cursor.execute(
        f"CREATE TABLE {table} (opens_at TIME, is_open BOOL, sown_on DATE, sown_at DATETIME, "
        "sown_at_tz TIMESTAMP, label STRING, plots INT64, payload BYTES)",
    )
    cursor.execute(
        f"INSERT INTO {table} VALUES "
        "(TIME '00:00:37', true, DATE '2024-01-01', DATETIME '2024-01-01 00:00:00.5', "
        "TIMESTAMP '2024-01-01 00:00:00.5+00', 'plot-1', 3, FROM_HEX('0aff')), "
        "(TIME '12:30:00.25', false, DATE '2024-02-29', DATETIME '2024-06-01 12:00:00', "
        "TIMESTAMP '2024-06-01 12:00:00+00', 'plot-2', 7, FROM_HEX('00'))",
    )

    return ("opens_at", "is_open", "sown_on", "sown_at", "sown_at_tz", "label", "plots", "payload")


def _seed_databricks(request: pytest.FixtureRequest) -> tuple[str, ...]:
    cursor = request.getfixturevalue("databricks_test_schema")
    cursor.execute(
        "CREATE TABLE slot (fee DECIMAL(12,4), is_open BOOLEAN, sown_on DATE, "
        "sown_at TIMESTAMP_NTZ, sown_at_tz TIMESTAMP, label STRING, plots INT, payload BINARY) "
        "USING DELTA",
    )
    cursor.execute(
        "INSERT INTO slot VALUES "
        "(4, true, DATE '2024-01-01', TIMESTAMP_NTZ '2024-01-01 00:00:00.5', "
        "TIMESTAMP '2024-01-01 00:00:00.5+00:00', 'plot-1', 3, X'0AFF'), "
        "(1.25, false, DATE '2024-02-29', TIMESTAMP_NTZ '2024-06-01 12:00:00', "
        "TIMESTAMP '2024-06-01 12:00:00+00:00', 'plot-2', 7, X'00')",
    )

    return ("fee", "is_open", "sown_on", "sown_at", "sown_at_tz", "label", "plots", "payload")


_NO_TIME_TZ = "absent: the engine has no time-of-day-with-zone type"
_NO_YEAR = "absent: the engine has no YEAR type"

# Every TemporalShape and SketchKind per substrate: the column sweeping it, or why none can.
_CATALOGUE: dict[str, dict[str, str]] = {
    "duckdb": {
        "date": "sown_on",
        "time": "opens_at",
        "time_tz": "opens_at_tz",
        "timestamp": "sown_at",
        "timestamp_tz": "sown_at_tz",
        "year": _NO_YEAR,
        "integer": "plots",
        "decimal": "fee",
        "text": "label",
        "boolean": "is_open",
        "temporal": "sown_at",
        "binary": "payload",
    },
    "snowflake": {
        "date": "sown_on",
        "time": "opens_at",
        "time_tz": _NO_TIME_TZ,
        "timestamp": "sown_at",
        "timestamp_tz": "absent: the duckdb stand-in cannot hold Snowflake's TIMESTAMP_TZ",
        "year": _NO_YEAR,
        "integer": "plots",
        "decimal": "fee",
        "text": "label",
        "boolean": "is_open",
        "temporal": "sown_at",
        "binary": "payload",
    },
    "postgres": {
        "date": "sown_on",
        "time": "opens_at",
        "time_tz": "opens_at_tz",
        "timestamp": "sown_at",
        "timestamp_tz": "sown_at_tz",
        "year": _NO_YEAR,
        "integer": "plots",
        "decimal": "fee",
        "text": "label",
        "boolean": "is_open",
        "temporal": "sown_at",
        "binary": "payload",
    },
    "redshift": {
        "date": "sown_on",
        "time": "opens_at",
        "time_tz": "opens_at_tz",
        "timestamp": "sown_at",
        "timestamp_tz": "sown_at_tz",
        "year": _NO_YEAR,
        "integer": "plots",
        "decimal": "fee",
        "text": "label",
        "boolean": "is_open",
        "temporal": "sown_at",
        "binary": "payload",
    },
    "mysql": {
        "date": "sown_on",
        "time": "opens_at",
        "time_tz": _NO_TIME_TZ,
        "timestamp": "sown_at",
        "timestamp_tz": "sown_at_tz",
        "year": "season",
        "integer": "id",
        "decimal": "fee",
        "text": "label",
        "boolean": "is_open",
        "temporal": "sown_at",
        "binary": "payload",
    },
    "clickhouse": {
        "date": "sown_on",
        "time": "opens_at",
        "time_tz": _NO_TIME_TZ,
        "timestamp": "absent: a zoneless DateTime64 is an instant in the server zone",
        "timestamp_tz": "sown_at_tz",
        "year": _NO_YEAR,
        "integer": "id",
        "decimal": "fee",
        "text": "label",
        "boolean": "is_open",
        "temporal": "sown_at_server",
        "binary": "absent: the engine has no binary type",
    },
    "bigquery": {
        "date": "sown_on",
        "time": "opens_at",
        "time_tz": _NO_TIME_TZ,
        "timestamp": "sown_at",
        "timestamp_tz": "sown_at_tz",
        "year": _NO_YEAR,
        "integer": "plots",
        "decimal": "absent: the decimal sketch drops the declared scale - an open defect",
        "text": "label",
        "boolean": "is_open",
        "temporal": "sown_at",
        "binary": "payload",
    },
    "databricks": {
        "date": "sown_on",
        "time": "absent: the engine has no time-of-day type",
        "time_tz": _NO_TIME_TZ,
        "timestamp": "sown_at",
        "timestamp_tz": "sown_at_tz",
        "year": _NO_YEAR,
        "integer": "plots",
        "decimal": "fee",
        "text": "label",
        "boolean": "is_open",
        "temporal": "sown_at",
        "binary": "payload",
    },
}


_SEEDERS: dict[str, Callable[[pytest.FixtureRequest], tuple[str, ...]]] = {
    "bigquery": _seed_bigquery,
    "clickhouse": _seed_clickhouse,
    "databricks": _seed_databricks,
    "duckdb": _seed_duckdb,
    "mysql": _seed_mysql,
    "postgres": _seed_postgres,
    "redshift": _seed_redshift,
    "snowflake": _seed_snowflake,
}


def _temporal_rules(tree: ast.AST) -> list[tuple[int, str]]:
    out = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_render"):
            out.append((node.lineno, f"renderer {node.name}"))

        if isinstance(node, ast.Assign) and _names_temporal_types(node.value):
            out.extend(
                (node.lineno, f"temporal type tuple {target.id}")
                for target in node.targets
                if isinstance(target, ast.Name)
            )

    return out


def _names_temporal_types(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Tuple)
        and bool(value.elts)
        and all(
            isinstance(elt, ast.Constant)
            and isinstance(elt.value, str)
            and is_temporal_type(elt.value)
            for elt in value.elts
        )
    )
