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

# Sent unprompted on every connect (MCP.md 2); Claude Code keeps the first 2,048 characters, so
# routing leads and the text stays under that. test_server.py binds its rules to SPEC 2.2.8/2.3/4.4.2/7.1.
SERVER_DESCRIPTION = (
    "Serves committed dbprint prints: a database's structure and per-column "
    "statistics, captured offline. Answer from these tools, not from the print's "
    "files: a file read directly carries none of the scope, redaction and "
    "unmeasured handling the tools apply.\n\n"
    "Which tool answers what:\n"
    "- Writing SQL: get_table_context with purpose: query for every table the "
    "query touches - DDL, the Joins list, the data dictionary, value lists.\n"
    "- A filter value whose stored spelling is not listed in full there: "
    "resolve_value; use every spelling it returns.\n"
    "- Which table or column holds a fact, by name, type, shape (email, phone) "
    "or what its notes say: search_columns (`text` searches the notes).\n"
    "- Which tables exist, their row counts, whether their statistics are "
    "stale: list_tables with detail: true.\n"
    "- What changed since the previous run: get_diff.\n"
    "- What a field or a finding's spec_ref means: get_reference.\n"
    "- The raw manifest index: get_manifest.\n\n"
    "Reading an answer:\n"
    "- Scope. A table with a scope block was read in part, by a sample or a "
    "row filter; its statistics describe the rows scanned, not the table. "
    "Under a sample a count MAY be multiplied by row_count / rows_scanned for "
    "a rough table-wide figure; under a filter nothing rescales, and a ratio, "
    "bound, percentile, sum or mean never does.\n"
    "- Inference. Fields under inferred, and relationships whose detection is "
    "inferred or measured, are dbprint's guesses, not database constraints. "
    "No sensitivity on a column means nothing was detected, not that the "
    "column is safe to publish.\n"
    "- Absence. A missing field is not zero. A field named in an unmeasured "
    "list was not measured this run: its value is unknown, not none."
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
