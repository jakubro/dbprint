"""MCP SDK adapter - imported only by `cli.commands.serve`, which gates on the [mcp] extra."""

from __future__ import annotations

import contextlib
import json
from typing import Any

import anyio
from mcp.server import NotificationOptions, Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError as SdkMcpError
from mcp.types import (
    SERVER_INFO_META_KEY,
    CallToolRequestParams,
    CallToolResult,
    ListResourceTemplatesResult,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
    ResourceTemplate,
    TextContent,
    TextResourceContents,
    Tool,
)
from mcp.types import Resource as McpResource

from dbprint import __version__ as DBPRINT_VERSION
from . import errors, paging, resources, tools
from .instructions import instructions
from .state import ServedConnections


SERVER_NAME = "dbprint"
SERVER_TITLE = "dbprint"
SERVER_DESCRIPTION = (
    "A database's schema, value lists and joins, from a snapshot in your repo - no database "
    "connection."
)
SERVER_WEBSITE = "https://jakubro.github.io/dbprint/"


def build_server(state: ServedConnections) -> Server:
    """Build a wired but not-yet-running Server; call run_stdio()/run_http() to serve."""

    async def list_resources(
        _ctx: ServerRequestContext,
        params: PaginatedRequestParams | None,
    ) -> ListResourcesResult:
        try:
            listed = resources.list_page(
                state,
                params.cursor if params else None,
                _as_sent(server, ListResourcesResult(resources=[])),
            )
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
                for e in listed.entries
            ],
            next_cursor=listed.next_cursor,
        )

    async def list_resource_templates(
        _ctx: ServerRequestContext,
        _params: PaginatedRequestParams | None,
    ) -> ListResourceTemplatesResult:
        return ListResourceTemplatesResult(
            resource_templates=[
                ResourceTemplate(
                    uri_template=t.uri_template,
                    name=t.name,
                    description=t.description,
                    mime_type=t.mime_type,
                )
                for t in resources.TEMPLATES
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
                    meta=result.meta(),
                ),
            ],
        )

    async def list_tools(
        _ctx: ServerRequestContext,
        params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        try:
            definitions, next_cursor = tools.list_page(
                params.cursor if params else None,
                _as_sent(server, ListToolsResult(tools=[])),
            )
        except errors.McpError as exc:
            raise SdkMcpError(exc.code, exc.detail) from exc

        return ListToolsResult(
            tools=[Tool.model_validate(t.wire()) for t in definitions],
            next_cursor=next_cursor,
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

    server = _LiveInstructionsServer(
        state,
        SERVER_NAME,
        version=DBPRINT_VERSION,
        title=SERVER_TITLE,
        description=SERVER_DESCRIPTION,
        website_url=SERVER_WEBSITE,
        on_list_resources=list_resources,
        on_list_resource_templates=list_resource_templates,
        on_read_resource=read_resource,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )

    return server


def initialization_options(server: Server) -> InitializationOptions:
    """Standard initialization options matching MCP.md 2 capabilities."""

    return InitializationOptions(
        server_name=SERVER_NAME,
        server_version=DBPRINT_VERSION,
        title=server.title,
        description=server.description,
        website_url=server.website_url,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(
                prompts_changed=False,
                resources_changed=False,
                tools_changed=False,
            ),
            experimental_capabilities={},
        ),
        instructions=server.instructions,
    )


async def run_stdio(server: Server) -> None:
    """Serve the configured Server over stdio (default transport)."""

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, initialization_options(server))


async def run_http(server: Server, host: str, port: int) -> None:
    """HTTP/SSE server bound to loopback, answering only a loopback `Host` and `Origin` for `port`.

    Non-loopback bind addresses are rejected at the CLI layer.
    """

    from mcp.server.sse import SseServerTransport
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    transport = SseServerTransport(
        "/messages/",
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"http://{name}" for name in hosts],
        ),
    )
    app = Starlette(
        routes=[
            Route("/sse", endpoint=_SseEndpoint(transport, server)),
            Mount("/messages/", app=transport.handle_post_message),
        ],
    )

    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    http = uvicorn.Server(config)

    try:
        await http.serve()
    finally:
        # A cancelled serve skips the shutdown that closes these; uvicorn sets them only once bound.
        for listener in getattr(http, "servers", ()):
            listener.close()


def serve_stdio(state: ServedConnections) -> None:
    """Blocking convenience: build server + run stdio until EOF/SIGTERM."""

    server = build_server(state)
    anyio.run(run_stdio, server)


def serve_http(state: ServedConnections, host: str, port: int) -> None:
    """Blocking convenience: build server + run HTTP/SSE."""

    server = build_server(state)
    anyio.run(run_http, server, host, port)


class _LiveInstructionsServer(Server):
    def __init__(self, state: ServedConnections, *args: Any, **kwargs: Any) -> None:
        self._state = state
        super().__init__(*args, **kwargs)

    @property
    def instructions(self) -> str:
        return instructions(self._state)

    @instructions.setter
    def instructions(self, value: str | None) -> None:
        # The base `__init__` assigns the attribute; the property is the only source.
        del value


class _SseEndpoint:
    """The `/sse` route as a raw ASGI app, so a request the transport refused ends quietly.

    `connect_sse` sends its own 421/403 and then raises `ValueError`; any response sent after it
    would be a second one.
    """

    def __init__(self, transport: Any, server: Server) -> None:
        self._transport = transport
        self._server = server

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async with contextlib.AsyncExitStack() as stack:
            try:
                streams = await stack.enter_async_context(
                    self._transport.connect_sse(scope, receive, send),
                )
            except ValueError:
                return

            await self._server.run(streams[0], streams[1], initialization_options(self._server))


def _as_sent(server: Server, empty: ListToolsResult | ListResourcesResult) -> paging.Measure:
    # A 2026-era connection adds the result's own fields and a serverInfo stamp to the reply.
    envelope = empty.model_dump(by_alias=True, mode="json", exclude_none=True)
    envelope["_meta"] = {SERVER_INFO_META_KEY: server.server_info_stamp}

    return lambda reply: paging.compact({**envelope, **reply})
