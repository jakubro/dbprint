"""Parameterized adapter fixture for the contract test battery.

`adapter_factory` covers the mock plus each DB-backed adapter's own substrate: Postgres on a
session-scoped local cluster (initdb + pg_ctl), Snowflake on an in-memory duckdb via
`SnowflakeDialectShim`. Each test gets its own freshly seeded database, torn down on exit.
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, LiteralString, cast

import chdb.dbapi
import duckdb
import psycopg
import pytest
from psycopg import sql

from dbprint.adapters import (
    Adapter,
    BigqueryAdapter,
    ClickhouseAdapter,
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    DatabricksAdapter,
    DuckdbAdapter,
    ForeignKeyMeta,
    IndexMeta,
    Inferred,
    Length,
    MockAdapter,
    MockTable,
    MysqlAdapter,
    PostgresAdapter,
    Range,
    RedshiftAdapter,
    SnowflakeAdapter,
    UniqueKeyMeta,
    ValueCount,
)
from tests.conftest import MysqlCluster, PostgresCluster


# Adapter factory parameterization.

PARAMS = [
    "mock",
    "postgres",
    "snowflake",
    "mysql",
    "duckdb",
    "clickhouse",
    "redshift",
    "databricks",
    "bigquery",
]
SQL_PARAMS = [
    "postgres",
    "snowflake",
    "mysql",
    "duckdb",
    "clickhouse",
    "redshift",
    "databricks",
    "bigquery",
]


# The wide contract table. The narrow tables (3 rows) pre-classify boolean/categorical, so
# the numeric, temporal, top-values and distribution branches never run on them.
# Against StatisticsConfig defaults, WIDE_ROW_COUNT clears `n * SMALL_TABLE_FACTOR` so
# sampling takes the sample path, and WIDE_DISTINCT exceeds `enumeration_threshold` so the
# wide columns escape `categorical`.

WIDE_ROW_COUNT = 200
WIDE_DISTINCT = 60

_WIDE_EPOCH = datetime(2026, 1, 1, 12, 0, 0)  # noqa: DTZ001 - seeds a naive temporal column
_WIDE_HERBARIUM_IDS = (
    "00000000-0000-7000-8000-000000000001",
    "00000000-0000-7000-8000-000000000002",
    "00000000-0000-7000-8000-000000000003",
)
_WIDE_RANKS = ("us", "eu", "ap")

# Day-count discriminators for SPEC 2.2.4: `observed_at` agrees under elapsed-time and
# calendar-boundary counting, so each column below is shaped to disagree - 0 either way,
# 0 elapsed but 1 across the boundary, and a 23:59:59 maximum one day short of span_days.
_WITHIN_DAY_START = datetime(2026, 1, 1, 1, 0, 0)  # noqa: DTZ001 - seeds a naive temporal column
_ACROSS_MIDNIGHT_START = datetime(2026, 1, 1, 23, 50, 0)  # noqa: DTZ001 - seeds a naive column
_LATE_EVENING_START = datetime(2025, 11, 3, 23, 59, 59)  # noqa: DTZ001 - seeds a naive column

# Future-dated columns (SPEC 2.2.4 clamp), pinned to fixed far-future instants rather than
# offsets from the clock so the fixture stays deterministic: `scheduled_at` is future
# throughout, `expires_at` pairs a past minimum with a sentinel maximum - the shape that
# drives the age subtraction negative without the clamp.
_SCHEDULED_START = datetime(3000, 1, 1, 12, 0, 0)  # noqa: DTZ001 - seeds a naive temporal column
_EXPIRES_START = datetime(2020, 1, 1, 12, 0, 0)  # noqa: DTZ001 - seeds a naive temporal column
_EXPIRES_STEP_DAYS = 6000

WideRow = tuple[str, str, str, int, str, str, str, str, str, str, str]

# Each temporal column's maximum and mandated span, so the battery's expectation derives
# from the seed, not from an adapter's output.
WIDE_TEMPORAL_MAX = {
    "observed_at": _WIDE_EPOCH + timedelta(days=WIDE_DISTINCT - 1),
    "within_day_at": _WITHIN_DAY_START + timedelta(minutes=20 * (WIDE_DISTINCT - 1)),
    "across_midnight_at": _ACROSS_MIDNIGHT_START + timedelta(seconds=20 * (WIDE_DISTINCT - 1)),
    "late_evening_at": _LATE_EVENING_START + timedelta(days=WIDE_DISTINCT - 1),
}
WIDE_TEMPORAL_SPAN_DAYS = {
    "observed_at": 59,
    "within_day_at": 0,
    "across_midnight_at": 0,
    "late_evening_at": 59,
}

# Columns whose maximum has not happened yet: age clamps to 0 rather than going negative.
WIDE_FUTURE_MAX = {
    "scheduled_at": _SCHEDULED_START + timedelta(days=WIDE_DISTINCT - 1),
    "expires_at": _EXPIRES_START + timedelta(days=_EXPIRES_STEP_DAYS * (WIDE_DISTINCT - 1)),
}


def _wide_rows() -> list[WideRow]:
    """Rows for `seedbank.viability_check`, identical across every substrate.

    `id` is fully unique (SPEC 4.2 `inferred.candidate_key`), `herbarium_id` is the declared
    FK, `label` repeats into top-values, `rank` is low-cardinality categorical. Every
    temporal column takes `WIDE_DISTINCT` distinct values, so it pre-classifies temporal.
    """

    rows: list[WideRow] = []

    for i in range(WIDE_ROW_COUNT):
        bucket = i % WIDE_DISTINCT
        rows.append(
            (
                f"00000000-0000-7000-8000-{i + 1000:012d}",
                _WIDE_HERBARIUM_IDS[i % len(_WIDE_HERBARIUM_IDS)],
                f"label-{bucket:03d}",
                bucket,
                _stamp(_WIDE_EPOCH + timedelta(days=bucket)),
                _WIDE_RANKS[i % len(_WIDE_RANKS)],
                _stamp(_WITHIN_DAY_START + timedelta(minutes=20 * bucket)),
                _stamp(_ACROSS_MIDNIGHT_START + timedelta(seconds=20 * bucket)),
                _stamp(_LATE_EVENING_START + timedelta(days=bucket)),
                _stamp(_SCHEDULED_START + timedelta(days=bucket)),
                _stamp(_EXPIRES_START + timedelta(days=_EXPIRES_STEP_DAYS * bucket)),
            ),
        )

    return rows


# Positions of `WideRow`'s temporal fields - the rest are id-, label- and score-shaped.
_WIDE_TEMPORAL_POSITIONS = (4, 6, 7, 8, 9, 10)


def _wide_values_clause(*, explicit_utc: bool = False) -> str:
    """Render the wide rows as one multi-row VALUES list - one round trip per test.

    `explicit_utc` appends a UTC offset to every temporal literal - needed only for Databricks,
    whose `TIMESTAMP` reinterprets a naive literal through the session zone at parse time.
    """

    def _cells(row: WideRow) -> list[Any]:
        cells: list[Any] = list(row)

        if explicit_utc:
            for i in _WIDE_TEMPORAL_POSITIONS:
                cells[i] = cells[i] + "+00:00"

        return cells

    return ",\n".join(
        "('{}', '{}', '{}', {}, '{}', '{}', '{}', '{}', '{}', '{}', '{}')".format(*_cells(row))
        for row in _wide_rows()
    )


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture(params=PARAMS, ids=PARAMS)
def adapter_factory(request: pytest.FixtureRequest) -> Callable[[], Adapter]:
    """Yield a factory that returns a connected adapter, parameterised by backend."""

    return _adapter_factory_for(request, request.param)


@pytest.fixture(params=SQL_PARAMS, ids=SQL_PARAMS)
def sql_adapter_factory(request: pytest.FixtureRequest) -> tuple[str, Callable[[], Adapter]]:
    """Yield `(vendor, factory)` for the adapters that emit SQL through a cursor.

    The mock is excluded: no cursor, no statements, nothing for a dialect sweep to observe.
    """

    return request.param, _adapter_factory_for(request, request.param)


@pytest.fixture
def all_sql_adapters(request: pytest.FixtureRequest) -> dict[str, Adapter]:
    """Every SQL adapter, connected, against its own substrate, in one test.

    `sql_adapter_factory` runs each parameter as its own test, so the values never meet.
    """

    return {kind: _adapter_factory_for(request, kind)() for kind in SQL_PARAMS}


def _adapter_factory_for(request: pytest.FixtureRequest, kind: str) -> Callable[[], Adapter]:
    """Build the connected-adapter factory for one backend name."""

    if kind == "mock":

        def build_mock() -> Adapter:
            a = MockAdapter(REFERENCE_FIXTURE)
            a.connect()

            return a

        return build_mock

    if kind == "postgres":
        creds = request.getfixturevalue("postgres_test_db")

        def build_postgres() -> Adapter:
            a = PostgresAdapter(creds)
            a.connect()

            return a

        return build_postgres

    if kind == "snowflake":
        duckdb_conn = request.getfixturevalue("snowflake_duckdb_connection")

        def build_snowflake() -> Adapter:
            creds = {
                "account": "test-account",
                "user": "test-user",
                "password": "test-password",
                "warehouse": "test-warehouse",
                "database": "memory",
                "role": "test-role",
            }
            a = SnowflakeAdapter(creds, cursor_factory=lambda _params: duckdb_conn)
            a.connect()

            return a

        return build_snowflake

    if kind == "mysql":
        creds = request.getfixturevalue("mysql_test_db")

        def build_mysql() -> Adapter:
            a = MysqlAdapter(creds)
            a.connect()

            return a

        return build_mysql

    if kind == "duckdb":
        native_connection = request.getfixturevalue("duckdb_native_connection")

        def build_duckdb() -> Adapter:
            a = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _p: native_connection)
            a.connect()

            return a

        return build_duckdb

    if kind == "clickhouse":
        native_cursor = request.getfixturevalue("clickhouse_native_connection")

        def build_clickhouse() -> Adapter:
            a = ClickhouseAdapter(
                {"host": "chdb", "database": "seedbank"},
                cursor_factory=lambda _p: native_cursor,
            )
            a.connect()

            return a

        return build_clickhouse

    if kind == "redshift":
        shim = request.getfixturevalue("redshift_postgres_connection")

        def build_redshift() -> Adapter:
            a = RedshiftAdapter(
                {"host": "redshift", "database": "seedbank", "user": "test", "password": "test"},
                cursor_factory=lambda _p: shim,
            )
            a.connect()

            return a

        return build_redshift

    if kind == "databricks":
        cursor = request.getfixturevalue("databricks_test_schema")

        def build_databricks() -> Adapter:
            a = DatabricksAdapter(
                {
                    "server_hostname": "local",
                    "http_path": "local",
                    "access_token": "local",
                    "catalog": "spark_catalog",
                },
                cursor_factory=lambda _p: cursor,
            )
            a.connect()

            return a

        return build_databricks

    if kind == "bigquery":
        cursor, dataset = request.getfixturevalue("bigquery_test_dataset")

        def build_bigquery() -> Adapter:
            a = BigqueryAdapter(
                {"project": "dbprint-test", "dataset": dataset},
                cursor_factory=lambda _p: cursor,
            )
            a.connect()

            return a

        return build_bigquery

    raise ValueError(f"unknown adapter kind: {kind!r}")


# Per-test in-memory duckdb seeded with the contract-test schema.


class SnowflakeDialectShim:
    """Serve the Snowflake-only catalog statements from duckdb's own catalog.

    What duckdb cannot parse is answered from its catalog and what it spells differently is
    rewritten; every other statement runs unchanged. Each substitution is a place the substrate
    stops being evidence about Snowflake, so the set stays as small as the adapter allows.
    """

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        cluster_by: dict[str, str] | None = None,
    ) -> None:
        self._con = con
        self._rows: list[tuple[Any, ...]] | None = None
        # duckdb has no clustering concept, so a test injects `cluster_by` here rather than
        # reading it off the catalog. Keyed lowercase to match what `physical_layout` compares.
        self._cluster_by = {name.lower(): value for name, value in (cluster_by or {}).items()}

    def execute(self, sql: str, params: Any = None) -> SnowflakeDialectShim:
        flat = " ".join(sql.lower().split())
        self._rows = None

        if "get_ddl(" in flat:
            self._rows = self._get_ddl(sql)
        elif flat.startswith("show imported keys in table"):
            self._rows = self._imported_keys(sql)
        elif flat.startswith("show primary keys in table"):
            self._rows = self._declared_keys(sql, "PRIMARY KEY")
        elif flat.startswith("show unique keys in table"):
            self._rows = self._declared_keys(sql, "UNIQUE")
        elif flat.startswith("show tables in schema"):
            self._rows = self._show_tables(sql)
        elif "object_dependencies" in flat:
            self._rows = self._object_dependencies(params)
        elif "information_schema.indexes" in flat:
            self._rows = self._indexes(params)
        elif flat.startswith("select row_count from information_schema.tables"):
            self._rows = self._row_count(params)
        elif flat.startswith("select comment from information_schema.tables"):
            self._rows = self._table_comment(params)
        elif flat.startswith("select column_name, comment from information_schema.columns"):
            self._rows = self._column_comments(params)
        elif params is None:
            self._con.execute(_to_duckdb(sql))
        else:
            self._con.execute(_to_duckdb(sql), params)

        return self

    def fetchall(self) -> list[Any]:
        return self._rows if self._rows is not None else self._con.fetchall()

    def fetchone(self) -> Any:
        if self._rows is None:
            return self._con.fetchone()

        return self._rows[0] if self._rows else None

    def close(self) -> None:
        self._con.close()

    def _get_ddl(self, sql: str) -> list[tuple[Any, ...]]:
        object_type, fqn = re.findall(r"'([^']*)'", sql)
        _, schema, name = fqn.split(".")
        source = "duckdb_views()" if object_type == "VIEW" else "duckdb_tables()"
        name_column = "view_name" if object_type == "VIEW" else "table_name"

        return self._con.execute(
            f"SELECT sql FROM {source} WHERE schema_name = ? AND {name_column} = ?",
            [schema, name],
        ).fetchall()

    def _show_tables(self, sql: str) -> list[tuple[Any, ...]]:
        """Fabricate `SHOW TABLES` rows: name at offset 1, cluster_by at 6, rest placeholder."""

        database, schema = re.findall(r'"([^"]*)"', sql)
        tables = self._con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = ?",
            [schema],
        ).fetchall()

        return [
            (None, name, database, schema, None, None, self._cluster_by.get(name.lower(), ""))
            for (name,) in tables
        ]

    def _row_count(self, params: Any) -> list[tuple[Any, ...]]:
        """Snowflake keeps a maintained row count on the table catalog entry.

        duckdb has no equivalent column, so the count comes from `estimated_size` - still a
        metadata answer to the adapter, and exact, so the threshold decision runs unstubbed.
        """

        catalog, schema, table = (str(p) for p in params)
        rows = self._con.execute(
            "SELECT estimated_size FROM duckdb_tables() "
            "WHERE database_name = ? AND schema_name = ? AND table_name = ?",
            [catalog, schema, table],
        ).fetchall()

        return rows if rows else [(None,)]

    def _table_comment(self, params: Any) -> list[tuple[Any, ...]]:
        return self._con.execute(
            "SELECT comment FROM duckdb_tables() "
            "WHERE database_name = ? AND schema_name = ? AND table_name = ?",
            list(params),
        ).fetchall()

    def _column_comments(self, params: Any) -> list[tuple[Any, ...]]:
        return self._con.execute(
            "SELECT column_name, comment FROM duckdb_columns() "
            "WHERE database_name = ? AND schema_name = ? AND table_name = ?",
            list(params),
        ).fetchall()

    def _declared_keys(self, sql: str, constraint_type: str) -> list[tuple[Any, ...]]:
        """Build `SHOW PRIMARY KEYS` / `SHOW UNIQUE KEYS` rows from duckdb's catalog.

        Both share one result shape, read positionally: created_on, database, schema, table,
        column, key_sequence, constraint_name, rely, comment.
        """

        database, schema, table = (
            part.strip('"') for part in sql.split("TABLE", 1)[1].strip().split(".")
        )
        rows = self._con.execute(
            "SELECT constraint_name, constraint_column_names FROM duckdb_constraints() "
            "WHERE constraint_type = ? AND database_name = ? "
            "AND schema_name = ? AND table_name = ?",
            [constraint_type, database, schema, table],
        ).fetchall()

        out: list[tuple[Any, ...]] = []

        for index, (name, columns) in enumerate(rows):
            # duckdb leaves a primary key's constraint_name NULL, but the adapter groups on
            # that name, so an unnamed key needs a stand-in or every key collapses into one.
            constraint_name = name or f"SYS_CONSTRAINT_{constraint_type.replace(' ', '_')}_{index}"

            for sequence, column in enumerate(columns, start=1):
                out.append(
                    (
                        None,
                        database,
                        schema,
                        table,
                        column,
                        sequence,
                        constraint_name,
                        "false",
                        None,
                    ),
                )

        return out

    def _imported_keys(self, sql: str) -> list[tuple[Any, ...]]:
        """Build `SHOW IMPORTED KEYS` rows from duckdb's FK catalog.

        One row per column of each foreign key, in Snowflake's documented column order - the
        adapter reads the tuple positionally.
        """

        database, schema, table = (
            part.strip('"') for part in sql.split("TABLE", 1)[1].strip().split(".")
        )
        rows = self._con.execute(
            "SELECT constraint_name, constraint_column_names, referenced_table, "
            "referenced_column_names FROM duckdb_constraints() "
            "WHERE constraint_type = 'FOREIGN KEY' AND database_name = ? "
            "AND schema_name = ? AND table_name = ?",
            [database, schema, table],
        ).fetchall()

        out: list[tuple[Any, ...]] = []

        for fk_name, source_columns, target_table, target_columns in rows:
            for sequence, (source, target) in enumerate(
                zip(source_columns, target_columns),
                start=1,
            ):
                out.append(
                    (
                        None,  # created_on
                        database,
                        schema,
                        target_table,
                        target,
                        database,
                        schema,
                        table,
                        source,
                        sequence,
                        "NO ACTION",
                        "NO ACTION",
                        fk_name,
                        f"SYS_CONSTRAINT_{fk_name}",
                        "NOT DEFERRABLE",
                        None,  # comment
                    ),
                )

        return out

    def _object_dependencies(self, params: Any) -> list[tuple[Any, ...]]:
        """Approximate `ACCOUNT_USAGE.OBJECT_DEPENDENCIES` by matching view SQL text against
        object names - duckdb tracks no dependency edge, so this proves row shape, not resolution.
        """

        database = str(params[0]) if params else ""
        views = self._con.execute(
            "SELECT database_name, schema_name, view_name, sql FROM duckdb_views() "
            "WHERE NOT internal",
        ).fetchall()
        objects = self._con.execute(
            "SELECT database_name, schema_name, table_name FROM duckdb_tables() WHERE NOT internal "
            "UNION ALL "
            "SELECT database_name, schema_name, view_name FROM duckdb_views() WHERE NOT internal",
        ).fetchall()

        rows: list[tuple[Any, ...]] = []

        for v_db, v_schema, v_name, v_sql in views:
            if v_db.upper() != database.upper():
                continue

            body = (v_sql or "").lower()

            for o_db, o_schema, o_name in objects:
                if o_db == v_db and o_schema == v_schema and o_name == v_name:
                    continue

                if re.search(rf"\b{re.escape(o_name.lower())}\b", body):
                    rows.append((v_db, v_schema, v_name, o_db, o_schema, o_name))

        return rows

    def _indexes(self, params: Any) -> list[tuple[Any, ...]]:
        """Expand duckdb_indexes() into the INDEXES x INDEX_COLUMNS row shape."""

        rows = self._con.execute(
            "SELECT index_name, is_unique, sql FROM duckdb_indexes() "
            "WHERE database_name = ? AND schema_name = ? AND table_name = ? "
            "ORDER BY index_name",
            list(params),
        ).fetchall()

        return [
            (index_name, is_unique, column)
            for index_name, is_unique, create_sql in rows
            for column in _index_columns(create_sql)
        ]


_SAMPLE_ROWS_RE = re.compile(r"\s+SAMPLE\s+ROW\s*\(\s*(\d+)\s+ROWS\s*\)", re.IGNORECASE)
_SAMPLE_FRACTION_RE = re.compile(
    r"\s+SAMPLE\s+(?:SYSTEM|BLOCK|BERNOULLI)?\s*\(\s*([\d.]+)\s*\)"
    r"(?:\s+(?:SEED|REPEATABLE)\s*\(\s*(\d+)\s*\))?",
    re.IGNORECASE,
)
# The argument is a bare derived-table alias (`mn`, `p_01`, ...) or a quoted column identifier.
_CONVERT_TIMEZONE_RE = re.compile(
    r"CONVERT_TIMEZONE\('UTC',\s*(\"(?:[^\"]|\"\")+\"|\w+)\)",
    re.IGNORECASE,
)
# The adapter emits exactly these two pictures, so a non-greedy capture to the literal suffices.
_TO_VARCHAR_TS_RE = re.compile(
    r"TO_VARCHAR\((.+?), 'YYYY-MM-DD\"T\"HH24:MI:SS\.FF6'\)",
    re.IGNORECASE,
)
_TO_VARCHAR_DATE_RE = re.compile(r"TO_VARCHAR\((.+?), 'YYYY-MM-DD'\)", re.IGNORECASE)
# The sketch's non-temporal canonical cast (SPEC 2.2.14) - bare column, no picture argument.
_TO_VARCHAR_BARE_RE = re.compile(r"TO_VARCHAR\((\"(?:[^\"]|\"\")+\")\)", re.IGNORECASE)
# The sketch's low-64-bit hash (SPEC 2.2.14) - a fixed 16-X hex format model over MD5.
_TO_NUMBER_HEX_RE = re.compile(r"TO_NUMBER\((.+?), 'X{16}'\)", re.IGNORECASE)
_MATERIALIZED_RE = re.compile(r'"[^"]+"\."[^"]+"\.("dbprint_sample_[0-9a-f]+")')
# `probe_grain` (SPEC 2.2.12) emits exactly two quoted columns per expression.
_COUNT_DISTINCT_MULTI_RE = re.compile(
    r'COUNT\(DISTINCT ("(?:[^"]|"")+"), ("(?:[^"]|"")+")\)',
    re.IGNORECASE,
)


def _to_duckdb(sql: str) -> str:
    """Rewrite the Snowflake constructs duckdb spells differently."""

    return _rewrite_sketch_hash(
        _rewrite_count_distinct_multi(
            _rewrite_temporal_render(_rewrite_sample(_unqualify_materialized(sql))),
        ),
    )


def _rewrite_count_distinct_multi(sql: str) -> str:
    """Snowflake's `COUNT(DISTINCT a, b)` -> duckdb's `COUNT(DISTINCT (a, b))` (SPEC 2.2.12)."""

    return _COUNT_DISTINCT_MULTI_RE.sub(r"COUNT(DISTINCT (\1, \2))", sql)


def _unqualify_materialized(sql: str) -> str:
    """Drop the database and schema from a materialized sample's own name.

    duckdb allows a temporary table only in its own `temp` catalog, so the bare name is what
    resolves for the create and every read after it; the prefix cannot match a real object.
    """

    return _MATERIALIZED_RE.sub(r"\1", sql)


def _rewrite_temporal_render(sql: str) -> str:
    """The adapter's temporal text rendering -> duckdb's own date/timestamp functions.

    `CONVERT_TIMEZONE` is rewritten first, so a TZ-aware picture wraps the already-rewritten
    expression rather than the original.
    """

    sql = _CONVERT_TIMEZONE_RE.sub(r"(\1 AT TIME ZONE 'UTC')", sql)
    sql = _TO_VARCHAR_TS_RE.sub(r"strftime(\1, '%Y-%m-%dT%H:%M:%S.%f')", sql)
    sql = _TO_VARCHAR_DATE_RE.sub(r"strftime(\1, '%Y-%m-%d')", sql)

    return _TO_VARCHAR_BARE_RE.sub(r"CAST(\1 AS VARCHAR)", sql)


def _rewrite_sketch_hash(sql: str) -> str:
    """SPEC 2.2.14's `TO_NUMBER(hex, 'XXXX...')` -> duckdb's `0x`-prefixed cast."""

    return _TO_NUMBER_HEX_RE.sub(r"(('0x' || \1))::UBIGINT", sql)


def _rewrite_sample(sql: str) -> str:
    """Snowflake's two `SAMPLE` forms -> the equivalent duckdb subqueries.

    duckdb accepts `USING SAMPLE` only at the end of a select, so the sampled reference is
    wrapped in a subquery instead. The fraction form is rewritten first, so an inner scope
    clause is already duckdb by the time the outer draw wraps it.
    """

    sql = _rewrite_sample_form(sql, _SAMPLE_FRACTION_RE, _duckdb_fraction)

    return _rewrite_sample_form(sql, _SAMPLE_ROWS_RE, lambda match: f"{match.group(1)} ROWS")


def _duckdb_fraction(match: re.Match[str]) -> str:
    """Snowflake's percentage draw as duckdb spells it, seed and all.

    duckdb carries method and seed in one parenthesised suffix. The method is always
    `bernoulli` - duckdb's `system` sampler takes whole vectors, so at fixture scale it
    returns every row or none, and the fraction stops meaning anything.
    """

    percent, seed = match.group(1), match.group(2)
    method = "bernoulli" if seed is None else f"bernoulli, {seed}"

    return f"{percent} PERCENT ({method})"


def _rewrite_sample_form(
    sql: str,
    pattern: re.Pattern[str],
    build: Callable[[re.Match[str]], str],
) -> str:
    """Replace every `<source> SAMPLE ...` this pattern matches with a duckdb wrap."""

    while True:
        match = pattern.search(sql)

        if match is None:
            return sql

        start = _sampled_source_start(sql, match.start())
        source = sql[start : match.start()]
        sql = (
            f"{sql[:start]}(SELECT * FROM {source} USING SAMPLE {build(match)}){sql[match.end() :]}"
        )


def _sampled_source_start(sql: str, end: int) -> int:
    """Index where the table reference ending at `end` begins - parenthesised or a bare token."""

    if sql[end - 1] != ")":
        start = end

        while start > 0 and not sql[start - 1].isspace():
            start -= 1

        return start

    depth = 0

    for index in range(end - 1, -1, -1):
        if sql[index] == ")":
            depth += 1
        elif sql[index] == "(":
            depth -= 1

            if depth == 0:
                return index

    return end


_INDEX_COLUMNS_RE = re.compile(r"\(([^)]+)\)")


def _index_columns(create_sql: str) -> list[str]:
    """Column list from a duckdb `CREATE INDEX ... ON tbl (cols)` statement."""

    match = _INDEX_COLUMNS_RE.search(create_sql or "")

    if not match:
        return []

    return [c.strip().strip('"').lower() for c in match.group(1).split(",") if c.strip()]


@pytest.fixture
def snowflake_duckdb_connection() -> Iterator[SnowflakeDialectShim]:
    """Fresh in-memory duckdb seeded with the contract-suite schema + data."""

    con = duckdb.connect(":memory:")
    # duckdb's sample seed is only repeatable single-threaded.
    con.execute("SET threads = 1")
    _seed_contract_schema_duckdb(con)

    try:
        yield SnowflakeDialectShim(con)
    finally:
        con.close()


@pytest.fixture
def duckdb_native_connection() -> Iterator[duckdb.DuckDBPyConnection]:
    """Fresh in-memory duckdb seeded with the contract-suite schema, read by the real adapter -
    no thread pin, since its seeded `TABLESAMPLE ... REPEATABLE` draws reproduce across threads.
    """

    con = duckdb.connect(":memory:")
    _seed_contract_schema_duckdb(con)

    try:
        yield con
    finally:
        con.close()


def _seed_contract_schema_duckdb(con: duckdb.DuckDBPyConnection) -> None:
    """Populate the duckdb instance with the contract-suite tables - mirrors the Postgres
    fixture, except FK actions stay NO ACTION: duckdb cannot parse CASCADE.
    """

    con.execute("CREATE SCHEMA seedbank")
    con.execute(
        """
        CREATE TABLE seedbank.herbarium (
            id UUID PRIMARY KEY,
            name VARCHAR(64) NOT NULL,
            rank VARCHAR(16) NOT NULL,
            code VARCHAR(16) NOT NULL
        )
        """,
    )
    # Bare unique index, no named constraint behind it: every substrate that can express
    # uniqueness through an index alone must agree the column is declared-unique.
    con.execute("CREATE UNIQUE INDEX herbarium_code_ux ON seedbank.herbarium (code)")
    con.execute(
        """
        CREATE TABLE seedbank.curator (
            id UUID PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            herbarium_id UUID REFERENCES seedbank.herbarium(id),
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            seed_count INTEGER,
            withdrawn_at TIMESTAMP
        )
        """,
    )
    con.execute("CREATE INDEX curator_email_idx ON seedbank.curator (email)")
    con.execute("COMMENT ON TABLE seedbank.curator IS 'Primary curator table'")
    con.execute("COMMENT ON COLUMN seedbank.curator.email IS 'user-facing email address'")

    con.execute(
        """
        INSERT INTO seedbank.herbarium (id, name, rank, code) VALUES
          ('00000000-0000-7000-8000-000000000001', 'Ashgrove',   'us', 'ASHGROVE'),
          ('00000000-0000-7000-8000-000000000002', 'Thornfield', 'eu', 'THORNFIELD'),
          ('00000000-0000-7000-8000-000000000003', 'Millbrook',  'us', 'MILLBROOK')
        """,
    )
    # seed_count carries the one seeded null (row 3); withdrawn_at is the all-null column.
    con.execute(
        """
        INSERT INTO seedbank.curator (id, email, herbarium_id, is_active, seed_count) VALUES
          ('00000000-0000-7000-8000-000000000011', 'a@x.com', '00000000-0000-7000-8000-000000000001', true,  30),
          ('00000000-0000-7000-8000-000000000012', 'b@x.com', '00000000-0000-7000-8000-000000000001', true,  31),
          ('00000000-0000-7000-8000-000000000013', 'c@x.com', '00000000-0000-7000-8000-000000000002', false, NULL)
        """,
    )

    con.execute(
        """
        CREATE TABLE seedbank.viability_check (
            id UUID PRIMARY KEY,
            herbarium_id UUID REFERENCES seedbank.herbarium(id),
            label VARCHAR(32) NOT NULL,
            score INTEGER NOT NULL,
            observed_at TIMESTAMP NOT NULL,
            rank VARCHAR(8) NOT NULL,
            within_day_at TIMESTAMP NOT NULL,
            across_midnight_at TIMESTAMP NOT NULL,
            late_evening_at TIMESTAMP NOT NULL,
            scheduled_at TIMESTAMP NOT NULL,
            expires_at TIMESTAMP NOT NULL
        )
        """,
    )
    con.execute(
        "INSERT INTO seedbank.viability_check "
        "(id, herbarium_id, label, score, observed_at, rank, within_day_at, across_midnight_at, late_evening_at, scheduled_at, expires_at) VALUES\n"
        + _wide_values_clause(),
    )


# Per-test in-memory chdb instance seeded with the contract-suite schema.


@pytest.fixture
def clickhouse_native_connection() -> Iterator[Any]:
    """Fresh in-memory chdb instance seeded with the contract-suite schema, read by the adapter."""

    conn = chdb.dbapi.connect()
    _seed_contract_schema_clickhouse(conn)

    try:
        yield conn.cursor()
    finally:
        conn.close()


def _seed_contract_schema_clickhouse(conn: Any) -> None:
    """Populate the chdb instance with the contract-suite tables: `SAMPLE BY sipHash64(id)` since
    a monotonic key never narrows (measured), and no FK/unique clause, which ClickHouse lacks.
    """

    cur = conn.cursor()
    cur.execute("CREATE DATABASE seedbank")
    cur.execute(
        """
        CREATE TABLE seedbank.herbarium (
            id UUID,
            name String,
            rank String,
            code String
        ) ENGINE = MergeTree
        ORDER BY (sipHash64(id), id)
        SAMPLE BY sipHash64(id)
        """,
    )
    cur.execute(
        """
        CREATE TABLE seedbank.curator (
            id UUID,
            email String COMMENT 'user-facing email address',
            herbarium_id Nullable(UUID),
            is_active Bool DEFAULT true,
            created_at DateTime DEFAULT now(),
            seed_count Nullable(Int32),
            withdrawn_at Nullable(DateTime),
            country LowCardinality(Nullable(String))
        ) ENGINE = MergeTree
        ORDER BY (sipHash64(id), id)
        SAMPLE BY sipHash64(id)
        COMMENT 'Primary curator table'
        """,
    )

    cur.execute(
        """
        INSERT INTO seedbank.herbarium (id, name, rank, code) VALUES
          ('00000000-0000-7000-8000-000000000001', 'Ashgrove',   'us', 'ASHGROVE'),
          ('00000000-0000-7000-8000-000000000002', 'Thornfield', 'eu', 'THORNFIELD'),
          ('00000000-0000-7000-8000-000000000003', 'Millbrook',  'us', 'MILLBROOK')
        """,
    )
    # seed_count and country each carry the one seeded null (row 3); withdrawn_at is all-null.
    cur.execute(
        """
        INSERT INTO seedbank.curator
          (id, email, herbarium_id, is_active, seed_count, country) VALUES
          ('00000000-0000-7000-8000-000000000011', 'a@x.com', '00000000-0000-7000-8000-000000000001', true,  30, 'us'),
          ('00000000-0000-7000-8000-000000000012', 'b@x.com', '00000000-0000-7000-8000-000000000001', true,  31, 'eu'),
          ('00000000-0000-7000-8000-000000000013', 'c@x.com', '00000000-0000-7000-8000-000000000002', false, NULL, NULL)
        """,
    )

    cur.execute(
        """
        CREATE TABLE seedbank.viability_check (
            id UUID,
            herbarium_id Nullable(UUID),
            label String,
            score Int32,
            observed_at DateTime64(0),
            rank String,
            within_day_at DateTime64(0),
            across_midnight_at DateTime64(0),
            late_evening_at DateTime64(0),
            scheduled_at DateTime64(0),
            expires_at DateTime64(0)
        ) ENGINE = MergeTree
        ORDER BY (sipHash64(id), id)
        SAMPLE BY sipHash64(id)
        """,
    )
    cur.execute(
        "INSERT INTO seedbank.viability_check "
        "(id, herbarium_id, label, score, observed_at, rank, within_day_at, across_midnight_at, late_evening_at, scheduled_at, expires_at) VALUES\n"
        + _wide_values_clause(),
    )

    # Every other table above declares `SAMPLE BY`; this one deliberately does not - the
    # shape `SAMPLE` cannot run against at any fraction (SAMPLING_NOT_SUPPORTED).
    cur.execute(
        """
        CREATE TABLE seedbank.unsampled (
            id UUID,
            note String
        ) ENGINE = MergeTree
        ORDER BY id
        """,
    )
    cur.execute(
        """
        INSERT INTO seedbank.unsampled (id, note) VALUES
          ('00000000-0000-7000-8000-000000000001', 'alpha'),
          ('00000000-0000-7000-8000-000000000002', 'beta')
        """,
    )


# Per-test postgres database seeded with the contract-test schema.


@pytest.fixture
def postgres_test_db(postgres_cluster: PostgresCluster) -> Iterator[dict[str, str]]:
    """Create a fresh DB in the shared cluster, seeded with the contract schema."""

    db_name = f"contract_{secrets.token_hex(4)}"
    admin_creds = {
        "host": "127.0.0.1",
        "port": str(postgres_cluster.port),
        "database": "postgres",
        "user": postgres_cluster.superuser,
        "password": "",
    }
    _exec_admin(admin_creds, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))

    db_creds = {**admin_creds, "database": db_name}
    _seed_contract_schema(db_creds)

    try:
        yield db_creds
    finally:
        _exec_admin(
            admin_creds,
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db_name)),
        )


def _seed_contract_schema(creds: dict[str, str]) -> None:
    """Populate the DB with two tables + one view used by the contract suite."""

    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password=creds["password"],
        autocommit=True,
    ) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS seedbank")
        conn.execute(
            """
            CREATE TABLE seedbank.herbarium (
                id uuid PRIMARY KEY,
                name varchar(64) NOT NULL,
                rank varchar(16) NOT NULL,
                code varchar(16) NOT NULL
            )
            """,
        )
        # Bare unique index, no named constraint behind it: every substrate that can express
        # uniqueness through an index alone must agree the column is declared-unique.
        conn.execute(
            "CREATE UNIQUE INDEX herbarium_code_ux ON seedbank.herbarium (code)",
        )
        conn.execute(
            """
            CREATE TABLE seedbank.curator (
                id uuid PRIMARY KEY,
                email varchar(255) UNIQUE NOT NULL,
                herbarium_id uuid NULL REFERENCES seedbank.herbarium(id) ON DELETE CASCADE,
                is_active boolean NOT NULL DEFAULT true,
                created_at timestamp with time zone NOT NULL DEFAULT now(),
                seed_count integer NULL,
                withdrawn_at timestamp with time zone NULL
            )
            """,
        )
        conn.execute("CREATE INDEX curator_email_idx ON seedbank.curator (email)")
        conn.execute("COMMENT ON TABLE seedbank.curator IS 'Primary curator table'")
        conn.execute("COMMENT ON COLUMN seedbank.curator.email IS 'user-facing email address'")

        # Seed minimal data so the contract suite's stats assertions see non-empty tables.
        conn.execute(
            """
            INSERT INTO seedbank.herbarium (id, name, rank, code) VALUES
              ('00000000-0000-7000-8000-000000000001', 'Ashgrove',  'us', 'ASHGROVE'),
              ('00000000-0000-7000-8000-000000000002', 'Thornfield','eu', 'THORNFIELD'),
              ('00000000-0000-7000-8000-000000000003', 'Millbrook', 'us', 'MILLBROOK')
            """,
        )
        # seed_count carries the one seeded null (row 3); withdrawn_at is the all-null column.
        conn.execute(
            """
            INSERT INTO seedbank.curator (id, email, herbarium_id, is_active, seed_count) VALUES
              ('00000000-0000-7000-8000-000000000011', 'a@x.com', '00000000-0000-7000-8000-000000000001', true,  30),
              ('00000000-0000-7000-8000-000000000012', 'b@x.com', '00000000-0000-7000-8000-000000000001', true,  31),
              ('00000000-0000-7000-8000-000000000013', 'c@x.com', '00000000-0000-7000-8000-000000000002', false, NULL)
            """,
        )

        conn.execute(
            """
            CREATE TABLE seedbank.viability_check (
                id uuid PRIMARY KEY,
                herbarium_id uuid NULL REFERENCES seedbank.herbarium(id),
                label varchar(32) NOT NULL,
                score integer NOT NULL,
                observed_at timestamp NOT NULL,
                rank varchar(8) NOT NULL,
                within_day_at timestamp NOT NULL,
                across_midnight_at timestamp NOT NULL,
                late_evening_at timestamp NOT NULL,
                scheduled_at timestamp NOT NULL,
                expires_at timestamp NOT NULL
            )
            """,
        )
        # Assembled at runtime, so LiteralString cannot apply; the values are fixture-only.
        conn.execute(
            cast(
                LiteralString,
                "INSERT INTO seedbank.viability_check "
                "(id, herbarium_id, label, score, observed_at, rank, within_day_at, across_midnight_at, late_evening_at, scheduled_at, expires_at) VALUES\n"
                + _wide_values_clause(),
            ),
        )

        # The planner's reltuples estimate is -1 until the table is analyzed, so without this
        # the wide table takes the small-table shortcut.
        conn.execute("ANALYZE")


# Helpers.


def _exec_admin(creds: dict[str, str], stmt: sql.SQL | sql.Composed) -> None:
    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password=creds["password"],
        autocommit=True,
    ) as conn:
        conn.execute(stmt)


# Per-test MySQL database seeded with the contract-test schema (MariaDB substrate).


@pytest.fixture
def mysql_test_db(mysql_cluster: MysqlCluster) -> Iterator[dict[str, str]]:
    """Create a fresh database in the shared cluster, seeded with the contract schema."""

    db_name = f"contract_{secrets.token_hex(4)}"
    _mysql_admin_exec(mysql_cluster.port, f"CREATE DATABASE `{db_name}`")
    _seed_contract_schema_mysql(mysql_cluster.port, db_name)
    creds = {
        "host": "127.0.0.1",
        "port": str(mysql_cluster.port),
        "database": db_name,
        "user": mysql_cluster.superuser,
        "password": "",
    }

    try:
        yield creds
    finally:
        _mysql_admin_exec(mysql_cluster.port, f"DROP DATABASE IF EXISTS `{db_name}`")


def _seed_contract_schema_mysql(port: int, db_name: str) -> None:
    """Populate the database with herbarium + curator (FK, index, comments)."""

    statements = [
        """
        CREATE TABLE herbarium (
            id CHAR(36) PRIMARY KEY,
            name VARCHAR(64) NOT NULL,
            rank VARCHAR(16) NOT NULL,
            code VARCHAR(16) NOT NULL,
            UNIQUE KEY herbarium_code_ux (code)
        )
        """,
        """
        CREATE TABLE curator (
            id CHAR(36) PRIMARY KEY,
            email VARCHAR(255) NOT NULL COMMENT 'user-facing email address',
            herbarium_id CHAR(36) NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            seed_count INT NULL,
            withdrawn_at DATETIME NULL,
            KEY curator_email_idx (email),
            CONSTRAINT curator_herbarium_fk FOREIGN KEY (herbarium_id)
                REFERENCES herbarium (id) ON DELETE CASCADE
        ) COMMENT='Primary curator table'
        """,
        """
        INSERT INTO herbarium (id, name, rank, code) VALUES
          ('00000000-0000-7000-8000-000000000001', 'Ashgrove',   'us', 'ASHGROVE'),
          ('00000000-0000-7000-8000-000000000002', 'Thornfield', 'eu', 'THORNFIELD'),
          ('00000000-0000-7000-8000-000000000003', 'Millbrook',  'us', 'MILLBROOK')
        """,
        # seed_count carries the one seeded null (row 3); withdrawn_at is explicitly NULL, so
        # the zero-date sentinel (a DEFAULT-value conversion) does not apply.
        """
        INSERT INTO curator (id, email, herbarium_id, is_active, seed_count) VALUES
          ('00000000-0000-7000-8000-000000000011', 'a@x.com',
           '00000000-0000-7000-8000-000000000001', 1, 30),
          ('00000000-0000-7000-8000-000000000012', 'b@x.com',
           '00000000-0000-7000-8000-000000000001', 1, 31),
          ('00000000-0000-7000-8000-000000000013', 'c@x.com',
           '00000000-0000-7000-8000-000000000002', 0, NULL)
        """,
        """
        CREATE TABLE viability_check (
            id CHAR(36) PRIMARY KEY,
            herbarium_id CHAR(36) NULL,
            label VARCHAR(32) NOT NULL,
            score INT NOT NULL,
            observed_at DATETIME NOT NULL,
            rank VARCHAR(8) NOT NULL,
            within_day_at DATETIME NOT NULL,
            across_midnight_at DATETIME NOT NULL,
            late_evening_at DATETIME NOT NULL,
            scheduled_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            CONSTRAINT metrics_herbarium_fk FOREIGN KEY (herbarium_id)
                REFERENCES herbarium (id)
        )
        """,
        "INSERT INTO viability_check (id, herbarium_id, label, score, observed_at, rank, within_day_at, across_midnight_at, late_evening_at, scheduled_at, expires_at) VALUES\n"
        + _wide_values_clause(),
        # InnoDB leaves information_schema.table_rows at 0 until analyzed, so without this
        # the wide table takes the small-table shortcut.
        "ANALYZE TABLE viability_check",
    ]
    _mysql_exec_many(port, db_name, statements)


def _mysql_admin_exec(port: int, stmt: str) -> None:
    """Run one statement against the cluster with no default database selected."""

    import mysql.connector

    conn = mysql.connector.connect(
        host="127.0.0.1",
        port=port,
        user="root",
        password="",
        autocommit=True,
    )

    try:
        cursor = conn.cursor()
        cursor.execute(stmt)
        cursor.close()
    finally:
        conn.close()


def _mysql_exec_many(port: int, db_name: str, statements: list[str]) -> None:
    """Run several statements against the given database.

    Buffered because a seed statement (`ANALYZE TABLE`) returns a status row; left unread it
    fails the next `execute` with "Unread result found".
    """

    import mysql.connector

    conn = mysql.connector.connect(
        host="127.0.0.1",
        port=port,
        user="root",
        password="",
        database=db_name,
        autocommit=True,
    )

    try:
        cursor = conn.cursor(buffered=True)

        for stmt in statements:
            cursor.execute(stmt)

        cursor.close()
    finally:
        conn.close()


# Per-test Redshift shim over a fresh Postgres database in the shared cluster.


_REDSHIFT_STRTOL_RE = re.compile(
    r"STRTOL\(SUBSTRING\(MD5\((.+?)\), (\d+), 8\), 16\)::NUMERIC",
    re.IGNORECASE,
)
_REDSHIFT_APPROX_DISC_RE = re.compile(r"APPROXIMATE\s+PERCENTILE_DISC\(", re.IGNORECASE)
# Postgres has no DATEDIFF function at all; `date - date` is its own native equivalent for the
# one call shape this adapter emits (both arguments always MIN/MAX of the same column).
_REDSHIFT_DATEDIFF_DAY_RE = re.compile(
    r"DATEDIFF\('day',\s*MIN\((.+?)\),\s*MAX\((.+?)\)\)",
    re.IGNORECASE,
)


def _to_postgres(sql: str) -> str:
    """Rewrite the three Redshift constructs Postgres spells differently - everything else the
    adapter emits is already Postgres-compatible SQL and passes through unchanged.
    """

    # Redshift's two 8-hex-digit STRTOL halves -> Postgres's `bit(32)` cast, as postgres/sketch.py
    # does; NUMERIC drops because the `::bigint::numeric` chain already lands there.
    rewritten = _REDSHIFT_STRTOL_RE.sub(
        r"('x' || substring(md5(\1), \2, 8))::bit(32)::bigint::numeric",
        sql,
    )

    # `APPROXIMATE` has no Postgres equivalent - there PERCENTILE_DISC is the ordinary form.
    rewritten = _REDSHIFT_APPROX_DISC_RE.sub("PERCENTILE_DISC(", rewritten)

    return _REDSHIFT_DATEDIFF_DAY_RE.sub(r"(MAX(\2) - MIN(\1))", rewritten)


class RedshiftDialectShim:
    """Serve the Redshift-only catalog statements from Postgres's own catalog: no Redshift
    substrate exists locally, and Postgres is the closest by dialect (see `execute` for the set).
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        sortkey_by_table: dict[str, tuple[tuple[str, int], ...]] | None = None,
        late_binding_views: frozenset[str] | None = None,
    ) -> None:
        self._conn = conn
        self._cursor = conn.cursor()
        self._rows: list[tuple[Any, ...]] | None = None
        # Postgres has no SORTKEY concept, so a test injects one here rather than reading it
        # off the catalog - the same seam `SnowflakeDialectShim.cluster_by` uses.
        self.sortkey_by_table = sortkey_by_table or {}
        # Postgres has no late-binding view concept - a test names one here, and
        # `_view_dependencies` rewrites its rows into the unresolved shape one would produce.
        self.late_binding_views = late_binding_views or frozenset()

    def execute(self, sql: str, params: Any = None) -> RedshiftDialectShim:
        flat = " ".join(sql.lower().split())
        self._rows = None

        if flat.startswith(("show table ", "show view ")):
            self._rows = self._show_table(sql)
        elif "svv_redshift_tables" in flat:
            self._rows = self._svv_tables()
        elif "svv_redshift_columns" in flat and "sortkey <> 0" in flat:
            self._rows = self._svv_physical_layout(params)
        elif "svv_redshift_columns" in flat and "lower(column_name)" in flat:
            self._rows = self._svv_resolve_column(params)
        elif "svv_redshift_columns" in flat:
            self._rows = self._svv_columns(params)
        elif "svv_table_info" in flat:
            self._rows = self._svv_table_info(params)
        elif flat == "select db_collation()":
            self._rows = [("case_sensitive",)]
        elif "pg_rewrite" in flat and "pg_depend" in flat:
            self._rows = self._view_dependencies(sql)
        elif params is None:
            self._cursor.execute(cast(LiteralString, _to_postgres(sql)))
        else:
            self._cursor.execute(cast(LiteralString, _to_postgres(sql)), params)

        return self

    def fetchall(self) -> list[Any]:
        return self._rows if self._rows is not None else self._cursor.fetchall()

    def fetchone(self) -> Any:
        if self._rows is None:
            return self._cursor.fetchone()

        return self._rows[0] if self._rows else None

    def close(self) -> None:
        self._cursor.close()

    def _show_table(self, sql: str) -> list[tuple[Any, ...]]:
        """Fabricate a minimal `CREATE TABLE`/`CREATE VIEW` text from `information_schema` - enough
        for `extract_ddl`'s round trip, raising on a mismatch so the adapter's fallback is proven.
        """

        is_table_stmt = sql.lower().lstrip().startswith("show table")
        schema, table = re.findall(r'"([^"]+)"', sql)
        relkind_row = self._cursor.execute(
            "SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s",
            (schema, table),
        ).fetchone()

        if relkind_row is None:
            return [(None,)]

        is_view = relkind_row[0] in ("v", "m")

        if is_table_stmt and is_view:
            raise RuntimeError(f"SHOW TABLE issued against a view: {schema}.{table}")

        if not is_table_stmt and not is_view:
            raise RuntimeError(f"SHOW VIEW issued against a table: {schema}.{table}")

        rows = self._cursor.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table),
        ).fetchall()

        if not rows:
            return [(None,)]

        columns = ",\n".join(f'  "{name}" {dtype}' for name, dtype in rows)
        kind = "VIEW" if is_view else "TABLE"

        return [(f'CREATE {kind} "{schema}"."{table}" (\n{columns}\n);\n',)]

    def _svv_tables(self) -> list[tuple[Any, ...]]:
        """`is_matview` rides on Postgres's real `relkind = 'm'` - the adapter joins `STV_MV_INFO`
        for the same fact on a real cluster.
        """

        return self._cursor.execute(
            """
            SELECT n.nspname, c.relname, CASE WHEN c.relkind = 'v' THEN 'VIEW' ELSE 'TABLE' END,
                   c.relkind = 'm'
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'v', 'm')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY n.nspname, c.relname
            """,
        ).fetchall()

    def _svv_columns(self, params: Any) -> list[tuple[Any, ...]]:
        schema, table = params

        return self._cursor.execute(
            "SELECT column_name, ordinal_position, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (schema, table),
        ).fetchall()

    def _svv_resolve_column(self, params: Any) -> list[tuple[Any, ...]]:
        """`resolve_column`'s own read, over real Postgres rows, not an invented spelling."""

        schema, table, column = params

        return self._cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND LOWER(column_name) = %s",
            (schema, table, column),
        ).fetchall()

    def _svv_physical_layout(self, params: Any) -> list[tuple[Any, ...]]:
        """No native SORTKEY concept on Postgres: fabricate from `sortkey_by_table`, empty by
        default - the contract schema declares no sort key on any of its tables.
        """

        schema, table = params
        declared = self.sortkey_by_table.get(f"{schema}.{table}", ())

        return [(name, sign * (i + 1)) for i, (name, sign) in enumerate(declared)]

    def _view_dependencies(self, sql: str) -> list[tuple[Any, ...]]:
        """Real rows from Postgres's catalog, with any `late_binding_views` entry collapsed to the
        all-null unresolved row a genuine late-binding view produces.
        """

        rows = self._cursor.execute(cast(LiteralString, _to_postgres(sql))).fetchall()

        if not self.late_binding_views:
            return rows

        out: list[tuple[Any, ...]] = []
        seen_late_binding: set[str] = set()

        for view_schema, view_name, resolved, source_schema, source_name in rows:
            key = f"{view_schema}.{view_name}"

            if key in self.late_binding_views:
                if key not in seen_late_binding:
                    out.append((view_schema, view_name, False, None, None))
                    seen_late_binding.add(key)
            else:
                out.append((view_schema, view_name, resolved, source_schema, source_name))

        return out

    def _svv_table_info(self, params: Any) -> list[tuple[Any, ...]]:
        schema, table = params
        row = self._cursor.execute(
            "SELECT reltuples::bigint FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s "
            "AND c.reltuples >= 0",
            (schema, table),
        ).fetchone()

        return [(row[0],)] if row else []


