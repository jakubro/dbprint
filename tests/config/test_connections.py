"""Connection resolver tests - env > connections.yaml > .env precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbprint.config import ConfigError, resolve_connection


REQUIRED = ["host", "port", "database", "user", "password"]


def _write_connections_file(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")

    return path


def _write_dotenv(project_root: Path, body: str) -> Path:
    env_path = project_root / ".env"
    env_path.write_text(body)

    return env_path


class TestPrecedence:
    def test_env_wins_over_file_wins_over_dotenv(self, tmp_path: Path) -> None:
        cfile = tmp_path / "connections.yaml"
        _write_connections_file(
            cfile,
            """
secondary:
  host: file-host
  port: 5432
  database: file-db
  user: file-user
  password: file-pw
""",
        )
        _write_dotenv(
            tmp_path,
            "DBPRINT_SECONDARY_HOST=dotenv-host\nDBPRINT_SECONDARY_PORT=9999\n",
        )
        env = {"DBPRINT_SECONDARY_HOST": "env-host"}
        resolved = resolve_connection(
            "secondary",
            REQUIRED,
            project_root=tmp_path,
            connections_file=cfile,
            env=env,
        )
        assert resolved["host"] == "env-host"
        assert resolved["port"] == "5432"  # file beats .env (.env had 9999)
        assert resolved["database"] == "file-db"
        assert resolved["password"] == "file-pw"

    def test_dotenv_only(self, tmp_path: Path) -> None:
        _write_dotenv(
            tmp_path,
            "DBPRINT_PRIMARY_HOST=h\n"
            "DBPRINT_PRIMARY_PORT=1\n"
            "DBPRINT_PRIMARY_DATABASE=d\n"
            "DBPRINT_PRIMARY_USER=u\n"
            "DBPRINT_PRIMARY_PASSWORD=p\n",
        )
        resolved = resolve_connection(
            "primary",
            REQUIRED,
            project_root=tmp_path,
            connections_file=tmp_path / "missing.yaml",
            env={},
        )
        assert resolved == {
            "host": "h",
            "port": "1",
            "database": "d",
            "user": "u",
            "password": "p",
        }

    def test_env_only_no_files(self, tmp_path: Path) -> None:
        env = {
            "DBPRINT_PRIMARY_HOST": "h",
            "DBPRINT_PRIMARY_PORT": "1",
            "DBPRINT_PRIMARY_DATABASE": "d",
            "DBPRINT_PRIMARY_USER": "u",
            "DBPRINT_PRIMARY_PASSWORD": "p",
        }
        resolved = resolve_connection(
            "primary",
            REQUIRED,
            project_root=tmp_path,
            connections_file=tmp_path / "missing.yaml",
            env=env,
        )
        assert resolved == {"host": "h", "port": "1", "database": "d", "user": "u", "password": "p"}


class TestUnresolved:
    def test_missing_required_keys_raise_with_all_listed(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as exc_info:
            resolve_connection(
                "primary",
                ["host", "port", "user"],
                project_root=tmp_path,
                connections_file=tmp_path / "missing.yaml",
                env={},
            )

        msg = str(exc_info.value)
        assert "host" in msg
        assert "port" in msg
        assert "user" in msg
        assert "DBPRINT_PRIMARY_HOST" in msg
        assert "DBPRINT_PRIMARY_PORT" in msg

    def test_partial_unresolved_lists_only_missing(self, tmp_path: Path) -> None:
        env = {"DBPRINT_PRIMARY_HOST": "h"}

        with pytest.raises(ConfigError) as exc_info:
            resolve_connection(
                "primary",
                ["host", "port"],
                project_root=tmp_path,
                connections_file=tmp_path / "missing.yaml",
                env=env,
            )

        msg = str(exc_info.value)
        assert "['port']" in msg
        assert "DBPRINT_PRIMARY_HOST" not in msg


class TestEdgeCases:
    def test_extra_connections_in_file_ignored(self, tmp_path: Path) -> None:
        cfile = tmp_path / "connections.yaml"
        _write_connections_file(
            cfile,
            """
secondary:
  host: a
  port: 1
  database: a
  user: a
  password: a
other:
  host: w
  port: 2
""",
        )
        resolved = resolve_connection(
            "secondary",
            REQUIRED,
            project_root=tmp_path,
            connections_file=cfile,
            env={},
        )
        assert resolved["host"] == "a"

    def test_invalid_yaml_in_connections_file_raises(self, tmp_path: Path) -> None:
        cfile = tmp_path / "connections.yaml"
        _write_connections_file(cfile, ":\n  bad: : :\n")

        with pytest.raises(ConfigError, match="invalid YAML"):
            resolve_connection(
                "primary",
                ["host"],
                project_root=tmp_path,
                connections_file=cfile,
                env={},
            )

    def test_empty_connections_file_treated_as_absent(self, tmp_path: Path) -> None:
        cfile = tmp_path / "connections.yaml"
        _write_connections_file(cfile, "")
        env = {"DBPRINT_PRIMARY_HOST": "h"}
        resolved = resolve_connection(
            "primary",
            ["host"],
            project_root=tmp_path,
            connections_file=cfile,
            env=env,
        )
        assert resolved == {"host": "h"}

    def test_optional_key_present_is_returned(self, tmp_path: Path) -> None:
        env = {"DBPRINT_PRIMARY_HOST": "h", "DBPRINT_PRIMARY_SCHEMA": "seedbank"}
        resolved = resolve_connection(
            "primary",
            ["host"],
            project_root=tmp_path,
            connections_file=tmp_path / "missing.yaml",
            env=env,
            optional_keys=["schema"],
        )
        assert resolved == {"host": "h", "schema": "seedbank"}

    def test_optional_key_absent_is_omitted_without_error(self, tmp_path: Path) -> None:
        env = {"DBPRINT_PRIMARY_HOST": "h"}
        resolved = resolve_connection(
            "primary",
            ["host"],
            project_root=tmp_path,
            connections_file=tmp_path / "missing.yaml",
            env=env,
            optional_keys=["schema", "private_key_file"],
        )
        assert resolved == {"host": "h"}  # absent optionals neither raise nor appear

    def test_required_still_raises_when_optionals_present(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="port"):
            resolve_connection(
                "primary",
                ["host", "port"],
                project_root=tmp_path,
                connections_file=tmp_path / "missing.yaml",
                env={"DBPRINT_PRIMARY_HOST": "h"},
                optional_keys=["schema"],
            )

    def test_yaml_value_coerced_to_string(self, tmp_path: Path) -> None:
        """YAML ints (e.g. port: 5432) must be returned as strings."""

        cfile = tmp_path / "connections.yaml"
        _write_connections_file(
            cfile,
            """
primary:
  port: 5432
""",
        )
        resolved = resolve_connection(
            "primary",
            ["port"],
            project_root=tmp_path,
            connections_file=cfile,
            env={},
        )
        assert resolved == {"port": "5432"}
        assert isinstance(resolved["port"], str)
