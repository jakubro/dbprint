"""`statement_timeout` in `.dbprint.yaml`: one duration, inherited from `defaults`, never zero."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbprint.config import ConfigError, load_project


def _connection_timeout(tmp_path: Path, body: str) -> int | None:
    (tmp_path / ".dbprint.yaml").write_text(body)

    return load_project(tmp_path).connections["w"].statement_timeout


class TestAccepted:
    def test_absent_is_no_limit(self, tmp_path: Path) -> None:
        assert _connection_timeout(tmp_path, "connections:\n  w:\n    adapter: duckdb\n") is None

    def test_a_duration_is_held_in_seconds(self, tmp_path: Path) -> None:
        body = "connections:\n  w:\n    adapter: duckdb\n    statement_timeout: 10m\n"

        assert _connection_timeout(tmp_path, body) == 600

    def test_defaults_are_inherited(self, tmp_path: Path) -> None:
        body = "defaults:\n  statement_timeout: 2h\nconnections:\n  w:\n    adapter: duckdb\n"

        assert _connection_timeout(tmp_path, body) == 7200

    def test_the_connection_overrides_defaults(self, tmp_path: Path) -> None:
        body = (
            "defaults:\n  statement_timeout: 2h\n"
            "connections:\n  w:\n    adapter: duckdb\n    statement_timeout: 30s\n"
        )

        assert _connection_timeout(tmp_path, body) == 30


class TestRefused:
    @pytest.mark.parametrize("value", ["10", "1h30m", "0s", "ten minutes", "-5m"])
    def test_anything_but_one_positive_duration(self, tmp_path: Path, value: str) -> None:
        body = f"connections:\n  w:\n    adapter: duckdb\n    statement_timeout: {value}\n"

        with pytest.raises(ConfigError, match="statement_timeout must be a duration like 30s"):
            _connection_timeout(tmp_path, body)
