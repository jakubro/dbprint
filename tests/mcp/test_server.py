"""Server build smoke tests plus wire-level error-model tests over both transports.

`TestRealTransportErrorPaths` repeats the error tests over a real stdio subprocess: the
in-process `Client(server)` dispatches directly and sanitizes an escaped exception into an
internal-error shape, hiding a handler that forgot to map its own errors.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
import yaml
from mcp import ClientSession
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError as SdkMcpError
from mcp.types import CallToolResult, TextContent

from dbprint.config import ConnectionConfig
from dbprint.mcp import ServedConnections, build_server
from dbprint.mcp import tools as tools_module
from .conftest import StdioServer


def _state_for(conn: ConnectionConfig) -> ServedConnections:
    return ServedConnections(served={conn.name: conn}, default=conn.name)


def _read_resource_error_code(conn: ConnectionConfig, uri: str) -> int:
    """Drive `resources/read` over the SDK's in-process transport; return the JSON-RPC code."""

    async def _run() -> int:
        server = build_server(_state_for(conn))

        async with Client(server) as client:
            try:
                await client.read_resource(uri)
            except SdkMcpError as exc:
                return exc.error.code

            raise AssertionError(f"{uri} did not raise an MCPError")

    return anyio.run(_run)


def _call_tool(conn: ConnectionConfig, name: str, arguments: dict[str, object]) -> CallToolResult:
    """Drive a real `tools/call` over the SDK's in-process transport."""

    async def _run() -> CallToolResult:
        server = build_server(_state_for(conn))

        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(_run)


def _list_and_read(conn: ConnectionConfig, uri: str) -> tuple[str | None, str]:
    """Drive resources/list then resources/read; return (listed mimeType, read mimeType)."""

    async def _run() -> tuple[str | None, str]:
        server = build_server(_state_for(conn))

        async with Client(server) as client:
            listed = await client.list_resources()
            by_uri = {str(r.uri): r.mime_type for r in listed.resources}
            read = await client.read_resource(uri)
            read_mime = read.contents[0].mime_type
            assert read_mime is not None

            return by_uri.get(uri), read_mime

    return anyio.run(_run)


def _stdio_params(server: StdioServer) -> StdioServerParameters:
    return StdioServerParameters(
        command=server.command,
        args=["serve", server.conn_name, "--project", str(server.project_dir)],
    )


def _stdio_call_tool(
    server: StdioServer,
    name: str,
    arguments: dict[str, object],
) -> CallToolResult:
    """Drive a real `tools/call` over a subprocess speaking the actual stdio wire."""

    async def _run() -> CallToolResult:
        async with (
            stdio_client(_stdio_params(server)) as (read, write),
            ClientSession(
                read,
                write,
            ) as session,
        ):
            await session.initialize()

            return await session.call_tool(name, arguments)

    return anyio.run(_run)


def _stdio_read_resource_error(server: StdioServer, uri: str) -> SdkMcpError:
    """Drive `resources/read` over the real stdio wire; return the raised MCPError."""

    async def _run() -> SdkMcpError:
        async with (
            stdio_client(_stdio_params(server)) as (read, write),
            ClientSession(
                read,
                write,
            ) as session,
        ):
            await session.initialize()

            try:
                await session.read_resource(uri)
            except SdkMcpError as exc:
                return exc

            raise AssertionError(f"{uri} did not raise an MCPError")

    return anyio.run(_run)


def _stdio_list_resources_error_code(server: StdioServer) -> int:
    """Drive `resources/list` over the real stdio wire; return the JSON-RPC code."""

    async def _run() -> int:
        async with (
            stdio_client(_stdio_params(server)) as (read, write),
            ClientSession(
                read,
                write,
            ) as session,
        ):
            await session.initialize()

            try:
                await session.list_resources()
            except SdkMcpError as exc:
                return exc.error.code

            raise AssertionError("list_resources did not raise an MCPError")

    return anyio.run(_run)


