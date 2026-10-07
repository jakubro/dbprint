"""A composite column is declined whatever its spelling nests, and its scalar siblings still profile.

Measured on chdb, local Spark and duckdb, each spelling composites with nested groups or brackets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from dbprint.adapters import Adapter
from dbprint.adapters.clickhouse import ClickhouseAdapter
from dbprint.adapters.databricks import DatabricksAdapter
from dbprint.adapters.duckdb import DuckdbAdapter
from dbprint.config.project import ConnectionConfig, RedactRule, StatisticsConfig
from dbprint.engine import Engine
from tests._engine_run import conformance_errors
from tests.adapters._credentials import DATABRICKS_CREDS


_COMPOSITES: dict[str, tuple[str, list[str], str]] = {
    "clickhouse": (
        (
            "CREATE TABLE seedbank.field_log (plot Int32, tags Array(Nullable(String)), "
            "amounts Array(Decimal(10, 2)), nested_map Map(String, Array(String)), "
            "pair Tuple(String, Nullable(UInt8)), spot Point) ENGINE = Memory"
        ),
        [
            "INSERT INTO seedbank.field_log VALUES (1, ['a', NULL], [1.5], {'k': ['v']}, ('x', 1), (1, 2))",
            "INSERT INTO seedbank.field_log VALUES (2, ['b'], [2.5], {'k': ['w']}, ('y', NULL), (3, 4))",
        ],
        "field_log",
    ),
    "databricks": (
        (
            "CREATE TABLE field_log (plot INT, tags ARRAY<STRING>, amounts ARRAY<DECIMAL(10,2)>, "
            "attrs MAP<STRING, INT>, rec STRUCT<a: INT, b: STRING>) USING DELTA"
        ),
        [
            "INSERT INTO field_log VALUES (1, array('a'), array(1.5), map('k', 1), struct(1, 'x'))",
            "INSERT INTO field_log VALUES (2, array('b'), array(2.5), map('k', 2), struct(2, 'y'))",
        ],
        "field_log",
    ),
}


@pytest.mark.parametrize("vendor", ["clickhouse", "databricks"])
def test_a_nested_composite_is_declined_and_its_sibling_profiles(
    request: pytest.FixtureRequest,
    vendor: str,
) -> None:
    create, inserts, table_name = _COMPOSITES[vendor]
    adapter = _adapter(request, vendor, create, inserts)

    try:
        table = next(
            t
            for t in adapter.list_tables(include=["*"], exclude=[])
            if t.fqn.rsplit(".", 1)[-1] == table_name
        )
        columns = adapter.introspect_columns(table.fqn)
        _counts, phase_a = adapter.compute_base_statistics(table.fqn, columns, StatisticsConfig())
    finally:
        adapter.close()

    declined = {name for name, stats in phase_a.stats.items() if not stats.supported}

    assert declined == {c.name for c in columns} - {"plot", "spot"}
    assert phase_a.stats["plot"].cardinality == 2


def test_emails_inside_a_nested_struct_never_reach_the_print(tmp_path: Path) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute(
        "CREATE TABLE sample (plot INTEGER PRIMARY KEY, "
        "field_notes STRUCT(email VARCHAR, site STRUCT(bed VARCHAR)))",
    )
    con.execute(
        "INSERT INTO sample SELECT i, "
        "{'email': 'grower' || i || '@example.invalid', 'site': {'bed': 'b' || (i % 3)}} "
        "FROM range(40) r(i)",
    )
    con.close()
    conn = ConnectionConfig(
        name="garden",
        adapter="duckdb",
        output=tmp_path / "prints",
        redact=(RedactRule(looks_like=("email",), with_="drop"),),
    )

    Engine(DuckdbAdapter({"database": str(database)}), conn, tmp_path).generate()
    path = next((tmp_path / "prints" / "garden").rglob("statistics.yaml"))
    columns = yaml.safe_load(path.read_text())["columns"]

    assert columns["field_notes"]["classification"] == "composite"
    assert columns["field_notes"]["parts"][".email"]["redacted"] == "drop"
    assert columns["plot"]["classification"] != "unsupported"
    assert "@example.invalid" not in path.read_text()
    assert conformance_errors(tmp_path / "prints" / "garden") == []


def _adapter(
    request: pytest.FixtureRequest,
    vendor: str,
    create: str,
    inserts: list[str],
) -> Adapter:
    cursor: Any

    if vendor == "clickhouse":
        cursor = request.getfixturevalue("clickhouse_native_connection")
        adapter: Adapter = ClickhouseAdapter(
            {"host": "chdb", "database": "seedbank"},
            cursor_factory=lambda _params: cursor,
        )
    else:
        cursor = request.getfixturevalue("databricks_test_schema")
        adapter = DatabricksAdapter(DATABRICKS_CREDS, cursor_factory=lambda _params: cursor)

    for statement in (create, *inserts):
        cursor.execute(statement)

    adapter.connect()

    return adapter
