"""Implicit connection resolution tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbprint.cli.resolution import ConnectionResolutionError, resolve
from dbprint.config import (
    ConnectionConfig,
    DiffConfig,
    ProjectConfig,
    StatisticsConfig,
)


def _conn(name: str, *, auto: bool = False) -> ConnectionConfig:
    return ConnectionConfig(
        name=name,
        adapter="postgres",
        auto=auto,
        output=Path("/tmp/prints"),
        include=("*",),
        exclude=(),
        max_age_days=7,
        statistics=StatisticsConfig(),
        diff=DiffConfig(),
    )


def _project(connections: dict[str, ConnectionConfig]) -> ProjectConfig:
    return ProjectConfig(project_root=Path("/tmp/project"), connections=connections)


class TestExplicit:
    def test_named_connection_used(self) -> None:
        proj = _project({"a": _conn("a"), "b": _conn("b")})
        result = resolve(proj, "a")
        assert [c.name for c in result] == ["a"]

    def test_unknown_named_connection_errors(self) -> None:
        proj = _project({"a": _conn("a")})

        with pytest.raises(ConnectionResolutionError, match="unknown connection"):
            resolve(proj, "missing")


class TestImplicit:
    def test_zero_connections_errors(self) -> None:
        proj = _project({})

        with pytest.raises(ConnectionResolutionError, match="no connections"):
            resolve(proj, None)

    def test_single_connection_used(self) -> None:
        proj = _project({"a": _conn("a")})
        result = resolve(proj, None)
        assert [c.name for c in result] == ["a"]

    def test_auto_set_runs_all_auto_connections(self) -> None:
        proj = _project({"a": _conn("a", auto=True), "b": _conn("b"), "c": _conn("c", auto=True)})
        result = resolve(proj, None)
        assert {c.name for c in result} == {"a", "c"}

    def test_multi_without_auto_errors(self) -> None:
        proj = _project({"a": _conn("a"), "b": _conn("b")})

        with pytest.raises(ConnectionResolutionError, match="multiple connections defined"):
            resolve(proj, None)

    def test_named_overrides_auto_set(self) -> None:
        proj = _project({"a": _conn("a", auto=True), "b": _conn("b")})
        result = resolve(proj, "b")
        assert [c.name for c in result] == ["b"]
