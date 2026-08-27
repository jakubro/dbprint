"""dbprint serve - CLI-level tests (no real transport)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import dbprint
from dbprint.cli.main import main
from dbprint.engine import EXIT_GENERIC


PROJECT_BASE = """\
defaults:
  max_age_days: 7
  statistics: {}
  diff: {}
connections:
  primary:
    adapter: postgres
    output: prints
"""

# A connection name absent from PROJECT_BASE, so resolving it proves which project was read.
PROJECT_NAMED = PROJECT_BASE.replace("primary:", "warehouse:")


class TestExtraNotInstalled:
    """Installing without the [mcp] extra is supported; the guard replaces the traceback."""

    def test_missing_extra_exits_with_an_install_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)

        # None makes the import raise ImportError, matching an install without the extra.
        monkeypatch.delattr(dbprint, "mcp", raising=False)
        monkeypatch.setitem(sys.modules, "dbprint.mcp", None)

        result = CliRunner().invoke(main, ["serve", "primary"])

        assert result.exit_code == EXIT_GENERIC
        assert "Install dbprint[mcp]" in result.stderr


class TestFlagValidation:
    def test_http_requires_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["serve", "--transport", "http"])
        assert result.exit_code == 1
        assert "--port is required" in result.output

    def test_http_rejects_non_loopback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            main,
            ["serve", "--transport", "http", "--host", "0.0.0.0", "--port", "9000"],
        )
        assert result.exit_code == 1
        assert "loopback" in result.output

    def test_no_read_only_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["serve", "--no-read-only"])
        assert result.exit_code == 1
        assert "read-only" in result.output.lower()


class TestConnectionResolution:
    def test_unknown_connection_exits_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["serve", "nonexistent"])
        assert result.exit_code == 1


class TestStdioInvocation:
    def test_stdio_launch_path_calls_serve_stdio(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)

        with patch("dbprint.mcp.serve_stdio") as fake_serve:
            result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 0
        fake_serve.assert_called_once()


class TestProject:
    @staticmethod
    def _project_and_elsewhere(tmp_path: Path) -> tuple[Path, Path]:
        """Return a project directory holding `warehouse`, and an unrelated sibling."""

        project = tmp_path / "project"
        (project / "prints").mkdir(parents=True)
        (project / ".dbprint.yaml").write_text(PROJECT_NAMED)

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        return project, elsewhere

    def test_resolves_a_project_the_working_directory_cannot_reach(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project, elsewhere = self._project_and_elsewhere(tmp_path)
        monkeypatch.chdir(elsewhere)

        with patch("dbprint.mcp.serve_stdio") as fake_serve:
            result = CliRunner().invoke(main, ["serve", "warehouse", "--project", str(project)])

        assert result.exit_code == 0
        fake_serve.assert_called_once()

    def test_the_same_invocation_without_the_flag_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, elsewhere = self._project_and_elsewhere(tmp_path)
        monkeypatch.chdir(elsewhere)

        result = CliRunner().invoke(main, ["serve", "warehouse"])

        assert result.exit_code == EXIT_GENERIC

    def test_the_config_file_itself_is_accepted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project, elsewhere = self._project_and_elsewhere(tmp_path)
        monkeypatch.chdir(elsewhere)

        with patch("dbprint.mcp.serve_stdio") as fake_serve:
            result = CliRunner().invoke(
                main,
                ["serve", "warehouse", "--project", str(project / ".dbprint.yaml")],
            )

        assert result.exit_code == 0
        fake_serve.assert_called_once()

    def test_a_non_direct_child_is_refused_not_walked_up_to(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--project` is exact - a subdirectory of the project does not resolve it."""

        project, elsewhere = self._project_and_elsewhere(tmp_path)
        monkeypatch.chdir(elsewhere)

        result = CliRunner().invoke(
            main,
            ["serve", "warehouse", "--project", str(project / "prints")],
        )

        assert result.exit_code == EXIT_GENERIC
        assert str(project / "prints") in result.output

    def test_directory_holding_no_project_exits_one_naming_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project, elsewhere = self._project_and_elsewhere(tmp_path)
        monkeypatch.chdir(project)

        result = CliRunner().invoke(main, ["serve", "--project", str(elsewhere)])

        assert result.exit_code == EXIT_GENERIC
        assert str(elsewhere) in result.output

    def test_a_path_that_does_not_exist_names_itself(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--project` is a plain string, so an absent path fails as `ConfigError`, not exit 2."""

        project, _ = self._project_and_elsewhere(tmp_path)
        monkeypatch.chdir(project)
        absent = tmp_path / "absent"

        result = CliRunner().invoke(main, ["serve", "--project", str(absent)])

        assert result.exit_code == EXIT_GENERIC
        assert str(absent) in result.output

    def test_the_old_flag_is_gone(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project, _ = self._project_and_elsewhere(tmp_path)
        monkeypatch.chdir(project)

        result = CliRunner().invoke(main, ["serve", "--project-dir", str(project)])

        assert result.exit_code == 2
        assert "no such option" in result.output.lower()
