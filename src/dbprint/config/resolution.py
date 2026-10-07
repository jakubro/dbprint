"""Which connections run: a named CONNECTION, else the `auto: true` set in declaration order, else
a sole connection; anything else raises `ConnectionResolutionError`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .project import ConnectionConfig, ProjectConfig


class ConnectionResolutionError(ValueError):
    """Raised when implicit resolution cannot pick a connection set."""


@dataclass(frozen=True)
class ResolvedConnections:
    """The connections a run or a server takes, in declaration order.

    `default` is the one a request naming no connection means - None when several are peers.
    """

    connections: list[ConnectionConfig]
    default: str | None


def resolve(project_config: ProjectConfig, conn_arg: str | None) -> list[ConnectionConfig]:
    """Return the ordered list of connections to run, or raise `ConnectionResolutionError`."""

    return resolve_connections(project_config, conn_arg).connections


def resolve_connections(project_config: ProjectConfig, conn_arg: str | None) -> ResolvedConnections:
    """The connections `conn_arg` names, else the `auto` set, else the sole one, with a default."""

    connections = project_config.connections

    if not connections:
        raise ConnectionResolutionError(
            ".dbprint.yaml defines no connections. Add at least one under `connections:`.",
        )

    if conn_arg is not None:
        if conn_arg not in connections:
            known = sorted(connections)

            raise ConnectionResolutionError(f"unknown connection {conn_arg!r}. Known: {known}.")

        return ResolvedConnections([connections[conn_arg]], conn_arg)

    auto_set = [c for c in connections.values() if c.auto]

    if auto_set:
        return ResolvedConnections(auto_set, auto_set[0].name if len(auto_set) == 1 else None)

    if len(connections) == 1:
        only = next(iter(connections.values()))

        return ResolvedConnections([only], only.name)

    known = sorted(connections)

    raise ConnectionResolutionError(
        f"no CONNECTION supplied and multiple connections defined: {known}. "
        f"Pass one as the positional argument, or mark one or more with `auto: true`.",
    )