@pytest.fixture
def redshift_postgres_connection(
    postgres_cluster: PostgresCluster,
) -> Iterator[RedshiftDialectShim]:
    """Fresh Postgres database, seeded with the contract schema, wrapped in the Redshift shim."""

    db_name = f"contract_rs_{secrets.token_hex(4)}"
    admin_creds = {
        "host": "127.0.0.1",
        "port": str(postgres_cluster.port),
        "database": "postgres",
        "user": postgres_cluster.superuser,
        "password": "",
    }
    _exec_admin(admin_creds, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    db_creds = {**admin_creds, "database": db_name}
    _seed_contract_schema(db_creds)

    conn = psycopg.connect(
        host=db_creds["host"],
        port=int(db_creds["port"]),
        dbname=db_creds["database"],
        user=db_creds["user"],
        password=db_creds["password"],
        autocommit=True,
    )
    # `pg_class.reltuples` (what `_svv_table_info` reads) stays 0 until analyzed - the same
    # staleness `_seed_contract_schema_mysql`'s own `ANALYZE TABLE` works around.
    conn.execute("ANALYZE")

    try:
        yield RedshiftDialectShim(conn)
    finally:
        conn.close()
        _exec_admin(
            admin_creds,
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db_name)),
        )


# Per-test schema on a session-scoped local PySpark + Delta session.


