"""`--project` naming a git address: refusal on write/live commands, resolution on read ones.

Drives real CLI commands against a local bare git repository - never a network host.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dbprint.cli.main import main


PROJECT_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
"""

# Passed to every git subprocess call; only `commit` actually reads author/committer.
_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "botanist",
    "GIT_AUTHOR_EMAIL": "botanist@seedbank.example",
    "GIT_COMMITTER_NAME": "botanist",
    "GIT_COMMITTER_EMAIL": "botanist@seedbank.example",
}


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_IDENTITY_ENV},
    )


def _init_repo(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "--initial-branch=main"], cwd=work)


def _commit_project(work: Path, at: Path) -> None:
    at.mkdir(parents=True, exist_ok=True)
    (at / ".dbprint.yaml").write_text(PROJECT_YAML)
    (at / "prints" / "primary").mkdir(parents=True)
    (at / "prints" / "primary" / "manifest.yaml").write_text("format_version: 1\ntables: {}\n")
    _run_git(["add", "."], cwd=work)
    _run_git(["commit", "-m", "initial"], cwd=work)


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A bare `main`-branch repo whose root holds a minimal committed project."""

    work = tmp_path / "work"
    _init_repo(work)
    _commit_project(work, work)

    bare = tmp_path / "bare.git"
    _run_git(["clone", "--bare", "--quiet", str(work), str(bare)], cwd=tmp_path)

    return bare


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _address(bare_repo: Path) -> str:
    """The explicit grammar, default branch, repository root - any git URL string qualifies."""

    return f"{bare_repo}#main:"


@pytest.mark.parametrize("command", ["list", "check"])
def test_read_only_commands_resolve_a_remote_project(command: str, bare_repo: Path) -> None:
    result = CliRunner().invoke(main, [command, "--project", _address(bare_repo), "--no-tui"])

    assert "no .dbprint.yaml" not in result.output
    assert "remote repository" not in result.output


@pytest.mark.parametrize("command", ["generate", "diff"])
def test_write_or_live_commands_refuse_before_cloning(
    command: str,
    bare_repo: Path,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(main, [command, "--project", _address(bare_repo), "--no-tui"])

    assert result.exit_code != 0
    assert "remote repository" in result.output
    assert not (tmp_path / "home" / ".dbprint" / "cache").exists()


def test_check_online_refuses_before_cloning(bare_repo: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        ["check", "--online", "--project", _address(bare_repo), "--no-tui"],
    )

    assert result.exit_code != 0
    assert "remote repository" in result.output
    assert not (tmp_path / "home" / ".dbprint" / "cache").exists()


def test_check_offline_does_not_refuse(bare_repo: Path) -> None:
    result = CliRunner().invoke(main, ["check", "--project", _address(bare_repo), "--no-tui"])

    assert "remote repository" not in result.output


def test_a_bare_remote_never_discovers_a_nested_config(tmp_path: Path) -> None:
    """The bare form means the repository root - a nested `.dbprint.yaml` is never walked to."""

    work = tmp_path / "nested-work"
    _init_repo(work)
    _commit_project(work, work / "project1")

    bare = tmp_path / "nested-bare.git"
    _run_git(["clone", "--bare", "--quiet", str(work), str(bare)], cwd=tmp_path)

    result = CliRunner().invoke(main, ["list", "--project", f"{bare}#main:"])

    assert "no .dbprint.yaml at" in result.output


def test_serve_starts_a_refresh_watcher_for_a_remote_project(bare_repo: Path) -> None:
    """A long-lived MCP server refreshes its clone on the TTL - only meaningful for a remote."""

    with (
        patch("dbprint.mcp.serve_stdio"),
        patch("dbprint.cli.options.watch_for_refresh") as fake_watch,
    ):
        result = CliRunner().invoke(main, ["serve", "--project", _address(bare_repo)])

    assert result.exit_code == 0
    fake_watch.assert_called_once()


def test_serve_starts_no_refresh_watcher_for_a_local_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".dbprint.yaml").write_text(PROJECT_YAML)
    (project / "prints" / "primary").mkdir(parents=True)
    (project / "prints" / "primary" / "manifest.yaml").write_text(
        "format_version: 1\ntables: {}\n",
    )

    with (
        patch("dbprint.mcp.serve_stdio"),
        patch("dbprint.cli.options.watch_for_refresh") as fake_watch,
    ):
        result = CliRunner().invoke(main, ["serve", "--project", str(project)])

    assert result.exit_code == 0
    fake_watch.assert_not_called()


def test_docs_serve_starts_a_refresh_watcher_for_a_remote_project(bare_repo: Path) -> None:
    pytest.importorskip("dbprint.docs")

    with (
        patch("dbprint.docs.serve"),
        patch("dbprint.cli.options.watch_for_refresh") as fake_watch,
    ):
        result = CliRunner().invoke(
            main,
            ["docs", "serve", "--project", _address(bare_repo), "--all"],
        )

    assert result.exit_code == 0
    fake_watch.assert_called_once()
