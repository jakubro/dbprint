"""Every published value is a SPEC 2.2.4 scalar, whatever object a driver decodes a column into.

Sweeps each driver's decoding of awkward types, and checks live listed values select their rows.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from decimal import Decimal
from typing import Any, LiteralString, cast

import chdb.dbapi
import duckdb
import psycopg
import pyarrow as pa
import pytest
import redshift_connector.utils.type_utils as redshift_types
import yaml
from google.cloud.bigquery import SchemaField
from google.cloud.bigquery import _helpers as bigquery_helpers
from mysql.connector.constants import FieldFlag, FieldType
from mysql.connector.conversion import MySQLConverter
from psycopg.adapt import Transformer
from psycopg.pq import Format
from pyspark.sql import Row

from dbprint.adapters import StatisticsConfig
from dbprint.engine.yaml_dumper import dump_yaml
from dbprint.spec.rounding import UnrepresentableValue, measured_value
from tests.adapters.conftest import SQL_PARAMS, _adapter_factory_for, _mysql_exec_many
from tests.conftest import pg_connect


_SCALARS = (type(None), bool, int, float, Decimal, str)
_LOADED = (str, int, float, bool)
_BUCKET = [0] * 15 + [1] * 10 + [2] * 5


def _postgres(name: str, wire: bytes) -> Any:
    oid = psycopg.postgres.types.get(name)
    assert oid is not None

    return Transformer().get_loader(oid.oid, Format.TEXT).load(wire)


def _mysql(field_type: int, flags: int, wire: bytes) -> Any:
    return MySQLConverter().to_python(("v", field_type, None, None, None, None, 1, flags, 45), wire)


def _bigquery(field: SchemaField, wire: str) -> Any:
    return bigquery_helpers._row_tuple_from_json({"f": [{"v": wire}]}, [field])[0]


def _redshift(decoder: Callable[[bytes, int, int], Any], fmt: str, *fields: int) -> Any:
    data = struct.pack(fmt, *fields)

    return decoder(data, 0, len(data))


def _arrow(array: pa.Array) -> Any:
    return pa.table({"v": array}).to_pandas()["v"][0]


def _duckdb(expr: str) -> Any:
    row = duckdb.connect().execute(f"SELECT {expr}").fetchone()
    assert row is not None

    return row[0]


def _chdb(expr: str) -> Any:
    cursor = chdb.dbapi.connect().cursor()
    cursor.execute(f"SELECT {expr}")

    return cursor.fetchone()[0]


DECODED: dict[str, Callable[[], Any]] = {
    "postgres inet": lambda: _postgres("inet", b"10.0.0.1"),
    "postgres cidr": lambda: _postgres("cidr", b"10.0.0.0/16"),
    "postgres int4range": lambda: _postgres("int4range", b"[1,5)"),
    "postgres int4multirange": lambda: _postgres("int4multirange", b"{[0,1),[10,12)}"),
    "postgres interval": lambda: _postgres("interval", b"1 year 2 mons 3 days"),
    "postgres jsonb": lambda: _postgres("jsonb", b'{"a": 1}'),
    "postgres uuid": lambda: _postgres("uuid", b"00000000-0000-0000-0000-00000000000a"),
    "postgres timetz": lambda: _postgres("timetz", b"12:00:00+02"),
    "postgres macaddr": lambda: _postgres("macaddr", b"08:00:2b:01:02:03"),
    "mysql set": lambda: _mysql(FieldType.STRING, FieldFlag.SET, b"red,blue"),
    "mysql bit": lambda: _mysql(FieldType.BIT, 0, b"\x05"),
    "mysql time": lambda: _mysql(FieldType.TIME, 0, b"-01:00:00"),
    "bigquery interval": lambda: _bigquery(SchemaField("v", "INTERVAL"), "0-1 2 3:4:5"),
    "bigquery range": lambda: _bigquery(
        SchemaField("v", "RANGE", range_element_type="DATE"),
        "[2024-01-01, 2024-02-01)",
    ),
    "bigquery bytes": lambda: _bigquery(SchemaField("v", "BYTES"), "YWI="),
    "bigquery numeric": lambda: _bigquery(SchemaField("v", "NUMERIC"), "1.5"),
    "redshift year to month": lambda: _redshift(redshift_types.intervaly2m_recv_integer, "!i", 14),
    "redshift day to second": lambda: _redshift(
        redshift_types.intervald2s_recv_integer,
        "!qi",
        3_600_000_000,
        2,
    ),
    "redshift interval with months": lambda: _redshift(
        redshift_types.interval_recv_integer,
        "!qii",
        0,
        3,
        14,
    ),
    "databricks struct": lambda: _arrow(pa.array([{"a": 10}])),
    "databricks array": lambda: _arrow(pa.array([[1, 2]])),
    "databricks spark row": lambda: Row(a=10),
    "duckdb interval": lambda: _duckdb("INTERVAL '1 year 2 months 3 days'"),
    "duckdb uuid": lambda: _duckdb("'00000000-0000-0000-0000-00000000000a'::UUID"),
    "duckdb timetz": lambda: _duckdb("'12:00:00+02'::TIMETZ"),
    "duckdb map": lambda: _duckdb("MAP {'a': 1}"),
    "duckdb blob": lambda: _duckdb("'ab'::BLOB"),
    "clickhouse ipv4": lambda: _chdb("toIPv4('10.0.0.1')"),
    "clickhouse uuid": lambda: _chdb("toUUID('00000000-0000-0000-0000-00000000000a')"),
    "clickhouse fixed string": lambda: _chdb("toFixedString('ab', 2)"),
}


# The decodes with no scalar a print can carry: containers, ranges, intervals, bytes, JSON trees.
_REFUSED = frozenset(
    {
        "bigquery bytes",
        "bigquery interval",
        "bigquery range",
        "databricks array",
        "databricks spark row",
        "databricks struct",
        "duckdb blob",
        "duckdb map",
        "mysql set",
        "postgres cidr",
        "postgres inet",
        "postgres int4multirange",
        "postgres int4range",
        "postgres jsonb",
        "redshift day to second",
        "redshift interval with months",
        "redshift year to month",
    },
)


@pytest.mark.parametrize("case", sorted(DECODED))
def test_a_decoded_value_is_published_as_a_scalar_or_refused_by_name(case: str) -> None:
    decoded = DECODED[case]()

    if case in _REFUSED:
        with pytest.raises(UnrepresentableValue) as refused:
            measured_value(decoded, "values[0]")

        assert type(decoded).__qualname__ in str(refused.value)
        assert "producer defect" in str(refused.value)
    else:
        published = measured_value(decoded, "values[0]")

        assert type(published) in _SCALARS, f"{case}: {type(published)!r} passed through"
        assert type(yaml.safe_load(dump_yaml({"v": published}))["v"]) in _LOADED


@pytest.mark.parametrize("vendor", SQL_PARAMS)
def test_every_listed_string_like_value_selects_its_own_rows(
    vendor: str,
    request: pytest.FixtureRequest,
) -> None:
    table, matchers = _SEEDERS[vendor](request)
    adapter = _adapter_factory_for(request, vendor)()
    fqn = next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if t.fqn.endswith(".spelled")
    )
    columns = adapter.introspect_columns(fqn)
    stats = adapter.compute_statistics(fqn, columns, StatisticsConfig(), frozenset())[1]

    for column, matcher in matchers.items():
        listed = stats[column].values

        assert listed is not None, f"{vendor}.{column}: no value list"
        assert len(listed) == stats[column].cardinality == 3, f"{vendor}.{column}"

        for entry in listed:
            assert type(entry.value) in _SCALARS, f"{vendor}.{column}: {entry.value!r}"
            dump_yaml({"v": entry.value})
            predicate = matcher.format(col=column, lit=_literal(entry.value))
            rows = adapter.execute_query(f"SELECT COUNT(*) FROM {table} WHERE {predicate}")

            assert rows[0][0] == entry.count, f"{vendor}.{column} = {entry.value!r}"

    adapter.close()


def _literal(value: Any) -> str:
    text = str(value).replace("'", "''")

    return f"'{text}'"


def _rows(*columns: list[str]) -> list[tuple[Any, ...]]:
    return [(i, *(column[_BUCKET[i]] for column in columns)) for i in range(len(_BUCKET))]


def _values_clause(rows: list[tuple[Any, ...]], render: Callable[[int, str], str]) -> str:
    return ", ".join(
        "(" + ", ".join(str(v) if i == 0 else render(i, str(v)) for i, v in enumerate(row)) + ")"
        for row in rows
    )


_DUCKDB_SPANS = ["1 year", "1 year 2 months 3 days", "6 months"]
_LABELS = ["x0", "x1", "x2"]


def _seed_duckdb(request: pytest.FixtureRequest) -> tuple[str, dict[str, str]]:
    con = request.getfixturevalue("duckdb_native_connection")
    _seed_duckdb_family(con.execute)

    return "seedbank.spelled", {
        "span": "{col} = CAST({lit} AS INTERVAL)",
        "label": "{col} = {lit}",
    }


def _seed_snowflake(request: pytest.FixtureRequest) -> tuple[str, dict[str, str]]:
    shim = request.getfixturevalue("snowflake_duckdb_connection")
    shim.execute("CREATE TABLE seedbank.spelled (id INTEGER, label VARCHAR)")
    shim.execute(
        "INSERT INTO seedbank.spelled VALUES "
        + _values_clause(_rows(_LABELS), lambda _, v: _literal(v)),
    )

    return "seedbank.spelled", {"label": "{col} = {lit}"}


def _seed_duckdb_family(execute: Callable[..., Any]) -> None:
    execute("CREATE TABLE seedbank.spelled (id INTEGER, span INTERVAL, label VARCHAR)")
    execute(
        "INSERT INTO seedbank.spelled VALUES "
        + _values_clause(
            _rows(_DUCKDB_SPANS, _LABELS),
            lambda i, v: f"INTERVAL {_literal(v)}" if i == 1 else _literal(v),
        ),
    )


_POSTGRES_TYPES = {
    "host": ("inet", ["10.0.0.0", "10.0.0.1", "10.0.0.2"]),
    "subnet": ("cidr", ["10.0.0.0/16", "10.1.0.0/16", "10.2.0.0/16"]),
    "slot": ("int4range", ["[0,2)", "[1,3)", "[2,4)"]),
    "shift": ("int4multirange", ["{[0,1),[10,12)}", "{[1,2),[10,12)}", "{[2,3),[10,12)}"]),
    "term": ("interval", ["1 year", "1 year 2 mons 3 days", "6 mons"]),
    "label": ("text", _LABELS),
}


def _seed_postgres(request: pytest.FixtureRequest) -> tuple[str, dict[str, str]]:
    creds = request.getfixturevalue("postgres_test_db")
    columns = ", ".join(f"{name} {sql_type}" for name, (sql_type, _) in _POSTGRES_TYPES.items())
    types = [sql_type for sql_type, _ in _POSTGRES_TYPES.values()]
    rows = _rows(*(values for _, values in _POSTGRES_TYPES.values()))

    with pg_connect(creds) as conn:
        conn.execute(cast(LiteralString, f"CREATE TABLE seedbank.spelled (id integer, {columns})"))
        conn.execute(
            cast(
                LiteralString,
                "INSERT INTO seedbank.spelled VALUES "
                + _values_clause(rows, lambda i, v: f"CAST({_literal(v)} AS {types[i - 1]})"),
            ),
        )
        conn.execute("ANALYZE")

    return "seedbank.spelled", {
        name: f"{{col}} = CAST({{lit}} AS {sql_type})"
        for name, (sql_type, _) in _POSTGRES_TYPES.items()
    }


def _seed_redshift(request: pytest.FixtureRequest) -> tuple[str, dict[str, str]]:
    shim = request.getfixturevalue("redshift_postgres_connection")
    shim.execute("CREATE TABLE seedbank.spelled (id integer, term interval, label varchar(8))")
    shim.execute(
        "INSERT INTO seedbank.spelled VALUES "
        + _values_clause(
            _rows(["1 day", "2 days 3 hours", "6 hours"], _LABELS),
            lambda i, v: f"CAST({_literal(v)} AS interval)" if i == 1 else _literal(v),
        ),
    )
    shim.execute("ANALYZE")

    return "seedbank.spelled", {
        "term": "{col} = CAST({lit} AS interval)",
        "label": "{col} = {lit}",
    }


def _seed_mysql(request: pytest.FixtureRequest) -> tuple[str, dict[str, str]]:
    creds = request.getfixturevalue("mysql_test_db")
    rows = _rows(["red", "red,blue", "green"], ["happy", "sad", "calm"], _LABELS)
    _mysql_exec_many(
        int(creds["port"]),
        creds["database"],
        [
            (
                "CREATE TABLE spelled (id int primary key, palette SET('red','green','blue'), "
                "mood ENUM('happy','sad','calm'), label varchar(8))"
            ),
            "INSERT INTO spelled VALUES " + _values_clause(rows, lambda _, v: _literal(v)),
            "ANALYZE TABLE spelled",
        ],
    )

    return "spelled", {
        "palette": "{col} = {lit}",
        "mood": "{col} = {lit}",
        "label": "{col} = {lit}",
    }


def _seed_clickhouse(request: pytest.FixtureRequest) -> tuple[str, dict[str, str]]:
    cursor = request.getfixturevalue("clickhouse_native_connection")
    rows = _rows(
        ["10.0.0.0", "10.0.0.1", "10.0.0.2"],
        [f"00000000-0000-0000-0000-00000000000{k}" for k in "abc"],
        _LABELS,
    )
    cursor.execute(
        "CREATE TABLE seedbank.spelled (id Int32, host IPv4, tag UUID, label String) "
        "ENGINE = MergeTree ORDER BY (sipHash64(id), id) SAMPLE BY sipHash64(id)",
    )
    cursor.execute(
        "INSERT INTO seedbank.spelled VALUES " + _values_clause(rows, lambda _, v: _literal(v)),
    )

    return "seedbank.spelled", {
        "host": "{col} = toIPv4({lit})",
        "tag": "{col} = toUUID({lit})",
        "label": "{col} = {lit}",
    }


def _seed_bigquery(request: pytest.FixtureRequest) -> tuple[str, dict[str, str]]:
    cursor, dataset = request.getfixturevalue("bigquery_test_dataset")
    table = f"`dbprint-test`.`{dataset}`.spelled"
    cursor.execute(f"CREATE TABLE {table} (id INT64, label STRING)")
    cursor.execute(
        f"INSERT INTO {table} VALUES " + _values_clause(_rows(_LABELS), lambda _, v: _literal(v)),
    )

    return table, {"label": "{col} = {lit}"}


def _seed_databricks(request: pytest.FixtureRequest) -> tuple[str, dict[str, str]]:
    cursor = request.getfixturevalue("databricks_test_schema")
    cursor.execute("CREATE TABLE spelled (id INT, label STRING) USING DELTA")
    cursor.execute(
        "INSERT INTO spelled VALUES " + _values_clause(_rows(_LABELS), lambda _, v: _literal(v)),
    )

    return "spelled", {"label": "{col} = {lit}"}


_SEEDERS: dict[str, Callable[[pytest.FixtureRequest], tuple[str, dict[str, str]]]] = {
    "bigquery": _seed_bigquery,
    "clickhouse": _seed_clickhouse,
    "databricks": _seed_databricks,
    "duckdb": _seed_duckdb,
    "mysql": _seed_mysql,
    "postgres": _seed_postgres,
    "redshift": _seed_redshift,
    "snowflake": _seed_snowflake,
}
