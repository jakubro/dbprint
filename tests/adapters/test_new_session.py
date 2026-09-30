"""`new_session`: an unconnected sibling on the same target, sharing identifier maps, never sessions."""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.adapters import (
    BigqueryAdapter,
    ClickhouseAdapter,
    DatabricksAdapter,
    DuckdbAdapter,
    MysqlAdapter,
    PostgresAdapter,
    RedshiftAdapter,
    SnowflakeAdapter,
)


_ADAPTERS: dict[str, tuple[Any, dict[str, Any], tuple[str, ...]]] = {
    "postgres": (
        PostgresAdapter,
        {"host": "h", "port": "5432", "user": "u", "password": "p"},
        ("_identities",),
    ),
    "mysql": (
        MysqlAdapter,
        {"host": "h", "port": "3306", "user": "u", "password": "p"},
        ("_identities",),
    ),
    "redshift": (
        RedshiftAdapter,
        {"host": "h", "user": "u", "password": "p"},
        ("_identities",),
    ),
    "snowflake": (
        SnowflakeAdapter,
        {"account": "a", "user": "u", "warehouse": "w", "role": "r", "password": "p"},
        ("_identities",),
    ),
    "clickhouse": (
        ClickhouseAdapter,
        {"host": "h"},
        ("_samplable", "_identities"),
    ),
    "bigquery": (
        BigqueryAdapter,
        {"project": "p"},
        ("_ddl_cache", "_identities"),
    ),
    "databricks": (
        DatabricksAdapter,
        {"server_hostname": "h", "http_path": "p", "access_token": "t"},
        ("_identities",),
    ),
    "duckdb": (DuckdbAdapter, {"database": ":memory:"}, ("_identities",)),
}


@pytest.fixture(params=sorted(_ADAPTERS))
def pair(request: pytest.FixtureRequest) -> tuple[Any, Any, tuple[str, ...]]:
    cls, credentials, shared = _ADAPTERS[request.param]
    adapter = cls(credentials, statement_timeout=30)

    return adapter, adapter.new_session(), shared


def test_the_session_is_its_own(pair: tuple[Any, Any, tuple[str, ...]]) -> None:
    adapter, session, _ = pair

    assert type(session) is type(adapter)
    assert session._connection is not adapter._connection
    assert session._connection.params == adapter._connection.params


def test_the_identifier_maps_are_shared_by_reference(
    pair: tuple[Any, Any, tuple[str, ...]],
) -> None:
    adapter, session, shared = pair

    for name in shared:
        assert getattr(session, name) is getattr(adapter, name), name


@pytest.mark.parametrize("vendor", ["postgres", "redshift"])
def test_per_database_sessions_are_never_shared(vendor: str) -> None:
    """A table's database session belongs to one worker, whose sample copies live on it."""

    cls, credentials, _ = _ADAPTERS[vendor]
    adapter: Any = cls(credentials)
    session: Any = adapter.new_session()

    assert session._sessions is not adapter._sessions
    assert list(session._sessions.values()) == [session._connection]
