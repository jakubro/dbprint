"""Generate a print of one composite-typed table on each substrate, and read back what it published."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import psycopg
import yaml

from dbprint.adapters import (
    Adapter,
    AdapterType,
    SnowflakeAdapter,
)
from dbprint.adapters.base import ColumnParts
from dbprint.adapters.duckdb import DuckdbAdapter
from dbprint.config import StatisticsConfig
from dbprint.config.project import ConnectionConfig, RedactRule
from dbprint.engine import Engine
from tests._engine_run import conformance_errors
from tests.adapters._credentials import SNOWFLAKE_CREDS
from tests.adapters.conftest import SnowflakeDialectShim
from tests.conftest import pg_connect


_SALT = "array-test-salt"


def parts_of(adapter: Adapter, table: str, column: str) -> ColumnParts:
    """One column's descent, read straight off the adapter: these substrates extract no DDL."""

    adapter.connect()

    try:
        fqn = next(
            t.fqn
            for t in adapter.list_tables(include=["*"], exclude=[])
            if t.fqn.rsplit(".", 1)[-1] == table
        )
        columns = [c for c in adapter.introspect_columns(fqn) if c.name == column]
        counts, _ = adapter.compute_base_statistics(fqn, columns, StatisticsConfig())

        return adapter.profile_parts(fqn, columns, StatisticsConfig(), counts)[column]
    finally:
        adapter.close()


def snowflake(*statements: str, column_types: dict[str, str] | None = None) -> Adapter:
    """A Snowflake adapter over an in-memory duckdb holding `statements`' tables in `seedbank`."""

    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA seedbank")

    for statement in statements:
        con.execute(statement)

    shim = SnowflakeDialectShim(con, column_types=column_types)

    return SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _params: shim)


def duckdb_print(
    tmp_path: Path,
    *statements: str,
    statistics: StatisticsConfig | None = None,
    redact: tuple[RedactRule, ...] = (),
) -> dict[str, dict[str, Any]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))

    for statement in statements:
        con.execute(statement)

    con.close()
    conn = ConnectionConfig(
        name="garden",
        adapter="duckdb",
        output=tmp_path / "prints",
        redact=redact,
        redaction_salt=_SALT,
        statistics=statistics or StatisticsConfig(),
    )
    Engine(DuckdbAdapter({"database": str(database)}), conn, tmp_path).generate()
    errors = conformance_errors(tmp_path / "prints" / "garden")

    assert errors == [], errors

    return {
        path.parent.name: yaml.safe_load(path.read_text())["columns"]
        for path in (tmp_path / "prints" / "garden").rglob("statistics.yaml")
    }


def generate(
    adapter: Adapter,
    name: AdapterType,
    tmp_path: Path,
    include: str,
    redact: tuple[RedactRule, ...] = (),
) -> dict[str, Any]:
    conn = ConnectionConfig(
        name="primary",
        adapter=name,
        output=tmp_path,
        include=(include,),
        redact=redact,
    )

    try:
        Engine(adapter, conn, tmp_path).generate()
    finally:
        adapter.close()

    (written,) = (tmp_path / "primary").rglob("statistics.yaml")
    errors = conformance_errors(tmp_path / "primary")

    assert errors == [], errors

    return yaml.safe_load(written.read_text())["columns"]


def psql(creds: dict[str, str]) -> psycopg.Connection:
    return pg_connect(creds)