class SparkCursor:
    """DB-API-shaped cursor over one `SparkSession` - `spark.sql()` takes no params argument,
    so `?` binds are substituted client-side (see `_render_params`).

    This reimplements the connector's native binding rather than calling through it, so binding
    here does not prove the real driver binds it - `test_dialect_guard.py` is what proves that.
    """

    def __init__(self, spark: Any) -> None:
        self._spark = spark
        self._df: Any = None

    def execute(self, sql_text: str, params: Any = None) -> SparkCursor:
        rendered = sql_text if params is None else _render_params(sql_text, params, "?")
        self._df = self._spark.sql(rendered)

        return self

    def fetchall(self) -> list[Any]:
        return [tuple(row) for row in self._df.collect()] if self._df is not None else []

    def fetchone(self) -> Any:
        rows = self.fetchall()

        return rows[0] if rows else None

    def close(self) -> None:
        self._df = None

    @property
    def description(self) -> list[tuple[str, ...]]:
        if self._df is None:
            return []

        return [(name,) for name in self._df.schema.names]


def _render_params(sql_text: str, params: Any, placeholder: str) -> str:
    """Client-side stand-in for a driver's positional binding, shared by `SparkCursor` (`?`) and
    `BigqueryEmulatorCursor` (`%s`) - each passes its own adapter's real marker.
    """

    rendered = []

    for value in params:
        if isinstance(value, str):
            rendered.append("'" + value.replace("'", "''") + "'")
        else:
            rendered.append(str(value))

    parts = sql_text.split(placeholder)

    if len(parts) - 1 != len(rendered):
        raise ValueError(f"{sql_text!r} takes {len(parts) - 1} params, got {len(rendered)}")

    out = parts[0]

    for value, part in zip(rendered, parts[1:]):
        out += value + part

    return out


