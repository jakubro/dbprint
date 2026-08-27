"""JSON-RPC error mapping per MCP.md 8: handlers raise `McpError`, the adapter maps the code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


JsonRpcCode = Literal[
    -32601,  # MethodNotFound
    -32602,  # InvalidParams
    -32603,  # InternalError
]


@dataclass(frozen=True)
class McpError(Exception):
    """A typed error the SDK adapter maps to a JSON-RPC error response."""

    code: JsonRpcCode
    detail: str

    def __str__(self) -> str:
        return self.detail


# Constructors per MCP.md 8 trigger list.


def unknown_table(table: str, connection: str) -> McpError:
    """The requested table is absent from that connection's manifest."""

    return McpError(
        -32602,
        f"table {table!r} not found in connection {connection!r}. "
        f"Run dbprint list {connection} for valid names.",
    )


def unknown_connection(connection: str, configured: list[str]) -> McpError:
    """The requested connection is not configured in .dbprint.yaml."""

    return McpError(
        -32602,
        f"connection {connection!r} not in .dbprint.yaml. Configured: {sorted(configured)}",
    )


def unserved_connection(connection: str, served: list[str]) -> McpError:
    """The connection is configured, but this server instance was not started with it."""

    return McpError(
        -32602,
        f"connection {connection!r} is configured but not served by this instance. "
        f"Served: {sorted(served)}. Restart the server naming it to serve it.",
    )


def malformed_pattern(pattern: str) -> McpError:
    """The caller supplied a table pattern fnmatch cannot compile."""

    return McpError(-32602, f"pattern {pattern!r} is malformed fnmatch.")


def missing_table_argument(value: str) -> McpError:
    """`table` must be a non-empty string; the SDK runs no inputSchema check before dispatch."""

    return McpError(-32602, f"table {value!r} must be a non-empty string.")


def no_default_connection(configured: list[str]) -> McpError:
    """No connection was named and the config does not imply one."""

    return McpError(
        -32602,
        f"no default connection; pass conn explicitly. Configured: {sorted(configured)}",
    )


def missing_optional_artifact(artifact: str, table: str) -> McpError:
    """An optional artifact was requested for a table that has none."""

    return McpError(
        -32602,
        f"{artifact} is optional and not authored for table {table!r}.",
    )


def missing_optional_connection_artifact(artifact: str, connection: str) -> McpError:
    """An optional connection-grain artifact was requested for a connection that has none."""

    return McpError(
        -32602,
        f"{artifact} is optional and not authored for connection {connection!r}.",
    )


def manifest_references_missing_file(artifact: str, path: str) -> McpError:
    """The print is internally inconsistent: manifest cites an absent file."""

    return McpError(
        -32603,
        f"manifest references {artifact} but file is absent at {path}. Re-run dbprint generate.",
    )


def no_diff_available(path: str) -> McpError:
    """No diff.yaml: a top-level sibling of manifest.yaml, never an artifact it references."""

    return McpError(
        -32603,
        f"no diff available at {path}. Run `dbprint diff` or `dbprint generate` first.",
    )


def no_reading_guide_available(path: str) -> McpError:
    """No reading.md: a top-level sibling of manifest.yaml, never an artifact it references."""

    return McpError(
        -32603,
        f"no reading guide available at {path}. Run `dbprint generate` first.",
    )


def undeclared_artifact_kind(kind: str, table: str) -> McpError:
    """The manifest never declares this kind for this table - a caller error, not corruption."""

    return McpError(
        -32602,
        f"{kind} is not declared for table {table!r} - check the object's type before "
        "requesting it; the manifest does not declare every kind for every object.",
    )


def yaml_parse_error(path: str, message: str) -> McpError:
    """An artifact on disk is not parseable YAML."""

    return McpError(-32603, f"{path}: YAML parse error: {message}")


def malformed_manifest(path: str, detail: str) -> McpError:
    """A manifest parsed but is not the shape every reader below it walks."""

    return McpError(-32603, f"{path}: {detail}")


def malformed_uri(uri: str) -> McpError:
    """The caller supplied a URI outside the dbprint:// scheme."""

    return McpError(-32602, f"URI {uri!r} does not match the dbprint:// scheme.")


def unknown_tool(name: str, available: list[str]) -> McpError:
    """The caller invoked a tool this server does not expose."""

    return McpError(
        -32601,
        f"unknown tool {name!r}. Available: {sorted(available)}",
    )


def invalid_enum_argument(field: str, value: object, allowed: list[str]) -> McpError:
    """An argument's value is outside its tool's own advertised `inputSchema` enum."""

    return McpError(-32602, f"{field} {value!r} must be one of {allowed}.")


def invalid_minimum_argument(field: str, value: object, minimum: int) -> McpError:
    """An argument's value is below its tool's own advertised `inputSchema` minimum."""

    return McpError(-32602, f"{field} {value!r} must be an integer >= {minimum}.")


def unknown_section(document: str, section: str, available: list[str]) -> McpError:
    """The caller named a section number no heading in the document carries."""

    return McpError(
        -32602,
        f"section {section!r} not found in {document}. Available: {sorted(available)}",
    )