def _add_plain_view(server: StdioServer, fqn: str = "public.a_view") -> None:
    """Add a plain-view manifest entry (DDL only, no statistics/relationships) for a test."""

    print_root = server.project_dir / "prints" / server.conn_name
    manifest_path = print_root / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    *namespace, name = fqn.split(".")
    view_dir = print_root / Path(*namespace) / name
    view_dir.mkdir(parents=True, exist_ok=True)
    (view_dir / "ddl.sql").write_text(f"CREATE VIEW {fqn} AS SELECT 1;\n")
    manifest["tables"][fqn] = {
        "type": "view",
        "path": "/".join([*namespace, name]),
        "artifacts": {"ddl": "ddl.sql"},
        "columns": 1,
        "profiled_at": manifest["generated_at"],
    }
    manifest_path.write_text(yaml.safe_dump(manifest))


class TestBuildServer:
    def test_returns_server_instance(self, primary_conn: ConnectionConfig) -> None:
        from mcp.server import Server

        server = build_server(_state_for(primary_conn))
        assert isinstance(server, Server)

    def test_server_advertises_dbprint_name(self, primary_conn: ConnectionConfig) -> None:
        server = build_server(_state_for(primary_conn))
        assert server.name == "dbprint"

    def test_server_carries_package_version(self, primary_conn: ConnectionConfig) -> None:
        from dbprint import __version__

        server = build_server(_state_for(primary_conn))
        assert server.version == __version__

    def test_handlers_registered(self, primary_conn: ConnectionConfig) -> None:
        server = build_server(_state_for(primary_conn))
        # get_request_handler looks up the internal dispatch table the SDK uses.
        assert server.get_request_handler("resources/list") is not None
        assert server.get_request_handler("resources/read") is not None
        assert server.get_request_handler("tools/list") is not None
        assert server.get_request_handler("tools/call") is not None


