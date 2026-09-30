"""A table the config excludes whose print a new `redact` rule contradicts (SPEC 2.5, 2.2.9)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from dbprint.cli.main import main


_PROJECT = """\
version: 1
connections:
  garden:
    adapter: duckdb
"""
_NARROWED = """\
    exclude: ["garden.main.curator"]
    redact:
      - columns: ["garden.main.curator.email"]
        with: drop
"""


@pytest.fixture
def excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute(
        "CREATE TABLE curator AS SELECT i AS id, 'c' || i || '@example.org' AS email "
        "FROM range(30) r(i)",
    )
    con.execute("CREATE TABLE bed AS SELECT i AS id FROM range(10) r(i)")
    con.close()
    monkeypatch.setenv("DBPRINT_GARDEN_DATABASE", str(database))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".dbprint.yaml").write_text(_PROJECT)
    assert CliRunner().invoke(main, ["generate", "-q"]).exit_code in (0, 3)
    (tmp_path / ".dbprint.yaml").write_text(_PROJECT + _NARROWED)

    return tmp_path


def test_generate_fails_it_without_listing_it_as_unprofiled(excluded: Path) -> None:
    result = CliRunner().invoke(main, ["generate", "--no-tui"])
    manifest = yaml.safe_load((excluded / "prints" / "garden" / "manifest.yaml").read_text())

    assert result.exit_code == 5
    assert "failed_tables" not in manifest
    assert "garden.main.curator" in manifest["tables"]
    assert "delete its directory" in result.output
    assert "--force" not in result.output


def test_check_names_the_remedy_that_works(excluded: Path) -> None:
    CliRunner().invoke(main, ["generate", "-q"])

    result = CliRunner().invoke(main, ["check"])

    assert result.exit_code == 1
    assert "privacy.redaction-not-applied" in result.output
    assert "delete its directory" in result.output
