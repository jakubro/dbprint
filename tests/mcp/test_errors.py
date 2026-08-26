"""Error constructors per MCP.md 8."""

from __future__ import annotations

from dbprint.mcp import errors


class TestErrorConstructors:
    def test_unknown_table_includes_hint(self) -> None:
        err = errors.unknown_table("foo", "primary")
        assert err.code == -32602
        assert "foo" in err.detail
        assert "primary" in err.detail
        assert "dbprint list primary" in err.detail

    def test_unknown_connection_lists_configured(self) -> None:
        err = errors.unknown_connection("zzz", ["a", "b"])
        assert err.code == -32602
        assert "a" in err.detail and "b" in err.detail

    def test_malformed_pattern(self) -> None:
        err = errors.malformed_pattern("x[")
        assert err.code == -32602
        assert "x[" in err.detail

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

    def test_malformed_uri(self) -> None:
        err = errors.malformed_uri("not://a/dbprint/uri")
        assert err.code == -32602

    def test_missing_table_argument(self) -> None:
        err = errors.missing_table_argument("")
        assert err.code == -32602
        assert "non-empty string" in err.detail

    def test_no_diff_available(self) -> None:
        err = errors.no_diff_available("/tmp/x/diff.yaml")
        assert err.code == -32603
        assert "dbprint diff" in err.detail
        assert "manifest references" not in err.detail