@pytest.fixture(scope="session")
def databricks_spark_session() -> Iterator[Any]:
    """One local PySpark + Delta session per worker - startup runs 30-40s, so per-test is not
    viable. No emulator exists; this proves dialect shape only, never real Unity Catalog.

    A container without a JVM gets one installed; a host without one skips, as does an Ivy
    resolver that cannot reach Maven - an absent substrate skips tests one by one, never the run.
    """

    import tempfile

    from tests._provisioning import SPARK_IVY_CACHE_PATH, ensure_java

    try:
        ensure_java()
    except RuntimeError as exc:
        pytest.skip(str(exc))

    from delta import configure_spark_with_delta_pip
    from pyspark.errors import PySparkRuntimeError
    from pyspark.sql import SparkSession

    # Spark's default warehouse would litter the repo tree every run, so it is routed under this
    # worker's temp space. The Ivy cache stays a fixed shared path, warmed once by `just install`.
    warehouse_dir = tempfile.mkdtemp(prefix="dbprint-spark-warehouse-")
    SPARK_IVY_CACHE_PATH.mkdir(parents=True, exist_ok=True)

    builder = (
        SparkSession.builder.master("local[2]")
        .appName("dbprint-contract-suite")
        .config("spark.jars.ivy", str(SPARK_IVY_CACHE_PATH))
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )
        .config("spark.sql.warehouse.dir", warehouse_dir)
        .config("spark.ui.enabled", "false")
        # Deliberately non-UTC: a session left on the JVM default cannot catch a renderer that
        # reinterprets the session zone as UTC rather than converting to it.
        .config("spark.sql.session.timeZone", "America/New_York")
    )

    try:
        spark = configure_spark_with_delta_pip(builder).getOrCreate()
    except PySparkRuntimeError as exc:
        pytest.skip(f"Databricks fixture could not start a Spark session: {exc}")

    spark.sparkContext.setLogLevel("ERROR")

    try:
        yield spark
    finally:
        spark.stop()


