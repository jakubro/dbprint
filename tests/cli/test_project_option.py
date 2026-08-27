"""`--project` reaches every loader, uniformly, and stays exact."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dbprint.cli.main import main


PROJECT_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
"""


def _write_project(root: Path) -> None:
    (root / ".dbprint.yaml").write_text(PROJECT_YAML)
    (root / "prints" / "primary").mkdir(parents=True)
    (root / "prints" / "primary" / "manifest.yaml").write_text(
        "format_version: 1\ntables: {}\n",
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _write_project(root)

    return root


@pytest.fixture(autouse=True)
def _elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here runs where the project is unreachable by walking up - `--project` only."""

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))


@pytest.mark.parametrize("command", ["list", "context", "check"])
def test_offline_readers_resolve_the_project_and_the_config_file(
    command: str,
    project_dir: Path,
) -> None:
    args = [command] if command != "context" else [command, "--all"]

    by_dir = CliRunner().invoke(main, [*args, "--project", str(project_dir)])
    by_file = CliRunner().invoke(main, [*args, "--project", str(project_dir / ".dbprint.yaml")])

    for result in (by_dir, by_file):
        assert "no such option" not in result.output.lower()
        assert "no .dbprint.yaml" not in result.output


@pytest.mark.parametrize("command", ["generate", "diff"])
def test_adapter_commands_resolve_the_project_before_failing_on_credentials(
    command: str,
    project_dir: Path,
) -> None:
    """The assertion is that `--project` got the run PAST project resolution, into credentials."""

    result = CliRunner().invoke(main, [command, "--project", str(project_dir), "--no-tui"])

    assert "no .dbprint.yaml" not in result.output
    assert "missing required credentials" in result.output


@pytest.mark.parametrize("subcommand", ["serve", "build"])
def test_docs_subcommands_resolve_the_project(subcommand: str, project_dir: Path) -> None:
    pytest.importorskip("dbprint.docs")

    args = ["docs", subcommand, "--project", str(project_dir), "--all"]

    if subcommand == "serve":
        from unittest.mock import patch

        with patch("dbprint.docs.serve"):
            result = CliRunner().invoke(main, args)
    else:
        output_dir = project_dir.parent / "site"
        result = CliRunner().invoke(main, [*args, "--output", str(output_dir)])

    assert "no such option" not in result.output.lower()
    assert "no .dbprint.yaml" not in result.output


def test_a_non_direct_child_is_refused_on_every_loader(project_dir: Path) -> None:
    """`--project` naming the project's own PARENT must not silently walk down or up."""

    result = CliRunner().invoke(main, ["list", "--project", str(project_dir.parent)])

    assert "no .dbprint.yaml at" in result.output
    assert str(project_dir.parent / ".dbprint.yaml") in result.output


def test_old_flag_name_is_an_unknown_option_everywhere(project_dir: Path) -> None:
    result = CliRunner().invoke(main, ["list", "--project-dir", str(project_dir)])

    assert result.exit_code == 2
    assert "no such option" in result.output.lower()


def test_init_carries_no_project_option() -> None:
    """`init` creates a config rather than loading one - the one deliberate exclusion."""

    result = CliRunner().invoke(main, ["init", "--help"])

    assert "--project " not in result.output
