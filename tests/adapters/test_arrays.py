"""An array is descended through `[*]` (SPEC 2.2.18): its elements profiled, its shape counted."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import duckdb
import pytest

from dbprint.adapters import (
    BigqueryAdapter,
    ClickhouseAdapter,
    DatabricksAdapter,
    PostgresAdapter,
)
from dbprint.adapters.base import PartSource
from dbprint.adapters.dialect import Vendor
from dbprint.adapters.duckdb import DuckdbAdapter
from dbprint.adapters.sql_layout import select_from
from dbprint.config import StatisticsConfig
from dbprint.config.project import RedactRule
from tests.adapters._composites import duckdb_print, generate, parts_of, psql, snowflake
from tests.adapters._credentials import DATABRICKS_CREDS
from tests.adapters._dialects import STATS_MODULES, foreign_fragments, install_recorder
from tests.adapters._sql_style import alias_violations, layout_violations, violations


class TestDuckdb:
    def test_tags_publish_their_elements_their_sizes_and_their_empties(
        self,
        tmp_path: Path,
    ) -> None:
        tags = duckdb_print(
            tmp_path,
            "CREATE TABLE seed_lot (lot_no INTEGER, tags VARCHAR[])",
            "INSERT INTO seed_lot VALUES (1, ['gift', 'sale']), (2, []), (3, NULL), (4, ['sale', NULL])",
        )["seed_lot"]["tags"]
        element = tags["parts"]["[*]"]

        assert tags["classification"] == "composite"
        assert tags["size"]["min"] == 0
        assert tags["size"]["max"] == 2
        assert tags["empty_count"] == 1
        assert tags["cardinality"] == 3
        assert tags["parts_found"] == 1
        assert element["occurrences"] == 4
        assert element["null_count"] == 1
        assert element["classification"] == "categorical"
        assert element["values"] == [{"value": "sale", "count": 2}, {"value": "gift", "count": 1}]

    def test_a_nested_array_reaches_its_inner_elements_unless_the_depth_stops_it(
        self,
        tmp_path: Path,
    ) -> None:
        statements = (
            "CREATE TABLE reading (reading_id INTEGER, grid INTEGER[][])",
            "INSERT INTO reading VALUES (1, [[1, 2], [3]]), (2, [[]]), (3, [[4]])",
        )
        grid = duckdb_print(tmp_path / "deep", *statements)["reading"]["grid"]
        shallow = duckdb_print(
            tmp_path / "shallow",
            *statements,
            statistics=StatisticsConfig(max_part_depth=1),
        )["reading"]["grid"]

        assert grid["parts"]["[*]"]["classification"] == "composite"
        assert grid["parts"]["[*]"]["empty_count"] == 1
        assert grid["parts"]["[*][*]"]["occurrences"] == 4
        assert list(shallow["parts"]) == ["[*]"]
        assert shallow["parts_found"] == 1

    def test_an_array_of_records_lists_its_elements_without_their_members(
        self,
        tmp_path: Path,
    ) -> None:
        items = duckdb_print(
            tmp_path,
            "CREATE TABLE order_line (line_id INTEGER, items STRUCT(sku VARCHAR, qty INTEGER)[])",
            "INSERT INTO order_line VALUES (1, [{'sku': 'a', 'qty': 1}]), (2, [])",
        )["order_line"]["items"]

        assert items["classification"] == "composite"
        assert items["parts"]["[*]"]["classification"] in ("unsupported", "composite")

    def test_an_embedding_is_an_array_with_a_norm_and_no_element_list(
        self,
        tmp_path: Path,
    ) -> None:
        emb = duckdb_print(
            tmp_path,
            "CREATE TABLE passage (passage_id INTEGER, emb FLOAT[4])",
            "INSERT INTO passage SELECT i, [0.6, 0.8, 0, 0]::FLOAT[4] FROM range(100) r(i)",
            "INSERT INTO passage SELECT i, [i * 0.001, 0, 0, 1]::FLOAT[4] FROM range(100, 200) r(i)",
        )["passage"]["emb"]
        element = emb["parts"]["[*]"]

        assert emb["classification"] == "composite"
        assert emb["size"]["min"] == emb["size"]["max"] == 4
        assert emb["norm"]["min"] == pytest.approx(1.0, abs=1e-5)
        assert emb["zero_count"] == 0
        assert element["classification"] == "numeric"
        assert {"range", "percentiles", "mean", "zero_count"} <= set(element)
        assert not {"values", "frequencies", "distribution"} & set(element)

    def test_an_embeddings_elements_are_never_ranked(self, tmp_path: Path) -> None:
        database = tmp_path / "garden.duckdb"
        con = duckdb.connect(str(database))
        con.execute("CREATE TABLE passage (passage_id INTEGER, emb FLOAT[4])")
        con.execute(
            "INSERT INTO passage SELECT i, [i * 0.001, 0, 0, 1]::FLOAT[4] FROM range(200) r(i)",
        )
        con.close()
        adapter = DuckdbAdapter({"database": str(database)})
        adapter.connect()
        recorder = install_recorder(adapter)

        try:
            fqn = next(t.fqn for t in adapter.list_tables(include=["*"], exclude=[]))
            columns = adapter.introspect_columns(fqn)
            counts, _ = adapter.compute_base_statistics(fqn, columns, StatisticsConfig())
            adapter.profile_parts(fqn, columns, StatisticsConfig(), counts)
        finally:
            adapter.close()

        element_reads = [s for s in recorder.flattened() if "unnest(" in s]

        assert element_reads
        assert not [s for s in element_reads if "group by" in s]

    def test_a_float_array_with_few_values_lists_them(self, tmp_path: Path) -> None:
        element = duckdb_print(
            tmp_path,
            "CREATE TABLE weight (weight_id INTEGER, steps DOUBLE[])",
            "INSERT INTO weight SELECT i, [0.5, 1.0] FROM range(30) r(i)",
        )["weight"]["steps"]["parts"]["[*]"]

        assert element["classification"] == "categorical"
        assert sorted(v["value"] for v in element["values"]) == [0.5, 1.0]

    def test_descent_off_leaves_the_array_unsupported(self, tmp_path: Path) -> None:
        tags = duckdb_print(
            tmp_path,
            "CREATE TABLE seed_lot (lot_no INTEGER, tags VARCHAR[])",
            "INSERT INTO seed_lot VALUES (1, ['gift'])",
            statistics=StatisticsConfig(max_parts=0),
        )["seed_lot"]["tags"]

        assert tags["classification"] == "unsupported"
        assert not {"cardinality", "size", "parts"} & set(tags)

    def test_a_rule_on_the_column_marks_its_elements(self, tmp_path: Path) -> None:
        tags = duckdb_print(
            tmp_path,
            "CREATE TABLE seed_lot (lot_no INTEGER, tags VARCHAR[])",
            "INSERT INTO seed_lot VALUES (1, ['gift', 'sale'])",
            redact=(RedactRule(columns=("*.seed_lot.tags",), with_="mask"),),
        )["seed_lot"]["tags"]

        assert "redacted" not in tags
        assert tags["parts"]["[*]"]["redacted"] == "mask"

    def test_an_array_of_emails_is_caught_by_a_looks_like_rule(self, tmp_path: Path) -> None:
        element = duckdb_print(
            tmp_path,
            "CREATE TABLE grower (grower_id INTEGER, contacts VARCHAR[])",
            "INSERT INTO grower SELECT i, ['grower' || i || '@example.invalid'] FROM range(40) r(i)",
            redact=(RedactRule(looks_like=("email",), with_="hash"),),
        )["grower"]["contacts"]["parts"]["[*]"]

        assert element["inferred"]["looks_like"] == "email"
        assert element["redacted"] == "hash"


def test_a_postgres_array_reads_every_dimension_through_one_element_part(
    postgres_test_db: dict[str, str],
    tmp_path: Path,
) -> None:
    with psql(postgres_test_db) as conn:
        conn.execute("CREATE TABLE public.plot (plot_id integer, grid integer[], tags text[])")
        conn.execute(
            "INSERT INTO public.plot VALUES (1, '{{1,2},{3,4}}', '{gift,sale}'), "
            "(2, '{}', '{}'), (3, NULL, '{sale}')",
        )

    columns = generate(PostgresAdapter(postgres_test_db), "postgres", tmp_path, "*.plot")

    assert list(columns["grid"]["parts"]) == ["[*]"]
    assert columns["grid"]["parts"]["[*]"]["occurrences"] == 4
    assert columns["grid"]["size"]["max"] == 4
    assert columns["grid"]["empty_count"] == 1
    assert columns["tags"]["parts"]["[*]"]["classification"] == "categorical"


def test_a_clickhouse_array_counts_its_null_elements(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.plot (plot_id UInt32, tags Array(Nullable(String))) ENGINE = Memory",
    )
    cursor.execute(
        "INSERT INTO seedbank.plot VALUES (1, ['gift', NULL]), (2, []), (3, ['sale'])",
    )
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    tags = generate(adapter, "clickhouse", tmp_path, "*.plot")["tags"]

    assert tags["null_count"] == 0
    assert tags["empty_count"] == 1
    assert tags["parts"]["[*]"]["occurrences"] == 3
    assert tags["parts"]["[*]"]["null_count"] == 1


def test_a_bigquery_array_is_descended(bigquery_test_dataset: Any) -> None:
    cursor, dataset = bigquery_test_dataset
    cursor.execute(f"CREATE TABLE `{dataset}`.plot (plot_id INT64, tags ARRAY<STRING>)")
    cursor.execute(
        f"INSERT INTO `{dataset}`.plot (plot_id, tags) VALUES "
        "(1, ['gift', 'sale']), (2, []), (3, ['sale'])",
    )
    adapter = BigqueryAdapter(
        {"project": "dbprint-test", "dataset": dataset},
        cursor_factory=lambda _params: cursor,
    )
    tags = parts_of(adapter, "plot", "tags")

    assert tags.cardinality == 3
    assert tags.empty_count == 1
    assert [(p.path, p.occurrences) for p in tags.parts] == [("[*]", 3)]


def test_a_databricks_array_is_descended(databricks_test_schema: Any) -> None:
    cursor = databricks_test_schema
    cursor.execute("CREATE TABLE plot (plot_id INT, tags ARRAY<STRING>) USING DELTA")
    cursor.execute(
        "INSERT INTO plot VALUES (1, array('gift', 'sale')), (2, array()), (3, array('sale'))",
    )
    adapter = DatabricksAdapter(DATABRICKS_CREDS, cursor_factory=lambda _params: cursor)
    tags = parts_of(adapter, "plot", "tags")

    assert tags.empty_count == 1
    assert [(p.path, p.occurrences) for p in tags.parts] == [("[*]", 3)]
    assert tags.parts[0].stats.values is not None


def test_a_snowflake_array_is_descended_to_its_innermost_elements() -> None:
    setup = (
        "CREATE TABLE seedbank.plot (plot_id INTEGER, tags VARCHAR[], grid INTEGER[][])",
        (
            "INSERT INTO seedbank.plot VALUES (1, ['gift', 'sale'], [[1, 2], [3]]), "
            "(2, [], [[4]]), (3, ['sale'], NULL)"
        ),
    )
    tags = parts_of(snowflake(*setup), "plot", "tags")
    grid = parts_of(snowflake(*setup), "plot", "grid")

    assert tags.empty_count == 1
    assert [(p.path, p.occurrences) for p in tags.parts] == [("[*]", 3)]
    assert [v.value for v in tags.parts[0].stats.values or ()] == ["sale", "gift"]
    assert [(p.path, p.occurrences) for p in grid.parts] == [("[*]", 3), ("[*][*]", 4)]


@pytest.mark.parametrize(
    ("vendor", "sql_type"),
    [
        ("postgres", "text[]"),
        ("duckdb", "VARCHAR[]"),
        ("snowflake", "ARRAY"),
        ("bigquery", "ARRAY<STRING>"),
        ("databricks", "array<string>"),
        ("clickhouse", "Array(String)"),
    ],
)
def test_the_array_reads_speak_their_own_dialect(vendor: Vendor, sql_type: str) -> None:
    module: ModuleType = STATS_MODULES[vendor]
    arrays = module.ARRAYS
    node = PartSource("", sql_type, "src_table src", "src.tags")
    element = "VARCHAR" if vendor == "snowflake" else "STRING"
    statements = [
        select_from([f"{arrays.size('src.tags')} AS sz"], "src_table src"),
        select_from([f"{arrays.distinct('src.tags')} AS d"], "src_table src"),
        select_from(["COUNT(1)"], arrays.elements(node, element)),
    ]

    for statement in statements:
        assert foreign_fragments(statement, vendor) == []
        assert violations(statement, vendor) + alias_violations(statement, vendor) == []
        assert layout_violations(statement, vendor) == []