@pytest.fixture
def databricks_test_schema(databricks_spark_session: Any) -> Iterator[SparkCursor]:
    """Fresh Delta schema in the shared session, seeded with the contract schema."""

    spark = databricks_spark_session
    schema_name = f"contract_{secrets.token_hex(4)}"
    cursor = SparkCursor(spark)
    cursor.execute(f"CREATE SCHEMA IF NOT EXISTS `{schema_name}`")
    _seed_contract_schema_databricks(cursor, schema_name)
    cursor.execute(f"USE `{schema_name}`")

    try:
        yield cursor
    finally:
        cursor.execute(f"DROP SCHEMA IF EXISTS `{schema_name}` CASCADE")


class RecordedResponseCursor:
    """A DB-API cursor answering Unity Catalog's `information_schema`/`DESCRIBE ... AS JSON`
    statements from hand-transcribed rows, no local substrate existing to run them for real.

    Dispatch is by substring match on the lowercased statement text; nothing here reaches an
    engine, so every response is exactly what a test hands it.
    """

    def __init__(self, responses: dict[str, list[tuple[Any, ...]]] | None = None) -> None:
        self._responses = responses or {}
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> RecordedResponseCursor:
        del params
        flat = " ".join(sql.lower().split())

        if flat.startswith("select 1 from information_schema.tables where 1 = 0"):
            self._rows = []
        elif flat == "select current_catalog()":
            self._rows = self._responses.get("current_catalog", [("garden",)])
        elif "table_constraints" in flat:
            self._rows = self._responses.get("table_constraints", [])
        elif "key_column_usage" in flat and "referential_constraints" in flat:
            self._rows = self._responses.get("key_column_usage_fk", [])
        elif "information_schema.tables" in flat:
            self._rows = self._responses.get("tables", [])
        elif "information_schema.columns" in flat:
            self._rows = self._responses.get("columns", [])
        elif "key_column_usage" in flat:
            self._rows = self._responses.get("key_column_usage", [])
        elif "describe table extended" in flat and "as json" in flat:
            self._rows = self._responses.get("describe_extended_json", [])
        elif flat == "set spark.sql.session.collation.default":
            self._rows = self._responses.get(
                "session_collation",
                [("spark.sql.session.collation.default", "UTF8_BINARY")],
            )
        else:
            raise AssertionError(f"RecordedResponseCursor: no canned response for {sql!r}")

        return self

    def fetchall(self) -> list[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        pass


def _seed_contract_schema_databricks(cursor: SparkCursor, schema: str) -> None:
    """Seed the shared herbarium/curator/viability_check shape, minus every constraint clause -
    OSS Delta refuses `PRIMARY KEY`/`FOREIGN KEY`/`UNIQUE` (measured) and has no `CREATE INDEX`.
    """

    cursor.execute(
        f"""
        CREATE TABLE `{schema}`.herbarium (
            id STRING, name STRING NOT NULL, rank STRING NOT NULL, code STRING NOT NULL
        ) USING DELTA
        """,
    )
    cursor.execute(
        f"""
        CREATE TABLE `{schema}`.curator (
            id STRING,
            email STRING NOT NULL COMMENT 'user-facing email address',
            herbarium_id STRING,
            is_active BOOLEAN NOT NULL,
            created_at TIMESTAMP NOT NULL,
            seed_count INT,
            withdrawn_at TIMESTAMP
        ) USING DELTA COMMENT 'Primary curator table'
        """,
    )
    cursor.execute(
        f"""
        INSERT INTO `{schema}`.herbarium (id, name, rank, code) VALUES
          ('00000000-0000-7000-8000-000000000001', 'Ashgrove',  'us', 'ASHGROVE'),
          ('00000000-0000-7000-8000-000000000002', 'Thornfield','eu', 'THORNFIELD'),
          ('00000000-0000-7000-8000-000000000003', 'Millbrook', 'us', 'MILLBROOK')
        """,
    )
    # seed_count carries the one seeded null (row 3); withdrawn_at is the all-null column.
    cursor.execute(
        f"""
        INSERT INTO `{schema}`.curator
          (id, email, herbarium_id, is_active, created_at, seed_count) VALUES
          ('00000000-0000-7000-8000-000000000011', 'a@x.com', '00000000-0000-7000-8000-000000000001', true,  TIMESTAMP '2026-01-01 00:00:00+00:00', 30),
          ('00000000-0000-7000-8000-000000000012', 'b@x.com', '00000000-0000-7000-8000-000000000001', true,  TIMESTAMP '2026-01-01 00:00:00+00:00', 31),
          ('00000000-0000-7000-8000-000000000013', 'c@x.com', '00000000-0000-7000-8000-000000000002', false, TIMESTAMP '2026-01-01 00:00:00+00:00', NULL)
        """,
    )
    cursor.execute(
        f"""
        CREATE TABLE `{schema}`.viability_check (
            id STRING,
            herbarium_id STRING,
            label STRING NOT NULL,
            score INT NOT NULL,
            observed_at TIMESTAMP NOT NULL,
            rank STRING NOT NULL,
            within_day_at TIMESTAMP NOT NULL,
            across_midnight_at TIMESTAMP NOT NULL,
            late_evening_at TIMESTAMP NOT NULL,
            scheduled_at TIMESTAMP NOT NULL,
            expires_at TIMESTAMP NOT NULL
        ) USING DELTA
        """,
    )
    cursor.execute(
        "INSERT INTO `" + schema + "`.viability_check "
        "(id, herbarium_id, label, score, observed_at, rank, within_day_at, across_midnight_at, late_evening_at, scheduled_at, expires_at) VALUES\n"
        + _wide_values_clause(explicit_utc=True),
    )


# Session-scoped goccy/bigquery-emulator container, one per xdist worker; per-test dataset.


class BigqueryEmulatorCursor:
    """DB-API-shaped cursor speaking the emulator's REST `jobs.query` endpoint directly - the
    real `google-cloud-bigquery` client's job polling hangs against this emulator (measured).
    """

    def __init__(self, base_url: str, project: str) -> None:
        self._base_url = base_url
        self._project = project
        self._fields: list[dict[str, Any]] = []
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> BigqueryEmulatorCursor:
        rendered = sql if params is None else _render_params(sql, params, "%s")
        body = json.dumps({"query": rendered, "useLegacySql": False}).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/bigquery/v2/projects/{self._project}/queries",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read())

        if "error" in payload:
            raise RuntimeError(f"{rendered!r}: {payload['error']}")

        self._fields = payload.get("schema", {}).get("fields", [])
        self._rows = [
            tuple(_convert_cell(cell["v"], field) for cell, field in zip(row["f"], self._fields))
            for row in payload.get("rows", [])
        ]

        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        pass

    @property
    def description(self) -> list[tuple[str, ...]]:
        return [(f["name"],) for f in self._fields]


def _convert_cell(value: Any, field: dict[str, Any]) -> Any:
    """One REST cell, decoded per its own field schema - recurses for RECORD (STRUCT) types,
    whose nested `fields` list carries the sub-schema the same shape applies to.
    """

    if value is None:
        return None

    bq_type = field["type"]
    mode = field.get("mode", "NULLABLE")

    if mode == "REPEATED":
        return [_convert_one(item["v"], bq_type, field) for item in value]

    return _convert_one(value, bq_type, field)


def _convert_one(value: Any, bq_type: str, field: dict[str, Any]) -> Any:
    if value is None:
        return None

    if bq_type == "RECORD":
        return {
            subfield["name"]: _convert_cell(cell["v"], subfield)
            for cell, subfield in zip(value["f"], field["fields"])
        }

    return _convert_scalar(value, bq_type)


def _convert_scalar(value: Any, bq_type: str) -> Any:
    if value is None:
        return None

    if bq_type in ("INTEGER", "INT64"):
        return int(value)

    if bq_type in ("FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"):
        return float(value)

    if bq_type in ("BOOLEAN", "BOOL"):
        return value in ("true", "True", True)

    if bq_type == "TIMESTAMP":
        # A scalar cell renders epoch seconds, the same type inside a REPEATED field a formatted
        # string instead - measured; an emulator REST-encoding inconsistency, not a format switch.
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except ValueError:
            return datetime.fromisoformat(value)

    if bq_type == "DATE":
        return date.fromisoformat(value)

    if bq_type == "DATETIME":
        return datetime.fromisoformat(value)

    return value


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))

        return s.getsockname()[1]


