"""A Postgres foreign table is listed as a table whose rows live elsewhere (SPEC 2.2.20)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, LiteralString, cast

import pytest
import yaml
from click.testing import CliRunner

from dbprint.cli.main import main
from dbprint.config.connections import env_var_name
from tests._cli import write_project
from tests._engine_run import assert_conformant
from tests.conftest import PostgresCluster, fresh_database, pg_connect


_SETUP = (
    "CREATE EXTENSION postgres_fdw",
    "CREATE SCHEMA seedbank",
    "CREATE TABLE seedbank.storage_log (id int PRIMARY KEY, herbarium_id int, logged_at timestamptz)",
    "INSERT INTO seedbank.storage_log SELECT g, g % 7, now() FROM generate_series(1, 50) g",
    "CREATE TABLE seedbank.herbarium (id int PRIMARY KEY)",
    "CREATE TABLE seedbank.field_survey_src (id int, biome text)",
    "CREATE TABLE seedbank.field_survey (id int, biome text) PARTITION BY LIST (biome)",
    "CREATE TABLE seedbank.field_survey_local PARTITION OF seedbank.field_survey FOR VALUES IN ('a')",
)


@pytest.fixture
def fdw_db(postgres_cluster: PostgresCluster) -> Iterator[dict[str, str]]:
    """A loopback `postgres_fdw` server into its own database, and a foreign table over it."""

    with pg_connect(postgres_cluster.creds()) as conn:
        available = conn.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'postgres_fdw'",
        ).fetchone()

    if available is None:
        pytest.skip("postgres_fdw is not installed on the suite's Postgres")

    with fresh_database(postgres_cluster, "fdw") as creds:
        with pg_connect(creds) as conn:
            for statement in (
                *_SETUP,
                (
                    f"CREATE SERVER loop FOREIGN DATA WRAPPER postgres_fdw OPTIONS "
                    f"(host '127.0.0.1', port '{postgres_cluster.port}', dbname '{creds['database']}')"
                ),
                f"CREATE USER MAPPING FOR {postgres_cluster.superuser} SERVER loop",
                (
                    "CREATE FOREIGN TABLE seedbank.remote_storage_log (id int NOT NULL, herbarium_id int, "
                    "logged_at timestamptz) SERVER loop "
                    "OPTIONS (schema_name 'seedbank', table_name 'storage_log')"
                ),
                "COMMENT ON FOREIGN TABLE seedbank.remote_storage_log IS 'storage log kept on the loop server'",
                "CREATE VIEW seedbank.remote_storage_log_v AS SELECT * FROM seedbank.remote_storage_log",
                (
                    "CREATE FOREIGN TABLE seedbank.field_survey_remote PARTITION OF seedbank.field_survey "
                    "FOR VALUES IN ('b') SERVER loop OPTIONS (schema_name 'seedbank', table_name 'field_survey_src')"
                ),
            ):
                conn.execute(cast(LiteralString, statement))

        yield creds


def test_a_foreign_table_no_rule_opts_in_is_printed_from_the_catalog(
    fdw_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _seq_scans(fdw_db)
    result, prints = _generate(fdw_db, tmp_path, monkeypatch)
    manifest = yaml.safe_load((prints / "manifest.yaml").read_text())["tables"]
    remote = _fqn(manifest, "remote_storage_log")
    statistics = _artifact(prints, manifest, remote, "statistics.yaml")
    ddl = (prints / manifest[remote]["path"] / "ddl.sql").read_text()
    log_view = _artifact(
        prints,
        manifest,
        _fqn(manifest, "remote_storage_log_v"),
        "statistics.yaml",
    )

    assert result.exit_code in (0, 3), result.output
    assert manifest[remote]["type"] == "table"
    assert "row_count" not in manifest[remote]
    assert statistics["catalog_only"] is True
    assert statistics["external"] is True
    assert statistics["columns"]["id"]["nullable"] is False
    assert statistics["columns"]["herbarium_id"]["classification"] == "foreign_key_candidate"
    assert "CREATE FOREIGN TABLE" in ddl
    assert "SERVER loop" in ddl
    assert "OPTIONS (" in ddl
    assert "CREATE SERVER" not in ddl
    assert "USER MAPPING" not in ddl
    assert "IS 'storage log kept on the loop server'" in ddl
    assert remote in log_view["depends_on"]
    assert _seq_scans(fdw_db) == before
    assert_conformant(prints)


def test_a_parent_with_a_foreign_partition_stays_a_profiled_table(
    fdw_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, prints = _generate(fdw_db, tmp_path, monkeypatch)
    manifest = yaml.safe_load((prints / "manifest.yaml").read_text())["tables"]
    survey = _artifact(prints, manifest, _fqn(manifest, "field_survey"), "statistics.yaml")

    assert "external" not in survey
    assert "catalog_only" not in survey
    assert not [fqn for fqn in manifest if fqn.endswith(".field_survey_remote")]


def test_an_excluded_foreign_table_is_not_listed(
    fdw_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, prints = _generate(fdw_db, tmp_path, monkeypatch, exclude=("*.remote_storage_log",))
    manifest = yaml.safe_load((prints / "manifest.yaml").read_text())["tables"]

    assert not [fqn for fqn in manifest if fqn.endswith(".remote_storage_log")]


def test_an_opted_in_foreign_table_is_read_through_the_wrapper(
    fdw_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _seq_scans(fdw_db)
    rules = '    rules:\n      - include: ["*.remote_storage_log"]\n        read_rows: true\n'
    result, prints = _generate(fdw_db, tmp_path, monkeypatch, rules)
    manifest = yaml.safe_load((prints / "manifest.yaml").read_text())["tables"]
    statistics = _artifact(
        prints,
        manifest,
        _fqn(manifest, "remote_storage_log"),
        "statistics.yaml",
    )

    assert result.exit_code in (0, 3), result.output
    assert statistics["row_count"] == 50
    assert statistics["external"] is True
    assert _seq_scans(fdw_db) > before
    assert_conformant(prints)


def test_an_analyzed_large_foreign_table_draws_no_tablesample(
    fdw_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pg_connect(fdw_db) as conn:
        conn.execute(
            "INSERT INTO seedbank.storage_log SELECT g, g % 7, now() FROM generate_series(51, 30000) g",
        )
        conn.execute("ANALYZE seedbank.remote_storage_log")

    rules = '    rules:\n      - include: ["*.remote_storage_log"]\n        read_rows: true\n'
    result, prints = _generate(fdw_db, tmp_path, monkeypatch, rules)
    manifest = yaml.safe_load((prints / "manifest.yaml").read_text())["tables"]
    statistics = _artifact(
        prints,
        manifest,
        _fqn(manifest, "remote_storage_log"),
        "statistics.yaml",
    )

    assert result.exit_code in (0, 3), result.output
    assert statistics["row_count"] == 30000


def test_a_sample_on_an_opted_in_foreign_table_fails_it_alone(
    fdw_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = (
        '    rules:\n      - include: ["*.remote_storage_log"]\n        read_rows: true\n'
        "        sample: 0.1\n"
    )
    result, prints = _generate(fdw_db, tmp_path, monkeypatch, rules)
    manifest = yaml.safe_load((prints / "manifest.yaml").read_text())["tables"]

    assert result.exit_code == 5, result.output
    assert "cannot be sampled (TABLESAMPLE applies to local tables only)" in result.output
    assert "narrow it with a filter rule instead" in result.output
    assert _fqn(manifest, "herbarium")


def _generate(
    creds: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rules: str = "",
    exclude: tuple[str, ...] = ("*.storage_log", "*.field_survey_src"),
) -> tuple[Any, Path]:
    project = tmp_path / "project"
    write_project(project, "fdw", "postgres", f"    exclude: {list(exclude)}\n{rules}")
    monkeypatch.chdir(project)

    for key, value in creds.items():
        monkeypatch.setenv(env_var_name("fdw", key), value)

    return CliRunner().invoke(main, ["generate", "--no-tui"]), project / "prints" / "fdw"


def _fqn(manifest: dict[str, Any], name: str) -> str:
    (fqn,) = [fqn for fqn in manifest if fqn.endswith(f".seedbank.{name}")]

    return fqn


def _artifact(prints: Path, manifest: dict[str, Any], fqn: str, name: str) -> dict[str, Any]:
    return yaml.safe_load((prints / manifest[fqn]["path"] / name).read_text())


def _seq_scans(creds: dict[str, str]) -> int:
    with pg_connect(creds) as conn:
        conn.execute("SELECT pg_stat_clear_snapshot()")
        row = conn.execute(
            "SELECT seq_scan FROM pg_stat_user_tables "
            "WHERE schemaname = 'seedbank' AND relname = 'storage_log'",
        ).fetchone()

    assert row is not None

    return int(row[0])
