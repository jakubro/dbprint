"""dbprint MCP server package per MCP.md."""

from __future__ import annotations

from .errors import McpError
from .resources import (
    ReadResult,
    ResourceEntry,
    ResourceRef,
    enumerate_for,
    parse_uri,
    read,
)
from .server import build_server, serve_http, serve_stdio
from .state import ServedConnections
from .state import build as build_state
from .tools import TOOL_DEFINITIONS, TOOL_NAMES, ToolDef, dispatch


__all__ = [
    "TOOL_DEFINITIONS",
    "TOOL_NAMES",
    "McpError",
    "ReadResult",
    "ResourceEntry",
    "ResourceRef",
    "ServedConnections",
    "ToolDef",
    "build_server",
    "build_state",
    "dispatch",
    "enumerate_for",
    "parse_uri",
    "read",
    "serve_http",
    "serve_stdio",
]
