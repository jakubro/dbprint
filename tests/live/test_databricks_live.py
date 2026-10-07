"""Live e2e against a real Databricks SQL warehouse: extract -> classify -> write, plus every
behavior the local PySpark+Delta substrate cannot prove.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from tests._cli import credential_env, generate
from tests._engine_run import assert_conformant
from tests.live import _harness as harness


# Every variable `_live_creds` reads without a default, so exporting exactly what the skip
# reason names produces a run rather than a KeyError inside the first test.
_REQUIRED_ENV = (
    "DATABRICKS_SERVER_HOSTNAME",
    "DATABRICKS_HTTP_PATH",
    "DATABRICKS_ACCESS_TOKEN",
    "DATABRICKS_CATALOG",
)

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in _REQUIRED_ENV),
    reason=(
        f"set {', '.join(_REQUIRED_ENV)} to run the live Databricks e2e "
        "(DATABRICKS_SCHEMA defaults to dbprint_live). Runs against a real SQL warehouse - "
        "stop it when done."
    ),
)

CONN_NAME = "databricks_live"
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "databricks"


def _live_creds() -> dict[str, str]:
    return {
        "server_hostname": os.environ["DATABRICKS_SERVER_HOSTNAME"],
        "http_path": os.environ["DATABRICKS_HTTP_PATH"],
        "access_token": os.environ["DATABRICKS_ACCESS_TOKEN"],
        "catalog": os.environ["DATABRICKS_CATALOG"],
        "schema": os.environ.get("DATABRICKS_SCHEMA", "dbprint_live"),
    }


def _apply_fixtures(creds: dict[str, str]) -> None:
    from databricks import sql

    conn = sql.connect(
        server_hostname=creds["server_hostname"],
        http_path=creds["http_path"],
        access_token=creds["access_token"],
        catalog=creds["catalog"],
    )

    try:
        cursor = conn.cursor()
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS `{creds['schema']}`")
        cursor.execute(f"USE SCHEMA `{creds['schema']}`")

        for statement in harness.fixture_statements(_FIXTURE_DIR, "databricks"):
            cursor.execute(statement)

        cursor.close()
    finally:
        conn.close()


def _credential_env(creds: dict[str, str]) -> dict[str, str]:
    return credential_env(
        CONN_NAME,
        {key: creds[key] for key in ("server_hostname", "http_path", "access_token", "catalog")},
    )


def test_databricks_live_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full pipeline against a real warehouse: generate, conformance-clean, CLUSTER BY, FK."""

    creds = _live_creds()
    _apply_fixtures(creds)

    print_dir = generate(
        tmp_path,
        monkeypatch,
        CONN_NAME,
        "databricks",
        _credential_env(creds),
    )

    assert (print_dir / "manifest.yaml").is_file()

    assert_conformant(print_dir)

    manifest = yaml.safe_load((print_dir / "manifest.yaml").read_text())
    schema = creds["schema"]
    assert f"{schema}.loan_request" in manifest["tables"], (
        "the never-populated table is missing from the manifest - list_tables must not drop "
        "a real table just because it carries no write-collected statistics"
    )

    sheet = yaml.safe_load(
        (print_dir / schema / "herbarium_sheet" / "statistics.yaml").read_text(),
    )
    layout = sheet["physical_layout"]
    assert layout["mechanism"] == "cluster"
    assert [k["column"] for k in layout["keys"]] == ["herbarium_id", "logged_at"]

    columns = sheet["columns"]
    assert columns["herbarium_id"]["physical_layout_key"] is True

    relationships = yaml.safe_load(
        (print_dir / schema / "herbarium_sheet" / "relationships.yaml").read_text(),
    )
    herbarium_edge = next(
        (r for r in relationships["refers_to"] if r["target_table"] == f"{schema}.herbarium"),
        None,
    )
    assert herbarium_edge is not None, "the declared FK to herbarium did not surface"
    assert herbarium_edge["detection"] == "declared"

    ddl = (print_dir / schema / "herbarium_sheet" / "ddl.sql").read_text()
    assert "CLUSTER BY" in ddl.upper()


def test_databricks_live_composite_fk_decimal_precision_and_default() -> None:
    """A composite FK with reordered parent columns, a `DECIMAL(18,4)` column and a column
    `DEFAULT`, none in the shared schema - confirmation against a real workspace, self-contained.
    """

    from databricks import sql

    creds = _live_creds()
    conn = sql.connect(
        server_hostname=creds["server_hostname"],
        http_path=creds["http_path"],
        access_token=creds["access_token"],
        catalog=creds["catalog"],
    )
    cursor = conn.cursor()
    schema = creds["schema"]

    try:
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS `{schema}`")
        cursor.execute(f"USE SCHEMA `{schema}`")
        cursor.execute("DROP TABLE IF EXISTS dbprint_composite_fk_src")
        cursor.execute("DROP TABLE IF EXISTS dbprint_composite_fk_ref")
        cursor.execute(
            "CREATE TABLE dbprint_composite_fk_ref (pk1 INT NOT NULL, pk2 STRING NOT NULL, "
            "CONSTRAINT dbprint_ref_pk PRIMARY KEY (pk1, pk2)) USING DELTA",
        )
        cursor.execute(
            "CREATE TABLE dbprint_composite_fk_src (fk2 STRING, fk1 INT, "
            "viability_pct DECIMAL(18,4) DEFAULT 0.00, "
            "CONSTRAINT dbprint_src_fk FOREIGN KEY (fk2, fk1) "
            "REFERENCES dbprint_composite_fk_ref (pk2, pk1)) USING DELTA",
        )

        from dbprint.adapters import DatabricksAdapter

        adapter = DatabricksAdapter(creds)
        adapter.connect()

        try:
            fqn = f"{schema}.dbprint_composite_fk_src"
            adapter.list_tables(include=["*"], exclude=[])
            edges = adapter.introspect_relationships(fqn)
            columns = {c.name: c for c in adapter.introspect_columns(fqn)}
        finally:
            adapter.close()
    finally:
        cursor.execute("DROP TABLE IF EXISTS dbprint_composite_fk_src")
        cursor.execute("DROP TABLE IF EXISTS dbprint_composite_fk_ref")
        cursor.close()
        conn.close()

    assert len(edges) == 1, f"expected exactly one composite FK, got {len(edges)}: {edges}"
    edge = edges[0]
    assert edge.column == ("fk2", "fk1"), f"source columns duplicated or reordered: {edge.column}"
    assert edge.target_column == ("pk2", "pk1"), f"target columns wrong: {edge.target_column}"

    assert columns["viability_pct"].sql_type == "DECIMAL(18,4)"
    assert columns["viability_pct"].default is not None
