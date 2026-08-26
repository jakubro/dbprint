"""dbprint init - scaffolding tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from dbprint.cli.commands.init import init_command


def _patched_connections_dir(tmp_path: Path):
    """Redirect ~/.dbprint/ writes into tmp_path during init tests."""

    return patch(
        "dbprint.cli.commands.init.CONNECTIONS_FILE",
        tmp_path / "fake-home" / "connections.yaml",
    )


class TestFirstRun:
    def test_creates_project_config(self, tmp_path: Path) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path), _patched_connections_dir(tmp_path):
            result = runner.invoke(init_command, [])
            assert result.exit_code == 0
            assert (Path.cwd() / ".dbprint.yaml").is_file()

    def test_creates_prints_dir(self, tmp_path: Path) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path), _patched_connections_dir(tmp_path):
            runner.invoke(init_command, [])
            assert (Path.cwd() / "prints").is_dir()

    def test_creates_connections_template(self, tmp_path: Path) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path), _patched_connections_dir(tmp_path):
            runner.invoke(init_command, [])
            assert (tmp_path / "fake-home" / "connections.yaml").is_file()


class TestIdempotency:
    def test_second_run_preserves_existing(self, tmp_path: Path) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path), _patched_connections_dir(tmp_path):
            (Path.cwd() / ".dbprint.yaml").write_text("user_modified: true\n")
            result = runner.invoke(init_command, [])
            assert result.exit_code == 0
            assert (Path.cwd() / ".dbprint.yaml").read_text() == "user_modified: true\n"

    def test_force_overwrites(self, tmp_path: Path) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path), _patched_connections_dir(tmp_path):
            (Path.cwd() / ".dbprint.yaml").write_text("user_modified: true\n")
            result = runner.invoke(init_command, ["--force"])
            assert result.exit_code == 0
            assert "user_modified" not in (Path.cwd() / ".dbprint.yaml").read_text()
            assert "connections:" in (Path.cwd() / ".dbprint.yaml").read_text()


class TestCredentialsAreMachineWide:
    """`~/.dbprint/connections.yaml` is shared across every project; `--force` never reaches it."""

    def test_force_does_not_overwrite_populated_credentials(self, tmp_path: Path) -> None:
        runner = CliRunner()
        real_credentials = "primary:\n  host: db.internal\n  password: correct-horse\n"

        with runner.isolated_filesystem(temp_dir=tmp_path), _patched_connections_dir(tmp_path):
            creds = tmp_path / "fake-home" / "connections.yaml"
            creds.parent.mkdir(parents=True)
            creds.write_text(real_credentials)

            result = runner.invoke(init_command, ["--force"])

            assert result.exit_code == 0
            assert creds.read_text() == real_credentials
            assert "kept\tconnections_file" in result.output

    def test_absent_credentials_are_still_scaffolded(self, tmp_path: Path) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path), _patched_connections_dir(tmp_path):
            result = runner.invoke(init_command, [])

            assert result.exit_code == 0
            assert (tmp_path / "fake-home" / "connections.yaml").is_file()
