"""Multi-connection state + default resolution per MCP.md 5; built once by build()."""

from __future__ import annotations

from dataclasses import dataclass, field

from dbprint.config import ConnectionConfig, ProjectConfig
from . import errors


@dataclass(frozen=True)
class ServedConnections:
    """The connections this server exposes plus the optional default.

    `configured` names every connection `.dbprint.yaml` declares, not only the served subset -
    `resolve()` needs both to tell "not configured" from "configured but not served" (MCP.md
    5.2). Defaults to `served`'s own keys.
    """

    served: dict[str, ConnectionConfig]
    default: str | None
    configured: frozenset[str] = field(default_factory=frozenset)

    def resolve(self, conn: str | None) -> ConnectionConfig:
        """Return the ConnectionConfig for `conn`, falling back to the default.

        Raises McpError (InvalidParams) per MCP.md 5.2 when unknown, or omitted with no default.
        """

        configured = self.configured or frozenset(self.served)

        if conn is not None:
            if conn not in self.served:
                if conn in configured:
                    raise errors.unserved_connection(conn, list(self.served))

                raise errors.unknown_connection(conn, list(configured))

            return self.served[conn]

        if self.default is None:
            raise errors.no_default_connection(list(self.served))

        return self.served[self.default]


def build(
    project_config: ProjectConfig,
    conn_arg: str | None,
    *,
    configured: frozenset[str] | None = None,
) -> ServedConnections:
    """Resolve the served set + default per MCP.md 5.1.

    `configured` names every connection `.dbprint.yaml` declares - pass it when
    `project_config` has already been narrowed to the served subset; omitted, it defaults to
    `project_config`'s own connections.
    """

    connections = project_config.connections
    configured_set = configured if configured is not None else frozenset(connections)

    if conn_arg is not None:
        if conn_arg not in connections:
            raise errors.unknown_connection(conn_arg, list(configured_set))

        return ServedConnections(
            served={conn_arg: connections[conn_arg]},
            default=conn_arg,
            configured=configured_set,
        )

    auto_set = [c for c in connections.values() if c.auto]

    if auto_set:
        return ServedConnections(
            served={c.name: c for c in auto_set},
            default=auto_set[0].name if len(auto_set) == 1 else None,
            configured=configured_set,
        )

    if len(connections) == 1:
        only = next(iter(connections.values()))

        return ServedConnections(
            served={only.name: only},
            default=only.name,
            configured=configured_set,
        )

    # Caller should have rejected this at the CLI layer already.
    raise errors.no_default_connection(list(configured_set))