class TestReadResourceErrorsReachTheWire:
    """MCP.md 8: a resources/read failure carries the code its trigger publishes."""

    def test_unknown_table_is_invalid_params(self, primary_conn: ConnectionConfig) -> None:
        code = _read_resource_error_code(primary_conn, "dbprint://production/public.ghost/ddl")
        assert code == -32602

    def test_absent_optional_artifact_is_invalid_params(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """seedbank.vault ships with no description.md for real."""

        code = _read_resource_error_code(
            primary_conn,
            "dbprint://production/seedbank.vault/description",
        )
        assert code == -32602

    def test_malformed_uri_is_invalid_params(self, primary_conn: ConnectionConfig) -> None:
        code = _read_resource_error_code(primary_conn, "http://not-dbprint/x")
        assert code == -32602

    def test_malformed_manifest_is_internal_error(self, primary_conn: ConnectionConfig) -> None:
        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest_path.write_text("tables: not_a_mapping\n")

        code = _read_resource_error_code(
            primary_conn,
            "dbprint://production/seedbank.collector/ddl",
        )
        assert code == -32603

    def test_a_params_trigger_and_an_internal_trigger_carry_different_codes(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest_path.write_text("tables: not_a_mapping\n")

        params_code = _read_resource_error_code(primary_conn, "http://not-dbprint/x")
        internal_code = _read_resource_error_code(
            primary_conn,
            "dbprint://production/seedbank.collector/ddl",
        )

        assert params_code != internal_code
        assert params_code == -32602
        assert internal_code == -32603


class TestCallToolErrorsReachTheWire:
    """MCP.md 8.2: a failed tool call is isError=true, not a string prefix."""

    def test_unknown_table_is_a_failed_call(self, primary_conn: ConnectionConfig) -> None:
        result = _call_tool(primary_conn, "get_table_context", {"table": "public.ghost"})
        content = result.content[0]

        assert result.is_error is True
        assert isinstance(content, TextContent)
        assert "not found" in content.text
        assert not content.text.startswith("error:")

    def test_unknown_tool_name_is_a_failed_call(self, primary_conn: ConnectionConfig) -> None:
        result = _call_tool(primary_conn, "not_a_real_tool", {})

        assert result.is_error is True

    def test_unknown_connection_is_a_failed_call(self, primary_conn: ConnectionConfig) -> None:
        result = _call_tool(primary_conn, "list_tables", {"conn": "nonexistent"})

        assert result.is_error is True

    def test_successful_call_is_unaffected(self, primary_conn: ConnectionConfig) -> None:
        result = _call_tool(primary_conn, "list_tables", {})

        assert result.is_error is False

    def test_unanticipated_exception_is_still_a_failed_call_not_a_crash(
        self,
        primary_conn: ConnectionConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An exception the SDK does not turn into isError, so the server must (MCP.md 8.3)."""

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(tools_module, "dispatch", _raise)
        result = _call_tool(primary_conn, "list_tables", {})
        content = result.content[0]

        assert result.is_error is True
        assert isinstance(content, TextContent)
        assert "boom" in content.text

    def test_server_survives_every_failure_and_serves_the_next_request(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """Section 8.3: application-level errors are responses, never a terminated session."""

        async def _run() -> CallToolResult:
            server = build_server(_state_for(primary_conn))

            async with Client(server) as client:
                try:
                    await client.read_resource("dbprint://production/public.ghost/ddl")
                except SdkMcpError:
                    pass

                failed = await client.call_tool("not_a_real_tool", {})
                assert failed.is_error is True

                return await client.call_tool("list_tables", {})

        result = anyio.run(_run)

        assert result.is_error is False


class TestResourceMimeTypeReachesTheWire:
    """MCP.md 3.4: resources/read carries the mimeType resources/list advertised.

    Pinned on the wire payload: a bare `str` return hits the SDK's `text/plain` default.
    """

    def test_ddl_is_application_sql(self, primary_conn: ConnectionConfig) -> None:
        listed, read = _list_and_read(primary_conn, "dbprint://production/seedbank.collector/ddl")

        assert listed == "application/sql"
        assert read == "application/sql"

    def test_statistics_is_application_yaml(self, primary_conn: ConnectionConfig) -> None:
        listed, read = _list_and_read(
            primary_conn,
            "dbprint://production/seedbank.collector/statistics",
        )

        assert listed == "application/yaml"
        assert read == "application/yaml"

    def test_manifest_is_application_yaml(self, primary_conn: ConnectionConfig) -> None:
        listed, read = _list_and_read(primary_conn, "dbprint://production/manifest")

        assert listed == "application/yaml"
        assert read == "application/yaml"

    def test_listed_and_read_never_disagree(self, primary_conn: ConnectionConfig) -> None:
        """The defect's shape: list and read drawing from different sources could drift."""

        listed, read = _list_and_read(
            primary_conn,
            "dbprint://production/seedbank.collector/relationships",
        )

        assert listed == read


class TestHandshakeAdvertisesInstructions:
    def _instructions(self, conn: ConnectionConfig) -> str | None:
        async def _run() -> str | None:
            server = build_server(_state_for(conn))

            async with Client(server) as client:
                return client.instructions

        return anyio.run(_run)

    def test_instructions_carry_the_server_description(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        from dbprint.mcp.server import SERVER_DESCRIPTION

        assert self._instructions(primary_conn) == SERVER_DESCRIPTION


# `SERVER_DESCRIPTION` is delivered unprompted on every connect, so each entry anchors one of its
# claims to the SPEC sentence behind it: a moved or reworded sentence fails here instead.
_SPEC_PATH = Path(__file__).resolve().parents[2] / "docs/format/v1/SPEC.md"

_SERVER_DESCRIPTION_ANCHORS = (
    (
        (
            "is NOT rescalable to table grain by the reader without assuming the "
            "sample is representative"
        ),
        "SPEC 2.2.8's sum-not-rescalable sentence moved",
    ),
    (
        "sampled` and `matched` describe `looks_like` alone, never `epoch_unit` or `sensitivity`",
        "SPEC 4.1.3's sampled/matched scoping sentence moved",
    ),
    (
        'absence means "not detected", never "safe to publish"',
        "SPEC 4.4.2's sensitivity-absence sentence moved",
    ),
)


class TestServerDescriptionSpecBinding:
    """Binds `SERVER_DESCRIPTION`'s own claims to the SPEC sentences that back them."""

    @pytest.mark.parametrize(("needle", "message"), _SERVER_DESCRIPTION_ANCHORS)
    def test_the_cited_spec_sentence_still_holds(self, needle: str, message: str) -> None:
        assert needle in _SPEC_PATH.read_text(), message

    def test_sum_and_other_aggregates_are_not_told_to_rescale(self) -> None:
        from dbprint.mcp.server import SERVER_DESCRIPTION

        assert "sum" in SERVER_DESCRIPTION
        assert "mean" in SERVER_DESCRIPTION
        assert "A ratio, a bound, a percentile, or an aggregate" in SERVER_DESCRIPTION
        assert "assumes the sample is representative" in SERVER_DESCRIPTION

    def test_a_count_still_gets_the_rescale_instruction(self) -> None:
        from dbprint.mcp.server import SERVER_DESCRIPTION

        # The reciprocal, and named as a multiplication: `rows_scanned / row_count` is below 1,
        # so an agent multiplying by it shrinks the very count it meant to scale up.
        assert "multiplying it by row_count / rows_scanned" in SERVER_DESCRIPTION
        assert "rescaled by rows_scanned / row_count" not in SERVER_DESCRIPTION

    def test_the_evidence_promise_is_scoped_to_looks_like(self) -> None:
        from dbprint.mcp.server import SERVER_DESCRIPTION

        assert "looks_like publishes the sampled/matched evidence" in SERVER_DESCRIPTION
        assert "candidate_key's own verdict is recomputable" in SERVER_DESCRIPTION
        assert "sensitivity publishes no evidence at all" in SERVER_DESCRIPTION
        assert "its absence never means safe to publish" in SERVER_DESCRIPTION


class TestToolDescriptionsReachTheWire:
    """MCP.md 4: each tool's advertised description, over the same handshake a client sees."""

    def _descriptions(self, conn: ConnectionConfig) -> dict[str, str]:
        async def _run() -> dict[str, str]:
            server = build_server(_state_for(conn))

            async with Client(server) as client:
                listed = await client.list_tools()

                return {t.name: t.description or "" for t in listed.tools}

        return anyio.run(_run)

    def test_search_columns_is_named_the_entry_point(self, primary_conn: ConnectionConfig) -> None:
        assert "entry point" in self._descriptions(primary_conn)["search_columns"].lower()

    def test_get_diff_names_the_real_filename(self, primary_conn: ConnectionConfig) -> None:
        description = self._descriptions(primary_conn)["get_diff"]
        assert "diff.yaml" in description

    def test_get_manifest_is_framed_as_an_index_not_a_catalogue(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        assert "not a semantic catalogue" in self._descriptions(primary_conn)["get_manifest"]

    def test_get_table_context_names_truncation(self, primary_conn: ConnectionConfig) -> None:
        assert "truncation" in self._descriptions(primary_conn)["get_table_context"]


class TestToolFormatShapesReachTheWire:
    """MCP.md 4.1: the wire payload for each format, not just the handler's return value."""

    def _call(self, conn: ConnectionConfig, arguments: dict[str, object]) -> str:
        async def _run() -> str:
            server = build_server(_state_for(conn))

            async with Client(server) as client:
                result = await client.call_tool("get_table_context", arguments)
                content = result.content[0]
                assert isinstance(content, TextContent)

                return content.text

        return anyio.run(_run)

    def test_md_is_bare_text_not_a_json_envelope(self, primary_conn: ConnectionConfig) -> None:
        text = self._call(primary_conn, {"table": "seedbank.collector", "format": "md"})

        assert not text.startswith("{")
        assert "CREATE TABLE" in text

    def test_json_is_the_structured_object(self, primary_conn: ConnectionConfig) -> None:
        text = self._call(primary_conn, {"table": "seedbank.collector", "format": "json"})
        payload = json.loads(text)

        assert payload["table"] == "seedbank.collector"
        assert "text" not in payload

    def test_yaml_is_yaml_text_of_the_same_object(self, primary_conn: ConnectionConfig) -> None:
        json_text = self._call(primary_conn, {"table": "seedbank.collector", "format": "json"})
        yaml_text = self._call(primary_conn, {"table": "seedbank.collector", "format": "yaml"})

        assert not yaml_text.startswith("{")
        assert yaml.safe_load(yaml_text) == json.loads(json_text)


class TestRealTransportErrorPaths:
    """Error paths only a real transport can exercise, over a real stdio subprocess."""

    def test_list_resources_maps_a_corrupt_manifest(self, stdio_server: StdioServer) -> None:
        manifest_path = (
            stdio_server.project_dir / "prints" / stdio_server.conn_name / "manifest.yaml"
        )
        manifest_path.write_text("not: valid: yaml: [")

        assert _stdio_list_resources_error_code(stdio_server) == -32603

    def test_budgeted_md_never_returns_an_empty_success(self, stdio_server: StdioServer) -> None:
        result = _stdio_call_tool(
            stdio_server,
            "get_table_context",
            {"table": "seedbank.collector", "format": "md", "budget_tokens": 1},
        )
        content = result.content[0]
        assert isinstance(content, TextContent)

        # Never empty and successful - either the truncation marker names what was
        # dropped, or the call failed outright.
        assert content.text != ""
        assert result.is_error or "truncated" in content.text

    def test_corrupt_statistics_is_reported_not_silently_dropped(
        self,
        stdio_server: StdioServer,
    ) -> None:
        stats_path = (
            stdio_server.project_dir
            / "prints"
            / stdio_server.conn_name
            / "seedbank"
            / "collector"
            / "statistics.yaml"
        )
        stats_path.write_text("not: valid: yaml: [")

        result = _stdio_call_tool(
            stdio_server,
            "get_table_context",
            {"table": "seedbank.collector", "format": "json"},
        )
        content = result.content[0]
        assert isinstance(content, TextContent)
        payload = json.loads(content.text)

        assert result.is_error is False
        assert "statistics" in payload.get("_corrupted", {})

    def test_corrupt_statistics_resource_read_fails_not_a_successful_read(
        self,
        stdio_server: StdioServer,
    ) -> None:
        stats_path = (
            stdio_server.project_dir
            / "prints"
            / stdio_server.conn_name
            / "seedbank"
            / "collector"
            / "statistics.yaml"
        )
        stats_path.write_text("not: valid: yaml: [")

        error = _stdio_read_resource_error(
            stdio_server,
            f"dbprint://{stdio_server.conn_name}/seedbank.collector/statistics",
        )

        assert error.error.code == -32603

    def test_absent_declared_statistics_is_distinguishable_from_never_declared(
        self,
        stdio_server: StdioServer,
    ) -> None:
        stats_path = (
            stdio_server.project_dir
            / "prints"
            / stdio_server.conn_name
            / "seedbank"
            / "collector"
            / "statistics.yaml"
        )
        stats_path.unlink()

        result = _stdio_call_tool(
            stdio_server,
            "get_table_context",
            {"table": "seedbank.collector", "format": "json"},
        )
        content = result.content[0]
        assert isinstance(content, TextContent)
        payload = json.loads(content.text)

        assert result.is_error is False
        assert payload.get("_missing") == ["statistics"]
        assert "statistics" not in payload.get("_corrupted", {})

        _add_plain_view(stdio_server, "public.a_view")
        view_result = _stdio_call_tool(
            stdio_server,
            "get_table_context",
            {"table": "public.a_view", "format": "json"},
        )
        view_content = view_result.content[0]
        assert isinstance(view_content, TextContent)
        view_payload = json.loads(view_content.text)

        # A kind the manifest never declared for this table is a different case - it
        # must not surface in `_missing`, which names only a broken promise.
        assert "_missing" not in view_payload

    def test_unserved_but_configured_connection_names_the_remedy(
        self,
        stdio_server: StdioServer,
    ) -> None:
        (stdio_server.project_dir / ".dbprint.yaml").write_text(
            f"connections:\n"
            f"  {stdio_server.conn_name}:\n"
            f"    adapter: postgres\n"
            f"    auto: true\n"
            f"    output: prints\n"
            f"  staging:\n"
            f"    adapter: postgres\n"
            f"    output: prints\n",
        )

        result = _stdio_call_tool(stdio_server, "list_tables", {"conn": "staging"})
        content = result.content[0]
        assert isinstance(content, TextContent)

        assert result.is_error is True
        assert "configured but not served" in content.text
        assert "not in .dbprint.yaml" not in content.text

    def test_absent_diff_does_not_blame_the_manifest(self, stdio_server: StdioServer) -> None:
        diff_path = stdio_server.project_dir / "prints" / stdio_server.conn_name / "diff.yaml"
        diff_path.unlink()

        error = _stdio_read_resource_error(stdio_server, f"dbprint://{stdio_server.conn_name}/diff")

        assert error.error.code == -32603
        assert "manifest" not in error.error.message.lower()
        assert "diff" in error.error.message.lower()

    def test_absent_reading_guide_does_not_blame_the_manifest(
        self,
        stdio_server: StdioServer,
    ) -> None:
        reading_path = stdio_server.project_dir / "prints" / stdio_server.conn_name / "reading.md"
        reading_path.unlink()

        error = _stdio_read_resource_error(
            stdio_server,
            f"dbprint://{stdio_server.conn_name}/reading",
        )

        assert error.error.code == -32603
        assert "manifest" not in error.error.message.lower()
        assert "reading" in error.error.message.lower()

    def test_unserved_but_configured_connection_resource_read_names_the_remedy(
        self,
        stdio_server: StdioServer,
    ) -> None:
        (stdio_server.project_dir / ".dbprint.yaml").write_text(
            f"connections:\n"
            f"  {stdio_server.conn_name}:\n"
            f"    adapter: postgres\n"
            f"    auto: true\n"
            f"    output: prints\n"
            f"  staging:\n"
            f"    adapter: postgres\n"
            f"    output: prints\n",
        )

        error = _stdio_read_resource_error(stdio_server, "dbprint://staging/manifest")

        assert "configured but not served" in error.error.message
        assert "not in .dbprint.yaml" not in error.error.message

    def test_undeclared_kind_names_the_kind_and_table_not_a_phantom_file(
        self,
        stdio_server: StdioServer,
    ) -> None:
        _add_plain_view(stdio_server, "public.a_view")

        error = _stdio_read_resource_error(
            stdio_server,
            f"dbprint://{stdio_server.conn_name}/public.a_view/statistics",
        )

        assert error.error.code == -32602
        assert "statistics" in error.error.message
        assert "public.a_view" in error.error.message
        assert "does not exist" not in error.error.message

    def test_bad_format_is_a_failed_call_not_a_silent_markdown_fallback(
        self,
        stdio_server: StdioServer,
    ) -> None:
        result = _stdio_call_tool(
            stdio_server,
            "get_table_context",
            {"table": "seedbank.collector", "format": "yml"},
        )

        assert result.is_error is True

    def test_bad_budget_tokens_is_a_failed_call_not_an_accepted_zero(
        self,
        stdio_server: StdioServer,
    ) -> None:
        result = _stdio_call_tool(
            stdio_server,
            "get_table_context",
            {"table": "seedbank.collector", "budget_tokens": 0},
        )

        assert result.is_error is True

    def test_zero_limit_is_a_failed_call_not_an_empty_success(
        self,
        stdio_server: StdioServer,
    ) -> None:
        result = _stdio_call_tool(stdio_server, "search_columns", {"limit": 0})
        content = result.content[0]
        assert isinstance(content, TextContent)

        assert result.is_error is True
        assert "limit" in content.text

    def test_non_integer_limit_is_a_failed_call_not_a_python_traceback(
        self,
        stdio_server: StdioServer,
    ) -> None:
        result = _stdio_call_tool(stdio_server, "search_columns", {"limit": "5"})
        content = result.content[0]
        assert isinstance(content, TextContent)

        assert result.is_error is True
        assert "limit" in content.text

    def test_corruption_past_the_result_cap_still_names_the_table(
        self,
        stdio_server: StdioServer,
    ) -> None:
        """A `limit` that caps `matches` on the first table must not blind the corruption scan.

        `seedbank.vault` sorts last, so `limit: 1` exhausts the cap before it - only a scan that
        walks every declared table reaches vault's corruption.
        """

        vault_stats = (
            stdio_server.project_dir
            / "prints"
            / stdio_server.conn_name
            / "seedbank"
            / "vault"
            / "statistics.yaml"
        )
        vault_stats.write_text("not: valid: yaml: [")

        result = _stdio_call_tool(stdio_server, "search_columns", {"limit": 1})
        content = result.content[0]
        assert isinstance(content, TextContent)
        payload = json.loads(content.text)

        assert result.is_error is False
        assert payload.get("truncated") is True
        assert "seedbank.vault" in payload.get("unreadable_tables", [])
