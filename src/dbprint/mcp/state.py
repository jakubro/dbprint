"""Multi-connection state + default resolution per MCP.md 5; built once by build()."""

from __future__ import annotations

from dataclasses import dataclass, field

from dbprint.config import ConnectionConfig, ProjectConfig
from dbprint.config.resolution import ConnectionResolutionError, resolve_connections
from . import errors
from .parse_cache import ParseCache


@dataclass(frozen=True)
class ServedConnections:
    """The connections this server exposes, the optional default, and the parsed print files.

    `configured` lists every declared connection, served or not (MCP.md 5.2); default: `served`.
    """

    served: dict[str, ConnectionConfig]
    default: str | None
    configured: frozenset[str] = field(default_factory=frozenset)
    files: ParseCache = field(default_factory=ParseCache, compare=False)

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

    configured_set = configured if configured is not None else frozenset(project_config.connections)

    try:
        resolved = resolve_connections(project_config, conn_arg)
    except ConnectionResolutionError as exc:
        if conn_arg is not None:
            raise errors.unknown_connection(conn_arg, list(configured_set)) from exc

        raise errors.no_default_connection(list(configured_set)) from exc

    return ServedConnections(
        served={c.name: c for c in resolved.connections},
        default=resolved.default,
        configured=configured_set,
    )
