"""Parameterized adapter fixture for the contract test battery.

`adapter_factory` covers the mock plus each DB-backed adapter's own substrate: Postgres on a
session-scoped local cluster (initdb + pg_ctl), Snowflake on an in-memory duckdb via
`SnowflakeDialectShim`. Each test gets its own freshly seeded database, torn down on exit.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any, LiteralString, cast

import duckdb
import psycopg
import pytest
from psycopg import sql

from dbprint.adapters import (
    Adapter,
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    ForeignKeyMeta,
    IndexMeta,
    Inferred,
    MockAdapter,
    MockTable,
    MysqlAdapter,
    PostgresAdapter,
    Range,
    SnowflakeAdapter,
    UniqueKeyMeta,
    ValueCount,
)
from tests.conftest import MysqlCluster, PostgresCluster


# Adapter factory parameterization.

PARAMS = ["mock", "postgres", "snowflake", "mysql"]
SQL_PARAMS = ["postgres", "snowflake", "mysql"]


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


def _wide_values_clause() -> str:
    """Render the wide rows as one multi-row VALUES list - one round trip per test."""

    return ",\n".join(
        "('{}', '{}', '{}', {}, '{}', '{}', '{}', '{}', '{}', '{}', '{}')".format(*row)
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


def _seed_contract_schema_duckdb(con: duckdb.DuckDBPyConnection) -> None:
    """Populate the duckdb instance with the three tables used by the contract suite.

    Mirrors the Postgres fixture, except FK actions stay NO ACTION: duckdb cannot parse CASCADE.
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
                distribution="imbalanced",
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
