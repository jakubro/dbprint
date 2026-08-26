"""ServedConnections multi-conn state + default resolution per MCP.md 5.1."""

from __future__ import annotations

import pytest

from dbprint.config import ConnectionConfig, ProjectConfig
from dbprint.config.project import DiffConfig, StatisticsConfig
from dbprint.mcp import McpError, ServedConnections, build_state


def _conn(name: str, *, auto: bool = False) -> ConnectionConfig:
    return ConnectionConfig(
        name=name,
        adapter="postgres",
        auto=auto,
        statistics=StatisticsConfig(),
        diff=DiffConfig(),
    )


def _project(*conns: ConnectionConfig) -> ProjectConfig:
    return ProjectConfig(
        project_root=__import__("pathlib").Path("/tmp"),
        connections={c.name: c for c in conns},
    )


class TestBuildState:
    def test_single_connection_default(self) -> None:
        state = build_state(_project(_conn("primary")), conn_arg=None)
        assert "primary" in state.served
        assert state.default == "primary"

    def test_explicit_name(self) -> None:
        project = _project(_conn("primary"), _conn("secondary"))
        state = build_state(project, conn_arg="secondary")
        assert list(state.served) == ["secondary"]
        assert state.default == "secondary"

    def test_unknown_name_raises(self) -> None:
        project = _project(_conn("primary"))

        with pytest.raises(McpError):
            build_state(project, conn_arg="missing")

    def test_auto_set_no_default(self) -> None:
        project = _project(_conn("a", auto=True), _conn("b", auto=True))
        state = build_state(project, conn_arg=None)
        assert set(state.served) == {"a", "b"}
        assert state.default is None

    def test_no_auto_multiple_raises(self) -> None:
        project = _project(_conn("a"), _conn("b"))

        with pytest.raises(McpError):
            build_state(project, conn_arg=None)


class TestResolve:
    def test_explicit_conn_arg(self) -> None:
        state = ServedConnections(
            served={"primary": _conn("primary"), "other": _conn("other")},
            default="primary",
        )
        assert state.resolve("other").name == "other"

    def test_default_when_omitted(self) -> None:
        state = ServedConnections(
            served={"primary": _conn("primary")},
            default="primary",
        )
        assert state.resolve(None).name == "primary"

    def test_no_default_raises_when_omitted(self) -> None:
        state = ServedConnections(served={"a": _conn("a"), "b": _conn("b")}, default=None)

        with pytest.raises(McpError):
            state.resolve(None)

    def test_unknown_conn_raises(self) -> None:
        state = ServedConnections(served={"primary": _conn("primary")}, default="primary")

        with pytest.raises(McpError):
            state.resolve("missing")
