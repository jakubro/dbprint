"""A map is descended through `[keys]` and one part per key (SPEC 2.2.18), keys read as entries."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from dbprint.adapters import ClickhouseAdapter, DatabricksAdapter, PostgresAdapter
from dbprint.adapters.base import PartSource, key_literal
from dbprint.adapters.dialect import Vendor
from dbprint.adapters.sql_layout import derived, select_from
from dbprint.config import StatisticsConfig
from dbprint.config.project import RedactRule
from tests.adapters._composites import duckdb_print, generate, parts_of, psql, snowflake
from tests.adapters._credentials import DATABRICKS_CREDS
from tests.adapters._dialects import STATS_MODULES, foreign_fragments
from tests.adapters._sql_style import alias_violations, layout_violations, violations


_LABELS = (
    "CREATE TABLE gauge (gauge_no INTEGER, labels MAP(VARCHAR, VARCHAR))",
    (
        "INSERT INTO gauge VALUES (1, MAP {'unit': 'C', 'site': 'north'}), "
        "(2, MAP {'unit': 'F', 'site': NULL}), (3, MAP {'unit': 'C'}), (4, MAP {}), (5, NULL)"
    ),
)
_AWKWARD = "it's \\ a\nkey"


class TestDuckdb:
    def test_a_map_publishes_its_key_set_and_each_key_over_the_maps_holding_it(
        self,
        tmp_path: Path,
    ) -> None:
        labels = duckdb_print(tmp_path, *_LABELS)["gauge"]["labels"]
        parts = labels["parts"]

        assert labels["classification"] == "composite"
        assert (labels["size"]["min"], labels["size"]["max"]) == (0, 2)
        assert labels["empty_count"] == 1
        assert "cardinality" not in labels
        assert labels["parts_found"] == 3
        assert list(parts) == ["[keys]", ".unit", ".site"]
        assert parts["[keys]"]["occurrences"] == 5
        assert parts["[keys]"]["null_count"] == 0
        assert parts["[keys]"]["values"] == [
            {"value": "unit", "count": 3},
            {"value": "site", "count": 2},
        ]
        assert (parts[".site"]["occurrences"], parts[".site"]["null_count"]) == (2, 1)
        assert parts[".site"]["values"] == [{"value": "north", "count": 1}]
        assert (parts[".unit"]["occurrences"], parts[".unit"]["null_count"]) == (3, 0)

    def test_integer_quoted_and_awkward_keys_are_spelled_and_read(self, tmp_path: Path) -> None:
        columns = duckdb_print(
            tmp_path,
            "CREATE TABLE gauge (gauge_no INTEGER, scale MAP(INTEGER, DOUBLE), "
            "tags MAP(VARCHAR, INTEGER))",
            f"INSERT INTO gauge VALUES (1, MAP {{3: 1.5, 7: 2.0}}, "
            f"MAP {{'user-id': 1, {_standard_string(_AWKWARD)}: 2}}), "
            "(2, MAP {3: 2.5}, MAP {'user-id': 3})",
        )["gauge"]

        assert columns["scale"]["parts"]["[keys]"]["values"] == [
            {"value": 3, "count": 2},
            {"value": 7, "count": 1},
        ]
        assert columns["scale"]["parts"]['["3"]']["occurrences"] == 2
        assert columns["scale"]["parts"]['["7"]']["occurrences"] == 1
        assert columns["tags"]["parts"]['["user-id"]']["occurrences"] == 2
        assert columns["tags"]["parts"]['["it\'s \\\\ a\\nkey"]']["occurrences"] == 1

    def test_a_nested_map_reaches_its_inner_keys(self, tmp_path: Path) -> None:
        deep = duckdb_print(
            tmp_path,
            "CREATE TABLE gauge (gauge_no INTEGER, deep MAP(VARCHAR, MAP(VARCHAR, INTEGER)))",
            "INSERT INTO gauge VALUES (1, MAP {'a': MAP {'b': 1}}), (2, MAP {'a': MAP {}})",
        )["gauge"]["deep"]

        assert set(deep["parts"]) == {"[keys]", ".a", ".a[keys]", ".a.b"}
        assert deep["parts"][".a"]["classification"] == "composite"
        assert deep["parts"][".a"]["empty_count"] == 1

    def test_the_key_cap_counts_every_key_it_cut(self, tmp_path: Path) -> None:
        wide = duckdb_print(
            tmp_path,
            "CREATE TABLE gauge (gauge_no INTEGER, wide MAP(VARCHAR, INTEGER))",
            "INSERT INTO gauge SELECT i, MAP {'k' || i: i} FROM range(1000) r(i)",
            statistics=StatisticsConfig(max_parts=5),
        )["gauge"]["wide"]

        assert len(wide["parts"]) == 5
        assert "[keys]" in wide["parts"]
        assert wide["parts_found"] == 1001

    def test_keys_that_look_like_emails_withhold_every_key_part(self, tmp_path: Path) -> None:
        hits = duckdb_print(
            tmp_path,
            "CREATE TABLE gauge (gauge_no INTEGER, hits MAP(VARCHAR, INTEGER))",
            "INSERT INTO gauge SELECT i, MAP {'g' || i || '@example.invalid': i} FROM range(40) r(i)",
            redact=(RedactRule(looks_like=("email",), with_="hash"),),
        )["gauge"]["hits"]

        assert list(hits["parts"]) == ["[keys]"]
        assert hits["parts"]["[keys]"]["inferred"]["looks_like"] == "email"
        assert hits["parts"]["[keys]"]["redacted"] == "hash"
        assert hits["parts_found"] == 41
        assert "@example.invalid" not in "".join(
            p.read_text() for p in tmp_path.rglob("*.yaml") if p.is_file()
        )

    def test_a_rule_on_the_column_masks_the_keys_and_withholds_their_parts(
        self,
        tmp_path: Path,
    ) -> None:
        labels = duckdb_print(
            tmp_path,
            *_LABELS,
            redact=(RedactRule(columns=("*.gauge.labels",), with_="mask"),),
        )["gauge"]["labels"]

        assert list(labels["parts"]) == ["[keys]"]
        assert labels["parts"]["[keys]"]["redacted"] == "mask"

    def test_a_key_listed_without_its_key_set_is_withheld(self, tmp_path: Path) -> None:
        solo = duckdb_print(
            tmp_path,
            "CREATE TABLE gauge (gauge_no INTEGER, solo MAP(VARCHAR, INTEGER))",
            "INSERT INTO gauge SELECT i, MAP {'a': i} FROM range(10) r(i)",
            statistics=StatisticsConfig(max_parts=1),
        )["gauge"]["solo"]

        assert solo["parts"] == {}
        assert solo["parts_found"] == 2

    def test_descent_off_leaves_the_map_unsupported(self, tmp_path: Path) -> None:
        labels = duckdb_print(tmp_path, *_LABELS, statistics=StatisticsConfig(max_parts=0))

        assert labels["gauge"]["labels"]["classification"] == "unsupported"
        assert "parts" not in labels["gauge"]["labels"]


def test_a_postgres_hstore_off_the_search_path_is_a_map(
    postgres_test_db: dict[str, str],
    tmp_path: Path,
) -> None:
    with psql(postgres_test_db) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS kit")
        conn.execute("CREATE EXTENSION IF NOT EXISTS hstore SCHEMA kit")
        conn.execute("CREATE TABLE public.gauge (gauge_no integer, attrs kit.hstore)")
        conn.execute(
            "INSERT INTO public.gauge VALUES (1, 'unit=>C, site=>north'), "
            "(2, 'unit=>F, site=>NULL'), (3, ''), (4, NULL)",
        )

    attrs = generate(PostgresAdapter(postgres_test_db), "postgres", tmp_path, "*.gauge")["attrs"]

    assert attrs["classification"] == "composite"
    assert attrs["empty_count"] == 1
    assert set(attrs["parts"]) == {"[keys]", ".unit", ".site"}
    assert (attrs["parts"][".site"]["occurrences"], attrs["parts"][".site"]["null_count"]) == (2, 1)


def test_a_clickhouse_map_counts_a_repeated_key_once_and_its_entries_twice(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.gauge (gauge_no UInt32, labels Map(String, String)) ENGINE = Memory",
    )
    cursor.execute(
        "INSERT INTO seedbank.gauge VALUES (1, map('unit', 'C', 'unit', 'K')), "
        f"(2, map('unit', 'F', 'site', 'north')), (3, map()), (4, map({_escaped_string(_AWKWARD)}, 'q'))",
    )
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    labels = generate(adapter, "clickhouse", tmp_path, "*.gauge")["labels"]

    assert labels["size"]["max"] == 2
    assert labels["empty_count"] == 1
    assert labels["parts"]["[keys]"]["values"][0] == {"value": "unit", "count": 2}
    assert labels["parts"][".unit"]["values"] == [
        {"value": "C", "count": 1},
        {"value": "F", "count": 1},
    ]
    assert labels["parts"]['["it\'s \\\\ a\\nkey"]']["occurrences"] == 1


def test_a_databricks_map_is_descended(databricks_test_schema: Any) -> None:
    cursor = databricks_test_schema
    cursor.execute("CREATE TABLE gauge (gauge_no INT, labels MAP<STRING, STRING>) USING DELTA")
    cursor.execute(
        "INSERT INTO gauge VALUES (1, map('unit', 'C', 'site', 'north')), "
        f"(2, map('unit', 'F', 'site', NULL)), (3, map({_escaped_string(_AWKWARD)}, 'q'))",
    )
    adapter = DatabricksAdapter(DATABRICKS_CREDS, cursor_factory=lambda _params: cursor)
    labels = parts_of(adapter, "gauge", "labels")
    parts = {p.path: p for p in labels.parts}

    assert labels.found == 4
    assert (parts[".site"].occurrences, parts[".site"].stats.null_count) == (2, 1)
    assert parts['["it\'s \\\\ a\\nkey"]'].occurrences == 1


def test_a_snowflake_map_is_descended() -> None:
    adapter = snowflake(
        "CREATE TABLE seedbank.gauge (gauge_no INTEGER, labels MAP(VARCHAR, VARCHAR))",
        "INSERT INTO seedbank.gauge VALUES (1, MAP {'unit': 'C', 'site': 'north'}), "
        "(2, MAP {'unit': 'F', 'site': NULL}), (3, NULL)",
    )
    labels = parts_of(adapter, "gauge", "labels")
    parts = {p.path: p for p in labels.parts}

    assert sorted(parts) == [".site", ".unit", "[keys]"]
    assert parts["[keys]"].occurrences == 4
    assert (parts[".site"].occurrences, parts[".site"].stats.null_count) == (2, 1)


@pytest.mark.parametrize(
    ("vendor", "sql_type"),
    [
        ("duckdb", "MAP(VARCHAR, INTEGER)"),
        ("databricks", "map<string,int>"),
        ("clickhouse", "Map(String, UInt8)"),
        ("snowflake", "MAP(VARCHAR, NUMBER)"),
    ],
)
def test_the_map_reads_speak_their_own_dialect(vendor: Vendor, sql_type: str) -> None:
    module: ModuleType = STATS_MODULES[vendor]
    entries = module.MAPS.entries(None, PartSource("", sql_type, "src_table src", "src.tags"))
    key = module.MAPS.literal(_AWKWARD, entries.key_sql_type)
    statements = [
        select_from(["ent.k AS v"], entries.source),
        select_from(["ent.v AS v"], entries.source) + f"\nWHERE\n  ent.k = {key}",
        select_from([f"{entries.size} AS sz"], "src_table src"),
    ]

    for statement in statements:
        assert foreign_fragments(statement, vendor) == []
        assert violations(statement, vendor) + alias_violations(statement, vendor) == []
        assert layout_violations(derived(statement, "src"), vendor) == []


@pytest.mark.parametrize(
    ("key", "backslash_escapes", "expected"),
    [
        ("it's", False, "'it''s'"),
        ("a\\b'c\nd", True, "'a\\\\b\\'c\\nd'"),
        (3, False, "3"),
        (2.5, False, "CAST('2.5' AS DOUBLE)"),
    ],
)
def test_a_key_is_spelled_as_its_dialect_reads_a_literal(
    key: Any,
    backslash_escapes: bool,
    expected: str,
) -> None:
    assert key_literal(key, "DOUBLE", backslash_escapes=backslash_escapes) == expected


def _standard_string(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _escaped_string(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"
