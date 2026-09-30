"""`parallelism` in `.dbprint.yaml`: a positive integer, inherited from `defaults`, 1 on duckdb."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbprint.config import ConfigError, load_project


def _parallelism(tmp_path: Path, body: str) -> int:
    (tmp_path / ".dbprint.yaml").write_text(body)

    return load_project(tmp_path).connections["w"].parallelism


class TestAccepted:
    def test_absent_is_one_session(self, tmp_path: Path) -> None:
        assert _parallelism(tmp_path, "connections:\n  w:\n    adapter: postgres\n") == 1

    def test_a_count_is_held(self, tmp_path: Path) -> None:
        body = "connections:\n  w:\n    adapter: snowflake\n    parallelism: 4\n"

        assert _parallelism(tmp_path, body) == 4

    def test_defaults_are_inherited(self, tmp_path: Path) -> None:
        body = "defaults:\n  parallelism: 4\nconnections:\n  w:\n    adapter: postgres\n"

        assert _parallelism(tmp_path, body) == 4

    def test_the_connection_overrides_defaults(self, tmp_path: Path) -> None:
        body = (
            "defaults:\n  parallelism: 4\n"
            "connections:\n  w:\n    adapter: postgres\n    parallelism: 2\n"
        )

        assert _parallelism(tmp_path, body) == 2

    def test_duckdb_accepts_one(self, tmp_path: Path) -> None:
        body = "connections:\n  w:\n    adapter: duckdb\n    parallelism: 1\n"

        assert _parallelism(tmp_path, body) == 1


class TestRefused:
    @pytest.mark.parametrize("value", ["0", "-2", "'4'", "2.5", "true"])
    def test_anything_but_a_positive_integer(self, tmp_path: Path, value: str) -> None:
        body = f"connections:\n  w:\n    adapter: postgres\n    parallelism: {value}\n"

        with pytest.raises(ConfigError, match="parallelism must be an integer of at least 1"):
            _parallelism(tmp_path, body)

    def test_duckdb_refuses_more_than_one(self, tmp_path: Path) -> None:
        body = "connections:\n  w:\n    adapter: duckdb\n    parallelism: 2\n"

        with pytest.raises(ConfigError, match="not supported on the duckdb adapter"):
            _parallelism(tmp_path, body)

    def test_an_inherited_value_is_refused_on_duckdb_too(self, tmp_path: Path) -> None:
        body = "defaults:\n  parallelism: 4\nconnections:\n  w:\n    adapter: duckdb\n"

        with pytest.raises(ConfigError, match="duckdb"):
            _parallelism(tmp_path, body)
