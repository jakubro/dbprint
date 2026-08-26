"""`dbprint docs` - CLI-level tests (no real HTTP bind; `serve()` is patched)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import dbprint
from dbprint.cli.main import main
from dbprint.engine import EXIT_GENERIC, EXIT_OK


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

PROJECT_TWO_CONNECTIONS = """\
defaults:
  max_age_days: 7
  statistics: {}
  diff: {}
connections:
  primary:
    adapter: postgres
    output: prints
  secondary:
    adapter: postgres
    output: prints
"""


class TestExtraNotInstalled:
    """Installing without the [docs] extra is supported; the guard replaces the traceback."""

    def test_serve_missing_extra_exits_with_an_install_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delattr(dbprint, "docs", raising=False)
        monkeypatch.setitem(sys.modules, "dbprint.docs", None)

        result = CliRunner().invoke(main, ["docs", "serve", "primary"])

        assert result.exit_code == EXIT_GENERIC
        assert "Install dbprint[docs]" in result.stderr

    def test_build_missing_extra_exits_with_an_install_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delattr(dbprint, "docs", raising=False)
        monkeypatch.setitem(sys.modules, "dbprint.docs", None)

        result = CliRunner().invoke(main, ["docs", "build", "primary"])

        assert result.exit_code == EXIT_GENERIC
        assert "Install dbprint[docs]" in result.stderr


class TestServeFlagValidation:
    def test_rejects_non_loopback_host(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["docs", "serve", "--host", "0.0.0.0"])

        assert result.exit_code == EXIT_GENERIC
        assert "loopback" in result.output

    def test_conn_and_all_together_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["docs", "serve", "primary", "--all"])

        assert result.exit_code == EXIT_GENERIC
        assert "not both" in result.output


class TestServeInvocation:
    def test_serves_the_resolved_connection_on_the_given_host_and_port(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)

        with patch("dbprint.docs.serve") as fake_serve:
            result = CliRunner().invoke(main, ["docs", "serve", "--port", "9001"])

        assert result.exit_code == EXIT_OK
        fake_serve.assert_called_once()
        connections, host, port = fake_serve.call_args.args
        assert [c.name for c in connections] == ["primary"]
        assert host == "127.0.0.1"
        assert port == 9001

    def test_all_widens_past_the_auto_set_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_TWO_CONNECTIONS)
        monkeypatch.chdir(tmp_path)

        with patch("dbprint.docs.serve") as fake_serve:
            result = CliRunner().invoke(main, ["docs", "serve", "--all"])

        assert result.exit_code == EXIT_OK
        connections = fake_serve.call_args.args[0]
        assert {c.name for c in connections} == {"primary", "secondary"}

    def test_no_conn_and_no_all_with_multiple_connections_is_an_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_TWO_CONNECTIONS)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["docs", "serve"])

        assert result.exit_code == EXIT_GENERIC
        assert "multiple connections defined" in result.output

    def test_unknown_connection_exits_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["docs", "serve", "nonexistent"])

        assert result.exit_code == EXIT_GENERIC
        assert "unknown connection" in result.output


class TestBuildInvocation:
    def test_writes_output_and_reports_the_page_count(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)
        output = tmp_path / "site"

        result = CliRunner().invoke(main, ["docs", "build", "--output", str(output)])

        assert result.exit_code == EXIT_OK
        assert (output / "index.html").is_file()
        assert "Wrote" in result.output

    def test_output_not_owned_without_force_exits_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)
        output = tmp_path / "site"
        output.mkdir()
        (output / "not-ours.txt").write_text("keep me\n")

        result = CliRunner().invoke(main, ["docs", "build", "--output", str(output)])

        assert result.exit_code == EXIT_GENERIC
        assert (output / "not-ours.txt").is_file()

    def test_force_recreates_an_unowned_output(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        monkeypatch.chdir(tmp_path)
        output = tmp_path / "site"
        output.mkdir()
        (output / "not-ours.txt").write_text("gone after --force\n")

        result = CliRunner().invoke(
            main,
            ["docs", "build", "--output", str(output), "--force"],
        )

        assert result.exit_code == EXIT_OK
        assert not (output / "not-ours.txt").exists()
