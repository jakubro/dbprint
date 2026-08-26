"""Implicit connection resolution: which connection runs when none is named.

A supplied CONN wins, then the `auto: true` set in declaration order, then the sole
connection when only one is defined. Anything else - no connections at all, an unknown CONN,
or several defined with none marked `auto` - raises `ConnectionResolutionError`.
"""

from __future__ import annotations

from dbprint.config import ConnectionConfig, ProjectConfig


class ConnectionResolutionError(ValueError):
    """Raised when implicit resolution cannot pick a connection set."""


def resolve(project_config: ProjectConfig, conn_arg: str | None) -> list[ConnectionConfig]:
    """Return the ordered list of connections to run, or raise `ConnectionResolutionError`."""

    connections = project_config.connections

    if not connections:
        raise ConnectionResolutionError(
            ".dbprint.yaml defines no connections. Add at least one under `connections:`.",
        )

    if conn_arg is not None:
        if conn_arg not in connections:
            known = sorted(connections)

            raise ConnectionResolutionError(f"unknown connection {conn_arg!r}. Known: {known}.")

        return [connections[conn_arg]]

    auto_set = [c for c in connections.values() if c.auto]

    if auto_set:
        return auto_set

    if len(connections) == 1:
        return list(connections.values())

    known = sorted(connections)

    raise ConnectionResolutionError(
        f"no CONN supplied and multiple connections defined: {known}. "
        f"Pass one as the positional argument, or mark one or more with `auto: true`.",
    )
