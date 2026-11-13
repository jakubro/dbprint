"""Error constructors per MCP.md 8."""

from __future__ import annotations

import pytest

from dbprint.mcp import errors
from tests._regenerate import assert_leaves_regenerating_to_the_maintainer
from tests._scripts import REPO_ROOT


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
        assert "dbprint diff" not in err.detail
        assert "manifest references" not in err.detail


class TestNoErrorTellsItsReaderToRunACommandThatQueriesTheDatabase:
    @pytest.mark.parametrize(
        ("error", "code", "subjects"),
        [
            (
                errors.unprofiled_table("arboretum.seedbank.extra"),
                -32602,
                ["arboretum.seedbank.extra"],
            ),
            (
                errors.manifest_references_missing_file("statistics.yaml", "/p/statistics.yaml"),
                -32603,
                ["statistics.yaml", "/p/statistics.yaml"],
            ),
            (errors.no_diff_available("/p/diff.yaml"), -32603, ["/p/diff.yaml"]),
            (errors.no_reading_guide_available("/p/reading.md"), -32603, ["/p/reading.md"]),
        ],
        ids=["unprofiled", "missing-file", "no-diff", "no-reading-guide"],
    )
    def test_it_names_what_is_missing_and_leaves_regenerating_to_the_maintainer(
        self,
        error: errors.McpError,
        code: int,
        subjects: list[str],
    ) -> None:
        assert error.code == code
        assert all(subject in error.detail for subject in subjects)
        assert_leaves_regenerating_to_the_maintainer(error.detail)

    @pytest.mark.parametrize(
        "error",
        [
            errors.no_diff_available("/p/diff.yaml"),
            errors.no_reading_guide_available("/p/reading.md"),
        ],
        ids=["no-diff", "no-reading-guide"],
    )
    def test_a_root_file_text_offers_no_diff_command_and_blames_no_manifest(
        self,
        error: errors.McpError,
    ) -> None:
        assert "dbprint diff" not in error.detail
        assert "manifest" not in error.detail

    @pytest.mark.parametrize(
        "error",
        [
            errors.unprofiled_table("arboretum.seedbank.extra"),
            errors.manifest_references_missing_file("statistics.yaml", "<path>"),
            errors.no_diff_available("<path>"),
            errors.no_reading_guide_available("<path>"),
        ],
        ids=["unprofiled", "missing-file", "no-diff", "no-reading-guide"],
    )
    def test_the_error_catalogue_quotes_it_verbatim(self, error: errors.McpError) -> None:
        assert f'"{error.detail}"' in (REPO_ROOT / "docs/MCP.md").read_text(encoding="utf-8")
