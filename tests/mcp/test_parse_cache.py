"""A served print is parsed once per file version, and every call still sees what is on disk."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from dbprint.config import ConnectionConfig
from dbprint.mcp import McpError, ServedConnections, dispatch, resources
from dbprint.mcp.tools import TOOL_NAMES
from dbprint.spec import artifact_yaml


_TABLE = "arboretum.seedbank.accession"


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every text the shared loader parses, in order."""

    seen: list[str] = []
    real = artifact_yaml.load

    def counting(text: str, **kwargs: Any) -> Any:
        seen.append(text)

        return real(text, **kwargs)

    monkeypatch.setattr(artifact_yaml, "load", counting)

    return seen


def test_a_repeated_call_parses_nothing(primary_conn: ConnectionConfig, parses: list[str]) -> None:
    state = _served(primary_conn)
    calls = [
        ("search_columns", {"pattern": "*_id"}),
        ("get_table_context", {"table": _TABLE}),
        ("get_diff", {"table": _TABLE}),
    ]

    first = [dispatch(state, name, arguments) for name, arguments in calls]
    parsed_first = len(parses)
    second = [dispatch(state, name, arguments) for name, arguments in calls]

    assert parsed_first > 0
    assert len(parses) == parsed_first
    assert second == first


def test_a_rewritten_file_is_the_only_one_parsed_again(
    primary_conn: ConnectionConfig,
    parses: list[str],
) -> None:
    state = _served(primary_conn)
    dispatch(state, "get_table_context", {"table": _TABLE, "format": "json"})
    path = _statistics(primary_conn)
    rewritten = path.read_text(encoding="utf-8").replace("columns:", "columns:\n  # rewritten", 1)
    _replace_keeping_mtime(path, rewritten + "\n# grown\n", same_size=False)
    parses.clear()

    reply = dispatch(state, "get_table_context", {"table": _TABLE, "format": "json"})

    assert parses == [path.read_text(encoding="utf-8")]
    assert isinstance(reply, dict)


def test_a_file_replaced_with_the_same_mtime_and_size_is_read_again(
    primary_conn: ConnectionConfig,
) -> None:
    state = _served(primary_conn)
    before = _value_resolution(state)
    path = _statistics(primary_conn)
    text = path.read_text(encoding="utf-8")
    assert "seed_count" in text
    _replace_keeping_mtime(path, text.replace("seed_count", "seed_c0unt"), same_size=True)

    with pytest.raises(McpError) as raised:
        _value_resolution(state)

    assert before["column"] == "seed_count"
    assert "seed_count" in str(raised.value)
    assert "seed_c0unt" in str(raised.value)


def test_a_deleted_file_fails_as_an_uncached_server_does(primary_conn: ConnectionConfig) -> None:
    state = _served(primary_conn)
    _value_resolution(state)
    _statistics(primary_conn).unlink()

    with pytest.raises(McpError) as cached:
        _value_resolution(state)

    with pytest.raises(McpError) as fresh:
        _value_resolution(_served(primary_conn))

    assert (cached.value.code, cached.value.detail) == (fresh.value.code, fresh.value.detail)


def test_a_fixed_file_is_read_without_a_restart(primary_conn: ConnectionConfig) -> None:
    state = _served(primary_conn)
    path = _statistics(primary_conn)
    good = path.read_text(encoding="utf-8")
    path.write_text("columns: [unclosed\n", encoding="utf-8")

    with pytest.raises(McpError) as corrupt:
        _value_resolution(state)

    path.write_text(good, encoding="utf-8")
    fixed = _value_resolution(state)

    assert str(path) in corrupt.value.detail
    assert fixed["column"] == "seed_count"


def test_no_tool_or_resource_mutates_a_cached_document(primary_conn: ConnectionConfig) -> None:
    state = _served(primary_conn)

    for _ in range(2):
        for arguments in _every_table_context_call():
            dispatch(state, "get_table_context", arguments)

        for name in TOOL_NAMES:
            if name not in {"get_table_context", "resolve_value", "get_reference"}:
                dispatch(state, name, {})

        dispatch(state, "search_columns", {"pattern": "*"})
        _value_resolution(state)

        for entry in resources.enumerate_for(state):
            if not entry.uri.startswith("dbprint://reference/"):
                resources.read(state, entry.uri)

    cached = state.files._entries

    assert len(cached) > 10
    assert {path: data for path, (_, data) in cached.items()} == {
        path: artifact_yaml.load(path.read_text(encoding="utf-8")) for path in cached
    }


def _served(conn: ConnectionConfig) -> ServedConnections:
    return ServedConnections(served={conn.name: conn}, default=conn.name)


def _statistics(conn: ConnectionConfig) -> Path:
    return conn.output / conn.name / "arboretum" / "seedbank" / "accession" / "statistics.yaml"


def _value_resolution(state: ServedConnections) -> dict[str, Any]:
    reply = dispatch(
        state,
        "resolve_value",
        {"table": _TABLE, "column": "seed_count", "text": "12"},
    )
    assert isinstance(reply, dict)

    return reply


def _every_table_context_call() -> list[dict[str, Any]]:
    return [
        {"table": _TABLE, "format": fmt, "purpose": purpose}
        for fmt in ("md", "json", "yaml")
        for purpose in ("profile", "query")
    ]


def _replace_keeping_mtime(path: Path, text: str, *, same_size: bool) -> None:
    before = path.stat()

    if same_size:
        assert len(text.encode()) == before.st_size

    staged = path.with_name(path.name + ".staged")
    staged.write_text(text, encoding="utf-8")
    os.replace(staged, path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
