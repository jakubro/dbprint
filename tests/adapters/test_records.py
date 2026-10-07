"""A record or union is descended through its members (SPEC 2.2.18), each profiled where held."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from dbprint.adapters import BigqueryAdapter, ClickhouseAdapter, DatabricksAdapter, PostgresAdapter
from dbprint.adapters.base import Member, PartSource
from dbprint.adapters.dialect import Vendor
from dbprint.adapters.sql_layout import select_from
from dbprint.config import StatisticsConfig
from dbprint.config.project import RedactRule
from tests.adapters._composites import duckdb_print, generate, parts_of, psql
from tests.adapters._credentials import DATABRICKS_CREDS
from tests.adapters._dialects import STATS_MODULES, foreign_fragments
from tests.adapters._sql_style import alias_violations, layout_violations, violations


_PLOT = (
    (
        "CREATE TABLE grower (grower_no INTEGER, plot STRUCT(town VARCHAR, code VARCHAR, "
        '"row label" VARCHAR, spot STRUCT(x DOUBLE, y DOUBLE)))'
    ),
    (
        "INSERT INTO grower SELECT i, CASE WHEN i % 10 = 0 THEN NULL ELSE {"
        "'town': ['north', 'south', 'east'][i % 3 + 1], "
        "'code': CASE WHEN i % 5 = 1 THEN NULL ELSE 'c' || (i % 40) END, "
        "'row label': 'r' || (i % 4), 'spot': {'x': i % 7, 'y': 1.5}} END FROM range(100) r(i)"
    ),
)
_FEE = "UNION(card VARCHAR, iban VARCHAR, cash BOOLEAN)"


class TestDuckdb:
    def test_a_nested_record_lists_every_member_counted_over_its_held_parents(
        self,
        tmp_path: Path,
    ) -> None:
        plot = duckdb_print(tmp_path, *_PLOT)["grower"]["plot"]
        parts = plot["parts"]

        assert plot["classification"] == "composite"
        assert not {"cardinality", "size"} & set(plot)
        assert set(parts) == {".town", ".code", '["row label"]', ".spot", ".spot.x", ".spot.y"}
        assert parts[".town"]["classification"] == "categorical"
        assert len(parts[".town"]["values"]) == 3
        assert parts[".code"]["occurrences"] == 90
        assert parts[".code"]["null_count"] == 20
        assert parts[".spot"]["classification"] == "composite"
        assert not {"cardinality", "size", "values"} & set(parts[".spot"])
        assert parts['["row label"]']["cardinality"] == 4

    def test_a_union_counts_each_member_where_its_tag_names_it(self, tmp_path: Path) -> None:
        fee = duckdb_print(
            tmp_path,
            f"CREATE TABLE levy (levy_no INTEGER, fee {_FEE})",
            f"INSERT INTO levy SELECT i, CASE WHEN i % 10 = 0 THEN NULL WHEN i % 3 = 0 "
            f"THEN union_value(card := CASE WHEN i % 9 = 0 THEN NULL ELSE 'k' || i END)::{_FEE} "
            f"ELSE union_value(iban := 'b' || (i % 6))::{_FEE} END FROM range(90) r(i)",
        )["levy"]["fee"]
        parts = fee["parts"]

        assert fee["classification"] == "composite"
        assert set(parts) == {".card", ".iban"}
        assert fee["parts_found"] == 2
        assert parts[".card"]["occurrences"] + parts[".iban"]["occurrences"] == 81
        assert parts[".card"]["null_count"] == 9

    def test_the_depth_cap_keeps_the_nested_record_as_a_count_profile(
        self,
        tmp_path: Path,
    ) -> None:
        plot = duckdb_print(tmp_path, *_PLOT, statistics=StatisticsConfig(max_part_depth=1))
        parts = plot["grower"]["plot"]["parts"]

        assert set(parts) == {".town", ".code", '["row label"]', ".spot"}
        assert parts[".spot"]["occurrences"] == 90

    def test_a_field_no_instance_holds_is_not_a_part(self, tmp_path: Path) -> None:
        plot = duckdb_print(
            tmp_path,
            'CREATE TABLE grower (grower_no INTEGER, plot STRUCT(town VARCHAR, "row ""b"" label" VARCHAR, '
            "spot STRUCT(x DOUBLE)))",
            "INSERT INTO grower VALUES (1, {'town': 'north', 'row \"b\" label': 'r1', 'spot': NULL}), "
            "(2, NULL)",
        )["grower"]["plot"]

        assert set(plot["parts"]) == {".town", '["row \\"b\\" label"]', ".spot"}
        assert plot["parts_found"] == 3

    def test_descent_off_leaves_the_record_unsupported(self, tmp_path: Path) -> None:
        plot = duckdb_print(tmp_path, *_PLOT, statistics=StatisticsConfig(max_parts=0))

        assert plot["grower"]["plot"]["classification"] == "unsupported"
        assert "parts" not in plot["grower"]["plot"]

    def test_an_email_member_is_caught_by_a_looks_like_rule(self, tmp_path: Path) -> None:
        contact = duckdb_print(
            tmp_path,
            "CREATE TABLE grower (grower_no INTEGER, contact STRUCT(mail VARCHAR, alias VARCHAR))",
            "INSERT INTO grower SELECT i, {'mail': 'g' || i || '@example.invalid', 'alias': 'a'} "
            "FROM range(40) r(i)",
            redact=(RedactRule(looks_like=("email",), with_="drop"),),
        )["grower"]["contact"]
        mail = contact["parts"][".mail"]

        assert mail["inferred"]["looks_like"] == "email"
        assert mail["redacted"] == "drop"
        assert "@example.invalid" not in "".join(
            p.read_text() for p in tmp_path.rglob("*") if p.is_file() and p.suffix != ".duckdb"
        )


def test_a_postgres_composite_counts_an_all_null_value_as_held(
    postgres_test_db: dict[str, str],
    tmp_path: Path,
) -> None:
    with psql(postgres_test_db) as conn:
        conn.execute("CREATE TYPE public.site_t AS (town text, code text)")
        conn.execute("CREATE TABLE public.lot (lot_no integer, site public.site_t)")
        conn.execute(
            "INSERT INTO public.lot VALUES (1, ROW('north', 'c1')), (2, ROW(NULL, NULL)), (3, NULL)",
        )

    site = generate(PostgresAdapter(postgres_test_db), "postgres", tmp_path, "*.lot")["site"]

    assert site["classification"] == "composite"
    assert site["null_count"] == 1
    assert site["parts"][".town"]["occurrences"] == 2
    assert site["parts"][".town"]["null_count"] == 1
    assert site["parts"][".code"]["null_count"] == 1


def test_clickhouse_tuples_and_unions_are_descended(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.lot (lot_no UInt32, site Tuple(town String, code Nullable(String)), "
        "pair Tuple(String, UInt8), tag Variant(UInt64, String), loose Dynamic) ENGINE = Memory",
    )
    cursor.execute(
        "INSERT INTO seedbank.lot SELECT number, ('north', if(number % 2 = 0, NULL, 'c')), "
        "('a', 1), if(number < 40, number::Variant(UInt64, String), "
        "if(number < 100, 's'::Variant(UInt64, String), NULL)), "
        "multiIf(number % 3 = 0, number::Int64::Dynamic, number % 3 = 1, 'x'::Dynamic, "
        "[number::Int64]::Dynamic) FROM numbers(110)",
    )
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    columns = generate(adapter, "clickhouse", tmp_path, "*.lot")

    assert set(columns["site"]["parts"]) == {".town", ".code"}
    assert columns["site"]["parts"][".code"]["null_count"] == 55
    assert set(columns["pair"]["parts"]) == {'["1"]', '["2"]'}
    assert columns["tag"]["classification"] == "composite"
    assert columns["tag"]["parts"][".UInt64"]["occurrences"] == 40
    assert columns["tag"]["parts"][".String"]["occurrences"] == 60
    assert columns["loose"]["classification"] == "composite"
    assert {".Int64", ".String", '["Array(Int64)"]'} <= set(columns["loose"]["parts"])


def test_a_clickhouse_nested_column_is_an_array_of_records(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute("SET flatten_nested = 0")
    cursor.execute(
        "CREATE TABLE seedbank.lot (lot_no UInt32, lines Nested(sku String, qty UInt8)) "
        "ENGINE = MergeTree ORDER BY lot_no",
    )
    cursor.execute("INSERT INTO seedbank.lot VALUES (1, [('a', 1), ('b', 2)]), (2, [])")
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    lines = generate(adapter, "clickhouse", tmp_path, "*.lot")["lines"]

    assert lines["classification"] == "composite"
    assert {"[*]", "[*].sku", "[*].qty"} <= set(lines["parts"])
    assert lines["parts"]["[*].sku"]["occurrences"] == 2


def test_a_bigquery_struct_is_descended(bigquery_test_dataset: Any) -> None:
    cursor, dataset = bigquery_test_dataset
    cursor.execute(
        f"CREATE TABLE `{dataset}`.lot (lot_no INT64, site STRUCT<town STRING, code STRING>)",
    )
    cursor.execute(
        f"INSERT INTO `{dataset}`.lot (lot_no, site) VALUES "
        "(1, STRUCT('north', 'c1')), (2, STRUCT('south', NULL)), (3, NULL)",
    )
    adapter = BigqueryAdapter(
        {"project": "dbprint-test", "dataset": dataset},
        cursor_factory=lambda _params: cursor,
    )
    site = parts_of(adapter, "lot", "site")

    assert {p.path: p.occurrences for p in site.parts} == {".town": 2, ".code": 2}


def test_a_databricks_struct_is_descended(databricks_test_schema: Any) -> None:
    cursor = databricks_test_schema
    cursor.execute(
        "CREATE TABLE lot (lot_no INT, site STRUCT<town: STRING, code: STRING>) USING DELTA",
    )
    cursor.execute(
        "INSERT INTO lot VALUES (1, named_struct('town', 'north', 'code', 'c1')), "
        "(2, named_struct('town', 'south', 'code', NULL)), (3, NULL)",
    )
    adapter = DatabricksAdapter(DATABRICKS_CREDS, cursor_factory=lambda _params: cursor)
    site = parts_of(adapter, "lot", "site")

    assert {p.path: p.occurrences for p in site.parts} == {".town": 2, ".code": 2}


@pytest.mark.parametrize(
    ("vendor", "sql_type", "member"),
    [
        ("postgres", "site_t", Member("town", "text")),
        ("duckdb", "STRUCT(town VARCHAR)", Member("town", "VARCHAR")),
        ("duckdb", "UNION(town VARCHAR)", Member("town", "VARCHAR", union=True)),
        ("bigquery", "STRUCT<town STRING>", Member("town", "STRING")),
        ("databricks", "struct<town:string>", Member("town", "string")),
        ("clickhouse", "Tuple(town String)", Member("town", "String", position=1)),
        ("clickhouse", "Tuple(String)", Member("1", "String", position=1)),
        ("clickhouse", "Variant(String)", Member("String", "String", union=True)),
        ("clickhouse", "Dynamic", Member("String", "String", union=True)),
    ],
)
def test_the_record_reads_speak_their_own_dialect(
    vendor: Vendor,
    sql_type: str,
    member: Member,
) -> None:
    module: ModuleType = STATS_MODULES[vendor]
    node = PartSource("", sql_type, "src_table src", "src.site")
    statement = select_from(["COUNT(1)"], module.RECORDS.source(node, member))

    assert foreign_fragments(statement, vendor) == []
    assert violations(statement, vendor) + alias_violations(statement, vendor) == []
    assert layout_violations(statement, vendor) == []
