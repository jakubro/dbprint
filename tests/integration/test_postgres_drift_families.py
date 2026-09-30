"""Shape facts a live Postgres changes - object type, collation, partitioning - report as shape."""

from __future__ import annotations

from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
import yaml
from click.testing import CliRunner

from dbprint.cli.main import main


_CONN = "drift_conn"


def _project(tmp_path: Path, creds: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".dbprint.yaml").write_text(
        f"connections:\n  {_CONN}:\n    adapter: postgres\n    auto: true\n"
        "    include: ['*.garden.*']\n    infer_relationships: false\n",
    )
    monkeypatch.chdir(tmp_path)

    for key in ("host", "port", "database", "user", "password"):
        monkeypatch.setenv(f"DBPRINT_{_CONN.upper()}_{key.upper()}", str(creds[key] or ""))

    return tmp_path


def _sql(creds: dict[str, str], *statements: str) -> None:
    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password=creds["password"],
        autocommit=True,
    ) as conn:
        for statement in statements:
            conn.execute(cast(LiteralString, statement))


def _diff(project: Path) -> dict:
    generated = CliRunner().invoke(main, ["generate", "--no-tui"])
    assert generated.exit_code in (0, 3), generated.output

    return yaml.safe_load(CliRunner().invoke(main, ["diff", "-q", "--format", "yaml"]).stdout)


def _mutated(creds: dict[str, str], project: Path, *statements: str) -> dict:
    _diff(project)
    _sql(creds, *statements)

    return yaml.safe_load(CliRunner().invoke(main, ["diff", "-q", "--format", "yaml"]).stdout)


def test_a_table_replaced_by_a_matview_is_a_type_change(
    e2e_postgres_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = e2e_postgres_db
    _sql(
        creds,
        "CREATE SCHEMA garden",
        "CREATE TABLE garden.plot AS SELECT i AS a, 'v' || i AS b FROM generate_series(1, 50) i",
    )
    project = _project(tmp_path, creds, monkeypatch)
    diff = _mutated(
        creds,
        project,
        "DROP TABLE garden.plot",
        "CREATE MATERIALIZED VIEW garden.plot AS "
        "SELECT i AS a, 'v' || i AS b FROM generate_series(1, 50) i",
    )
    fqn = f"{creds['database']}.garden.plot"

    assert {
        "kind": "table_type_changed",
        "table": fqn,
        "before": "table",
        "after": "matview",
    } in diff["changes"]
    assert diff["summary"]["tables_modified"] == 1
    assert diff["summary"]["unchanged_tables"] == 0


def test_a_recollated_column_is_a_collation_change(
    e2e_postgres_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = e2e_postgres_db
    _sql(
        creds,
        "CREATE SCHEMA garden",
        "CREATE TABLE garden.plot AS SELECT i AS a, 'v' || i AS b FROM generate_series(1, 50) i",
    )
    project = _project(tmp_path, creds, monkeypatch)
    diff = _mutated(creds, project, 'ALTER TABLE garden.plot ALTER COLUMN b TYPE text COLLATE "C"')
    events = [c for c in diff["changes"] if c["kind"] == "column_collation_changed"]

    assert [(e["column"], e["after"]) for e in events] == [("b", "C")]
    assert not any(c.get("stat") == "collation" for c in diff["changes"])


def test_repartitioning_reports_the_layout_once(
    e2e_postgres_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = e2e_postgres_db
    _sql(
        creds,
        "CREATE SCHEMA garden",
        "CREATE TABLE garden.plot (a int, b text)",
        "INSERT INTO garden.plot SELECT i, 'v' || i FROM generate_series(1, 50) i",
    )
    project = _project(tmp_path, creds, monkeypatch)
    diff = _mutated(
        creds,
        project,
        "DROP TABLE garden.plot",
        "CREATE TABLE garden.plot (a int, b text) PARTITION BY RANGE (a)",
        "CREATE TABLE garden.plot_all PARTITION OF garden.plot FOR VALUES FROM (0) TO (1000)",
        "INSERT INTO garden.plot SELECT i, 'v' || i FROM generate_series(1, 50) i",
    )
    kinds = [c["kind"] for c in diff["changes"] if c.get("table", "").endswith("garden.plot")]

    assert "physical_layout_changed" in kinds
    assert not any(c.get("stat") == "physical_layout_key" for c in diff["changes"])
