"""Mapping from `adapter` string in ConnectionConfig to adapter class.

The CLI constructs a connection's adapter through here so the Engine stays adapter-agnostic:
it receives a pre-built adapter and never imports a concrete adapter module.
"""

from __future__ import annotations

from dbprint.adapters import Adapter, MysqlAdapter, PostgresAdapter, SnowflakeAdapter


ADAPTERS: dict[str, type[Adapter]] = {
    "mysql": MysqlAdapter,
    "postgres": PostgresAdapter,
    "snowflake": SnowflakeAdapter,
}


def get_adapter_class(kind: str) -> type[Adapter]:
    """Return the adapter class for `kind`; raise KeyError when unknown."""

    if kind not in ADAPTERS:
        raise KeyError(f"unknown adapter kind {kind!r}. Known adapters: {sorted(ADAPTERS)}")

    return ADAPTERS[kind]
