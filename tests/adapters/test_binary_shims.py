"""A binary column's value list reaches the Redshift and Snowflake shims as each dialect spells it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dbprint.adapters import RedshiftAdapter, SnowflakeAdapter
from dbprint.adapters.dialect import Vendor
from dbprint.adapters.sql_layout import select_from
from tests.adapters._composites import generate
from tests.adapters._dialects import STATS_MODULES, foreign_fragments
from tests.adapters._sql_style import alias_violations, layout_violations, violations


def test_redshift_publishes_binary_values_as_lowercase_hex(
    redshift_postgres_connection: Any,
    tmp_path: Path,
) -> None:
    shim = redshift_postgres_connection
    shim.execute("CREATE TABLE seedbank.badge (badge_no integer, tag bytea)")
    shim.execute("INSERT INTO seedbank.badge VALUES (1, '\\x0aff'), (2, '\\x0aff'), (3, '\\x01')")
    adapter = RedshiftAdapter(
        {"host": "redshift", "database": "seedbank", "user": "test", "password": "test"},
        cursor_factory=lambda _p: shim,
    )
    tag = generate(adapter, "redshift", tmp_path, "*.badge")["tag"]

    assert {v["value"] for v in tag["values"]} == {"0aff", "01"}


def test_snowflake_publishes_binary_values_as_lowercase_hex(
    snowflake_duckdb_connection: Any,
    tmp_path: Path,
) -> None:
    shim = snowflake_duckdb_connection
    shim.execute("CREATE TABLE seedbank.badge (badge_no INTEGER, tag BLOB)")
    shim.execute("INSERT INTO seedbank.badge VALUES (1, '\\x0A\\xFF'::BLOB), (2, '\\x01'::BLOB)")
    adapter = SnowflakeAdapter(
        {
            "account": "test-account",
            "user": "test-user",
            "password": "test-password",
            "warehouse": "test-warehouse",
            "database": "memory",
            "role": "test-role",
        },
        cursor_factory=lambda _p: shim,
    )
    tag = generate(adapter, "snowflake", tmp_path, "*.badge")["tag"]

    assert {v["value"] for v in tag["values"]} == {"0aff", "01"}


@pytest.mark.parametrize(
    "vendor",
    ["postgres", "duckdb", "mysql", "snowflake", "bigquery", "redshift", "databricks"],
)
def test_the_binary_hex_read_speaks_its_own_dialect(vendor: Vendor) -> None:
    rendering = STATS_MODULES[vendor].render_binary
    statement = select_from([f"{rendering('src.tag')} AS v"], "src_table src")

    assert foreign_fragments(statement, vendor) == []
    assert violations(statement, vendor) + alias_violations(statement, vendor) == []
    assert layout_violations(statement, vendor) == []
