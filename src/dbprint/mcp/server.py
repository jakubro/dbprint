"""MCP SDK adapter - imported only by `cli.commands.serve`, which gates on the [mcp] extra."""

from __future__ import annotations

import json
from typing import Any

import anyio
from mcp.server import NotificationOptions, Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError as SdkMcpError
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
    TextContent,
    TextResourceContents,
    Tool,
)
from mcp.types import Resource as McpResource

from dbprint import __version__ as DBPRINT_VERSION
from . import errors, resources, tools
from .state import ServedConnections


SERVER_NAME = "dbprint"

# Delivered unprompted on every connect (MCP.md 2) - the floor that prevents a silent
# order-of-magnitude error, plus a pointer to the reading resource. Every field it names
# exists on the artifact by the time a client reads this (SPEC 2.2.8, 4.1.3, 2.3.8).
SERVER_DESCRIPTION = (
    "Reads committed dbprint prints - a database's structure and per-column "
    "statistics, captured offline. Three things decide whether an answer "
    "drawn from them is right.\n\n"
    "Scope. Counts and ratios describe the rows that were scanned, which is "
    "not always the whole table. A column carrying a population marker was "
    "sampled; rescale by rows_scanned / row_count before comparing against a "
    "table-wide figure.\n\n"
    "Inference. Everything under inferred is the producer's guess, not the "
    "database's assertion - candidate_key, looks_like, sensitivity, and any "
    "relationship marked detection: inferred. Each publishes the evidence it "
    "rests on; read that before acting on it.\n\n"
    "Absence. A missing field means the producer did not or could not "
    "measure it - never that the value is zero, none, or safe to assume.\n\n"
    "Start from search_columns to locate a fact across the print; the "
    "reading guide resource covers the rest."
)


def build_server(state: ServedConnections) -> Server:
    """Build a wired but not-yet-running Server; call run_stdio()/run_http() to serve."""

    async def list_resources(
        _ctx: ServerRequestContext,
        _params: PaginatedRequestParams | None,
    ) -> ListResourcesResult:
        try:
            entries = resources.enumerate_for(state)
        except errors.McpError as exc:
            # Mapped verbatim to ErrorData, as read_resource does below - unmapped, the SDK
            # sanitizes errors.McpError into an opaque internal error instead of its code.
            raise SdkMcpError(exc.code, exc.detail) from exc

        return ListResourcesResult(
            resources=[
                McpResource(
                    uri=e.uri,
                    name=e.name,
                    description=e.description,
                    mime_type=e.mime_type,
                )
                for e in entries
            ],
        )

    async def read_resource(
        _ctx: ServerRequestContext,
        params: ReadResourceRequestParams,
    ) -> ReadResourceResult:
        try:
            result = resources.read(state, params.uri)
        except errors.McpError as exc:
            # Mapped verbatim to ErrorData; anything else takes the dispatcher's code-0 fallback.
            raise SdkMcpError(exc.code, exc.detail) from exc

        return ReadResourceResult(
            contents=[
                TextResourceContents(
                    uri=params.uri,
                    text=result.content,
                    mime_type=result.mime_type,
                ),
            ],
        )

    async def list_tools(
        _ctx: ServerRequestContext,
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(name=t.name, description=t.description, input_schema=t.input_schema)
                for t in tools.TOOL_DEFINITIONS
            ],
        )

    async def call_tool(
        _ctx: ServerRequestContext,
        params: CallToolRequestParams,
    ) -> CallToolResult:
        # The SDK does not wrap a raised exception into isError=True, so an escaped fault would
        # reach the wire as a protocol-level error - the shape MCP.md 8.2 forbids here. The
        # catch is broad because MCP.md 8.3 requires no fault to crash the connection.
        try:
            result = tools.dispatch(state, params.name, params.arguments or {})
        except errors.McpError as exc:
            return CallToolResult(
                content=[TextContent(type="text", text=exc.detail)],
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 - returns is_error, never raises (MCP.md 8.2)
            return CallToolResult(
                content=[TextContent(type="text", text=str(exc))],
                is_error=True,
            )

        # `get_table_context` returns a bare string for md/yaml (MCP.md 4.1); others a dict.
        text = result if isinstance(result, str) else json.dumps(result, default=str, indent=2)

        return CallToolResult(content=[TextContent(type="text", text=text)], is_error=False)

    return Server(
        SERVER_NAME,
        version=DBPRINT_VERSION,
        instructions=SERVER_DESCRIPTION,
        on_list_resources=list_resources,
        on_read_resource=read_resource,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def initialization_options(server: Server) -> InitializationOptions:
    """Standard initialization options matching MCP.md 2 capabilities."""

    return InitializationOptions(
        server_name=SERVER_NAME,
        server_version=DBPRINT_VERSION,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(
                prompts_changed=False,
                resources_changed=False,
                tools_changed=False,
            ),
            experimental_capabilities={},
        ),
        instructions=SERVER_DESCRIPTION,
    )


async def run_stdio(server: Server) -> None:
    """Serve the configured Server over stdio (default transport)."""

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, initialization_options(server))


async def run_http(server: Server, host: str, port: int) -> None:
    """HTTP/SSE server bound to loopback only; non-loopback hosts rejected at the CLI layer."""

    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    transport = SseServerTransport("/messages/")

    async def handle_sse(request: Any) -> None:
        async with transport.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], initialization_options(server))

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=transport.handle_post_message),
        ],
    )

    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()


def serve_stdio(state: ServedConnections) -> None:
    """Blocking convenience: build server + run stdio until EOF/SIGTERM."""

    server = build_server(state)
    anyio.run(run_stdio, server)


def serve_http(state: ServedConnections, host: str, port: int) -> None:
    """Blocking convenience: build server + run HTTP/SSE."""

    server = build_server(state)
    anyio.run(run_http, server, host, port)