def _emulator_ready(base_url: str) -> bool:
    """Whether the server accepts HTTP at all - an error response proves it too, so the 404 a
    nonexistent project always draws must not fall through to the `URLError` branch.
    """

    try:
        with urllib.request.urlopen(f"{base_url}/bigquery/v2/projects/x/datasets", timeout=2):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(scope="session")
def bigquery_emulator() -> Iterator[tuple[str, str]]:
    """One `goccy/bigquery-emulator` container per xdist worker session, torn down on exit - it
    proves statement shape only, contradicting the vendor on sampling (ARCHITECTURE.md 10).

    Skips (never errors) when no container runtime is on PATH, the image cannot start, or the
    emulator never becomes ready - an absent substrate degrades each `bigquery` test individually.
    """

    import shutil

    if shutil.which("podman") is None:
        pytest.skip("no podman on PATH - BigQuery fixture needs a container runtime")

    project = "dbprint-test"
    port = _free_tcp_port()
    grpc_port = _free_tcp_port()
    container = "dbprint-bq-" + secrets.token_hex(4)
    base_url = f"http://127.0.0.1:{port}"

    # `--network=host` with the emulator's own `--port`, not `-p` publishing: measured on this
    # runtime, only the identity mapping (9050:9050) ever becomes reachable.
    try:
        subprocess.run(
            [
                "podman",
                "run",
                "-d",
                "--rm",
                "--network=host",
                "--name",
                container,
                "ghcr.io/goccy/bigquery-emulator:0.8.1",
                f"--project={project}",
                f"--port={port}",
                f"--grpc-port={grpc_port}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"bigquery-emulator container could not start: {exc.stderr.strip()}")

    try:
        deadline = time.monotonic() + 30

        while time.monotonic() < deadline:
            if _emulator_ready(base_url):
                break

            time.sleep(0.2)
        else:
            pytest.skip("bigquery-emulator did not become ready within 30s")

        yield base_url, project
    finally:
        subprocess.run(["podman", "stop", "-t", "0", container], check=False, capture_output=True)


@pytest.fixture
def bigquery_test_dataset(
    bigquery_emulator: tuple[str, str],
) -> Iterator[tuple[BigqueryEmulatorCursor, str]]:
    """Fresh dataset in the shared emulator, seeded with the contract schema - via the REST
    Datasets API, since `CREATE SCHEMA` reports success and creates nothing here (measured).
    """

    base_url, project = bigquery_emulator
    dataset = "contract_" + secrets.token_hex(4)
    request = urllib.request.Request(
        f"{base_url}/bigquery/v2/projects/{project}/datasets",
        data=json.dumps(
            {"datasetReference": {"projectId": project, "datasetId": dataset}},
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=10):
        pass

    cursor = BigqueryEmulatorCursor(base_url, project)
    _seed_contract_schema_bigquery(cursor, project, dataset)

    try:
        yield cursor, dataset
    finally:
        delete_request = urllib.request.Request(
            f"{base_url}/bigquery/v2/projects/{project}/datasets/{dataset}?deleteContents=true",
            method="DELETE",
        )

        try:
            with urllib.request.urlopen(delete_request, timeout=10):
                pass
        except urllib.error.URLError:
            pass


def _wide_values_clause_bigquery() -> str:
    """`_wide_values_clause`, with every temporal field wrapped in `TIMESTAMP(...)` - a bare
    literal resolves against a non-UTC zone on this emulator (measured), unlike the constructor.
    """

    temporal_positions = {4, 6, 7, 8, 9, 10}

    def render(row: WideRow) -> str:
        cells = [
            f"TIMESTAMP('{value}')" if i in temporal_positions else f"'{value}'"
            for i, value in enumerate(row)
        ]
        cells[3] = str(row[3])  # score: bare integer, never quoted

        return f"({', '.join(cells)})"

    return ",\n".join(render(row) for row in _wide_rows())


def _seed_contract_schema_bigquery(
    cursor: BigqueryEmulatorCursor,
    project: str,
    dataset: str,
) -> None:
    """Seed the shared herbarium/curator/viability_check shape, minus every constraint clause -
    they declare cleanly here but `INFORMATION_SCHEMA.TABLE_CONSTRAINTS` is absent (measured).
    """

    q = f"`{project}`.`{dataset}`"
    cursor.execute(
        f"""
        CREATE TABLE {q}.herbarium (
            id STRING, name STRING NOT NULL, rank STRING NOT NULL, code STRING NOT NULL
        )
        """,
    )
    cursor.execute(
        f"""
        CREATE TABLE {q}.curator (
            id STRING,
            email STRING NOT NULL,
            herbarium_id STRING,
            is_active BOOL NOT NULL,
            created_at TIMESTAMP NOT NULL,
            seed_count INT64,
            withdrawn_at TIMESTAMP
        )
        """,
    )
    cursor.execute(
        f"""
        INSERT INTO {q}.herbarium (id, name, rank, code) VALUES
          ('00000000-0000-7000-8000-000000000001', 'Ashgrove',  'us', 'ASHGROVE'),
          ('00000000-0000-7000-8000-000000000002', 'Thornfield','eu', 'THORNFIELD'),
          ('00000000-0000-7000-8000-000000000003', 'Millbrook', 'us', 'MILLBROOK')
        """,
    )
    cursor.execute(
        f"""
        INSERT INTO {q}.curator
          (id, email, herbarium_id, is_active, created_at, seed_count) VALUES
          ('00000000-0000-7000-8000-000000000011', 'a@x.com', '00000000-0000-7000-8000-000000000001', true,  TIMESTAMP '2026-01-01 00:00:00', 30),
          ('00000000-0000-7000-8000-000000000012', 'b@x.com', '00000000-0000-7000-8000-000000000001', true,  TIMESTAMP '2026-01-01 00:00:00', 31),
          ('00000000-0000-7000-8000-000000000013', 'c@x.com', '00000000-0000-7000-8000-000000000002', false, TIMESTAMP '2026-01-01 00:00:00', NULL)
        """,
    )
    cursor.execute(
        f"""
        CREATE TABLE {q}.viability_check (
            id STRING,
            herbarium_id STRING,
            label STRING NOT NULL,
            score INT64 NOT NULL,
            observed_at TIMESTAMP NOT NULL,
            rank STRING NOT NULL,
            within_day_at TIMESTAMP NOT NULL,
            across_midnight_at TIMESTAMP NOT NULL,
            late_evening_at TIMESTAMP NOT NULL,
            scheduled_at TIMESTAMP NOT NULL,
            expires_at TIMESTAMP NOT NULL
        )
        """,
    )
    cursor.execute(
        f"INSERT INTO {q}.viability_check "
        "(id, herbarium_id, label, score, observed_at, rank, within_day_at, across_midnight_at, late_evening_at, scheduled_at, expires_at) VALUES\n"
        + _wide_values_clause_bigquery(),
    )


# Reference fixture for the mock (exercises every classification + FK + index + comments).


REFERENCE_FIXTURE: dict[str, MockTable] = {
    "garden.seedbank.curator": MockTable(
        type="table",
        namespace_path=("garden", "seedbank", "curator"),
        ddl="CREATE TABLE garden.seedbank.curator (id uuid PRIMARY KEY, email varchar(255));\n",
        columns=[
            ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
            ColumnMeta(
                name="email",
                sql_type="varchar(255)",
                nullable=False,
                default=None,
                ordinal=2,
            ),
            ColumnMeta(
                name="herbarium_id",
                sql_type="uuid",
                nullable=True,
                default=None,
                ordinal=3,
            ),
            ColumnMeta(
                name="is_active",
                sql_type="boolean",
                nullable=False,
                default="true",
                ordinal=4,
            ),
            ColumnMeta(
                name="created_at",
                sql_type="timestamp with time zone",
                nullable=False,
                default="now()",
                ordinal=5,
            ),
            ColumnMeta(
                name="seed_count",
                sql_type="integer",
                nullable=True,
                default=None,
                ordinal=6,
            ),
        ],
        relationships=[
            ForeignKeyMeta(
                column=("herbarium_id",),
                target_table="garden.seedbank.herbarium",
                target_column=("id",),
                on_delete="CASCADE",
                on_update="NO ACTION",
                constraint_name="curator_herbarium_id_fkey",
            ),
        ],
        indexes=[
            IndexMeta(name="curator_email_idx", columns=("email",), unique=False, type="btree"),
        ],
        comments=CommentsMeta(
            table="Primary curator table",
            columns={"email": "user-facing email address"},
        ),
        stats={
            "id": ColumnStats(
                sql_type="uuid",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=250_000,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=(
                    ValueCount(value="00000000-0000-7000-8000-000000000001", count=1),
                    ValueCount(value="00000000-0000-7000-8000-000000000002", count=1),
                ),
                values_coverage=0.000008,
                inferred=Inferred(looks_like="uuid", candidate_key=True),
            ),
            "email": ColumnStats(
                sql_type="varchar(255)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=250_000,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=(
                    ValueCount(value="user01@example.com", count=1),
                    ValueCount(value="user02@example.com", count=1),
                ),
                values_coverage=0.000008,
                inferred=Inferred(looks_like="email", candidate_key=True),
            ),
            "herbarium_id": ColumnStats(
                sql_type="uuid",
                nullable=True,
                null_count=120,
                null_rate=0.00048,
                cardinality=12_000,
                cardinality_ratio=0.048,
                cardinality_method="exact",
                values=(
                    ValueCount(value="00000000-0000-7000-8000-000000000001", count=523),
                    ValueCount(value="00000000-0000-7000-8000-000000000002", count=420),
                ),
                values_coverage=0.003774,
                distribution="long_tail",
                length=Length(min=36, max=36, avg=36.0, p95=36.0),
            ),
            "is_active": ColumnStats(
                sql_type="boolean",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=2,
                cardinality_ratio=0.000008,
                cardinality_method="exact",
                values=(
                    ValueCount(value=True, count=240000),
                    ValueCount(value=False, count=10000),
                ),
                values_coverage=1.0,
            ),
            "created_at": ColumnStats(
                sql_type="timestamp with time zone",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=250_000,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                range=Range(
                    min="2020-01-01T00:00:00Z",
                    max="2026-05-17T22:48:00Z",
                    span_days=2328,
                ),
                percentiles={
                    "p01": "2020-03-15T10:11:12Z",
                    "p25": "2022-06-04T08:00:00Z",
                    "p50": "2024-01-15T12:00:00Z",
                    "p75": "2025-08-22T18:30:00Z",
                    "p99": "2026-05-10T11:22:33Z",
                },
                distribution="uniform",
                values=(
                    ValueCount(value="2021-03-10T00:00:00Z", count=1),
                    ValueCount(value="2023-07-04T00:00:00Z", count=1),
                ),
            ),
            "seed_count": ColumnStats(
                sql_type="integer",
                nullable=True,
                null_count=4200,
                null_rate=0.0168,
                cardinality=120,
                cardinality_ratio=0.00048,
                cardinality_method="exact",
                range=Range(min=18, max=137, span_days=None),
                percentiles={"p01": 19, "p25": 28, "p50": 41, "p75": 56, "p99": 89},
                mean=42.3,
                sum=10397340.0,
                distribution="imbalanced",
                values=(
                    ValueCount(value=19, count=1),
                    ValueCount(value=28, count=1),
                ),
            ),
        },
        samples={
            "email": [f"user{i}@example.com" for i in range(50)],
            "id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(50)],
        },
        row_count=250_000,
        row_count_estimate=250_000,
        unique_keys=[
            UniqueKeyMeta(columns=("id",), primary=True),
            UniqueKeyMeta(columns=("email",)),
        ],
    ),
    "garden.seedbank.herbarium": MockTable(
        type="table",
        namespace_path=("garden", "seedbank", "herbarium"),
        ddl=(
            "CREATE TABLE garden.seedbank.herbarium "
            "(id uuid PRIMARY KEY, code varchar(16) NOT NULL);\n"
        ),
        columns=[
            ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
            ColumnMeta(
                name="code",
                sql_type="varchar(16)",
                nullable=False,
                default=None,
                ordinal=2,
            ),
        ],
        relationships=[],
        # No index entry for `code`: the mock states declared-unique directly, not a backing index.
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "id": ColumnStats(
                sql_type="uuid",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=12_000,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=(
                    ValueCount(value="00000000-0000-7000-8000-000000000001", count=1),
                    ValueCount(value="00000000-0000-7000-8000-000000000002", count=1),
                ),
                values_coverage=0.000167,
                inferred=Inferred(looks_like="uuid", candidate_key=True),
            ),
            "code": ColumnStats(
                sql_type="varchar(16)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=12_000,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=(
                    ValueCount(value="ASHGROVE", count=1),
                    ValueCount(value="THORNFIELD", count=1),
                ),
                values_coverage=0.000167,
                inferred=Inferred(candidate_key=True),
            ),
        },
        samples={},
        row_count=12_000,
        unique_keys=[
            UniqueKeyMeta(columns=("id",), primary=True),
            UniqueKeyMeta(columns=("code",)),
        ],
    ),
    "fixture.staging.active_curators": MockTable(
        type="view",
        namespace_path=("fixture", "staging", "active_curators"),
        ddl="CREATE VIEW fixture.staging.active_curators AS SELECT 1;\n",
        columns=[],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={},
        samples={},
    ),
}


@pytest.fixture
def empty_stats_config() -> Any:
    """StatisticsConfig with defaults - the mock ignores it but the contract takes it."""

    from dbprint.config import StatisticsConfig

    return StatisticsConfig()
