"""Error constructors per MCP.md 8."""

from __future__ import annotations

from dbprint.mcp import errors


class TestErrorConstructors:
    def test_unknown_table_includes_hint(self) -> None:
        err = errors.unknown_table("foo", "primary")
        assert err.code == -32602
        assert "foo" in err.detail
        assert "primary" in err.detail
        assert "Call list_tables" in err.detail

    def test_unknown_connection_lists_configured(self) -> None:
        err = errors.unknown_connection("zzz", ["a", "b"])
        assert err.code == -32602
        assert "a" in err.detail and "b" in err.detail

    def test_no_default_connection_names_the_argument_to_pass(self) -> None:
        err = errors.no_default_connection(["a", "b"], served=False)
        assert err.code == -32602
        assert "pass connection explicitly" in err.detail

    def test_no_default_connection_labels_a_served_set_as_served(self) -> None:
        err = errors.no_default_connection(["staging", "production"], served=True)

        assert err.detail.endswith("Served: ['production', 'staging']")

    def test_no_default_connection_labels_a_configured_set_as_configured(self) -> None:
        err = errors.no_default_connection(["b", "a"], served=False)

        assert err.detail.endswith("Configured: ['a', 'b']")

    def test_missing_optional_artifact(self) -> None:
        err = errors.missing_optional_artifact("description.md", "public.curator")
        assert err.code == -32602
        assert "optional" in err.detail

    def test_manifest_references_missing_file(self) -> None:
        err = errors.manifest_references_missing_file("statistics.yaml", "/tmp/x")
        assert err.code == -32603
        assert "dbprint generate" in err.detail

    def test_unknown_tool_lists_available(self) -> None:
        err = errors.unknown_tool("nope", ["a", "b"])
        assert err.code == -32601
        assert "a" in err.detail and "b" in err.detail

    def test_no_diff_available(self) -> None:
        err = errors.no_diff_available("/tmp/x/diff.yaml")
        assert err.code == -32603
        assert "dbprint diff" in err.detail
        assert "manifest references" not in err.detail
