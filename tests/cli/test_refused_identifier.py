"""A table name the rules refuse (SPEC 1.5.5) costs its own connection, never the ones after it."""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from dbprint.cli.main import main


_PROJECT = """\
version: 1
connections:
  plots:
    adapter: duckdb
    auto: true
  garden:
    adapter: duckdb
    auto: true
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("plots", "garden"):
        con = duckdb.connect(str(tmp_path / f"{name}.duckdb"))
        con.execute("CREATE TABLE bed AS SELECT i AS id FROM range(20) r(i)")
        con.close()
        monkeypatch.setenv(f"DBPRINT_{name.upper()}_DATABASE", str(tmp_path / f"{name}.duckdb"))

    (tmp_path / ".dbprint.yaml").write_text(_PROJECT)
    monkeypatch.chdir(tmp_path)

    return tmp_path


def _add_dotted_table(project: Path) -> None:
    con = duckdb.connect(str(project / "plots.duckdb"))
    con.execute('CREATE TABLE "beds.v2" AS SELECT 1 AS id')
    con.close()


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(main, list(args))

    return result.exit_code, result.output


def _grow_garden(project: Path) -> None:
    con = duckdb.connect(str(project / "garden.duckdb"))
    con.execute("INSERT INTO bed SELECT i FROM range(20, 45) r(i)")
    con.close()


def test_generate_refuses_one_connection_and_profiles_the_next(project: Path) -> None:
    assert _run("generate", "--no-tui")[0] in (0, 3)
    _add_dotted_table(project)
    _grow_garden(project)

    code, output = _run("generate", "--no-tui", "--force")
    garden = yaml.safe_load((project / "prints" / "garden" / "manifest.yaml").read_text())

    assert code == 1
    assert "Reason: contains-period" in output
    assert garden["tables"]["garden.main.bed"]["row_count"] == 45


def test_diff_refuses_one_connection_and_compares_the_next(project: Path) -> None:
    assert _run("generate", "--no-tui")[0] in (0, 3)
    _add_dotted_table(project)
    _grow_garden(project)

    code, output = _run("diff")

    assert code == 1
    assert "Reason: contains-period" in output
    assert "20 -> 45" in output


def test_an_online_check_refuses_one_connection_and_checks_the_next(project: Path) -> None:
    assert _run("generate", "--no-tui")[0] in (0, 3)
    _add_dotted_table(project)

    code, output = _run("check", "--online")

    assert code == 1
    assert "Reason: contains-period" in output
    assert "garden" in output


def test_a_print_from_an_earlier_release_leaves_once_deleted_by_hand(project: Path) -> None:
    assert _run("generate", "--no-tui")[0] in (0, 3)
    root = project / "prints" / "plots"
    stale = root / "plots" / "main" / "beds.v2"
    shutil.copytree(root / "plots" / "main" / "bed", stale)
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())
    manifest["tables"]["plots.main.beds.v2"] = {
        **manifest["tables"]["plots.main.bed"],
        "path": "plots/main/beds.v2",
    }
    (root / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    _add_dotted_table(project)

    refused, message = _run("generate", "--no-tui")
    (project / ".dbprint.yaml").write_text(
        _PROJECT.replace(
            "  plots:\n    adapter: duckdb\n",
            '  plots:\n    adapter: duckdb\n    exclude: ["plots.main.beds.v2"]\n',
        ),
    )
    carried = _run("generate", "--no-tui")[0]
    still_failing = _run("check")[0]
    shutil.rmtree(stale)
    retired = _run("generate", "--no-tui")[0]
    clean = _run("check")[0]

    assert refused == 1
    assert f"Stale print: {stale}/" in message
    assert carried in (0, 3)
    assert still_failing == 1
    assert retired in (0, 3)
    assert clean == 0
    assert (
        "plots.main.beds.v2" not in yaml.safe_load((root / "manifest.yaml").read_text())["tables"]
    )
