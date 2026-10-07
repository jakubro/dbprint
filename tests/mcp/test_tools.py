"""Tool dispatch + per-tool behavior per MCP.md 4."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml

from dbprint.config import ConnectionConfig
from dbprint.config.project import RuleConfig
from dbprint.engine import AssemblyOptions, Purpose, assemble_context, assemble_structured_context
from dbprint.mcp import McpError, ServedConnections, dispatch, paging
from dbprint.mcp.tools import (
    TOOL_DEFINITIONS,
    TOOL_NAMES,
)
from tests import _mcp_pages


def _state_for(conn: ConnectionConfig) -> ServedConnections:
    return ServedConnections(served={conn.name: conn}, default=conn.name)


def _narrow_the_seeded_read(conn: ConnectionConfig, rows_scanned: int) -> None:
    """Rewrite the seeded statistics as a partial read of the same table (SPEC 2.2.8)."""

    path = conn.output / conn.name / "public" / "curator" / "statistics.yaml"
    statistics = yaml.safe_load(path.read_text())
    statistics["scope"] = {"rows_scanned": rows_scanned, "sample": 0.4}

    for column in statistics["columns"].values():
        column["rows_scanned"] = rows_scanned

    path.write_text(yaml.safe_dump(statistics))


def _dict_result(state: ServedConnections, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dict-returning `dispatch`; md/yaml `get_table_context` is the only string (MCP.md 4.1)."""

    result = dispatch(state, name, arguments)
    assert isinstance(result, dict)

    return result


class TestEveryListReplyIsPaged:
    """A reply past a client's ceiling is not a large answer but no answer, plus a turn."""

    @staticmethod
    def _wide_manifest(conn: ConnectionConfig, tables: int) -> None:
        """Grow the manifest past a page, reusing one seeded table's entry."""

        path = conn.output / conn.name / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        entry = next(iter(manifest["tables"].values()))
        manifest["tables"] = {f"seedbank.t{i:04d}": dict(entry) for i in range(tables)}
        path.write_text(yaml.safe_dump(manifest))

    @staticmethod
    def _walk(state: ServedConnections, name: str, arguments: dict[str, Any]) -> list[dict]:
        pages = [_dict_result(state, name, arguments)]

        while "next_cursor" in pages[-1]:
            pages.append(
                _dict_result(state, name, {**arguments, "cursor": pages[-1]["next_cursor"]}),
            )

        return pages

    def test_a_wide_listing_walks_to_every_table_once_in_pages_under_the_bound(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 600)
        pages = self._walk(_state_for(primary_conn), "list_tables", {"detail": True})
        names = [entry["table"] for page in pages for entry in page["tables"]]

        assert len(pages) > 1
        assert all(len(json.dumps(page, indent=2, default=str)) <= 20_000 for page in pages)
        assert names == [f"seedbank.t{i:04d}" for i in range(600)]
        assert {page["total"] for page in pages} == {600}

    def test_a_listing_that_fits_is_one_page_with_its_total(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 600)
        result = _dict_result(
            _state_for(primary_conn),
            "list_tables",
            {"pattern": "seedbank.t000*"},
        )

        assert result["tables"] == [f"seedbank.t000{i}" for i in range(10)]
        assert result["total"] == 10
        assert "next_cursor" not in result
        assert "truncated" not in result

    def test_a_manifest_pages_its_table_map_and_keeps_the_rest_on_the_first_page(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 600)
        pages = self._walk(_state_for(primary_conn), "get_manifest", {})
        names = [fqn for page in pages for fqn in page["tables"]]

        assert len(pages) > 1
        assert names == [f"seedbank.t{i:04d}" for i in range(600)]
        assert pages[0]["format_version"] == 1
        assert all("generated_at" not in page for page in pages[1:])
        assert all(len(json.dumps(page, indent=2, default=str)) <= 20_000 for page in pages)

    def test_a_manifest_written_out_of_order_still_pages_in_fqn_order(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        entry = next(iter(manifest["tables"].values()))
        manifest["tables"] = {f"seedbank.t{i:04d}": dict(entry) for i in reversed(range(600))}
        path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        pages = self._walk(_state_for(primary_conn), "get_manifest", {})

        assert [fqn for page in pages for fqn in page["tables"]] == [
            f"seedbank.t{i:04d}" for i in range(600)
        ]

    def test_a_header_larger_than_a_page_arrives_in_parts_and_every_page_fits(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        failed = [f"seedbank.withdrawn_{i:05d}" for i in range(2_000)]
        manifest["failed_tables"] = failed
        path.write_text(yaml.safe_dump(manifest))
        pages = self._walk(_state_for(primary_conn), "get_manifest", {})

        assert len(pages) > 1
        assert all(len(json.dumps(page, indent=2, default=str)) <= 20_000 for page in pages)
        assert _mcp_pages.merged(pages)["failed_tables"] == failed

    def test_a_column_search_walks_every_match_and_names_unreadable_tables_up_front(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 600)
        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        broken = dict(manifest["tables"]["seedbank.t0599"], path="broken")
        manifest["tables"]["seedbank.t0599"] = broken
        manifest_path.write_text(yaml.safe_dump(manifest))
        broken_dir = primary_conn.output / primary_conn.name / "broken"
        broken_dir.mkdir()
        (broken_dir / "statistics.yaml").write_text("not: valid: yaml: [")

        pages = self._walk(_state_for(primary_conn), "search_columns", {})
        matches = [(m["table"], m["column"], m.get("part")) for p in pages for m in p["matches"]]

        assert len(pages) > 1
        assert len(matches) == len(set(matches)) == pages[0]["total"]
        assert {table for table, _, _ in matches} == {f"seedbank.t{i:04d}" for i in range(599)}
        assert pages[0]["unreadable_tables"] == ["seedbank.t0599"]
        assert all("unreadable_tables" not in page for page in pages[1:])

    def test_a_diff_pages_its_changes_in_file_order(self, primary_conn: ConnectionConfig) -> None:
        path = primary_conn.output / primary_conn.name / "diff.yaml"
        diff = yaml.safe_load(path.read_text())
        diff["changes"] = [
            {"kind": "table_added", "table": f"seedbank.t{i:04d}"} for i in reversed(range(900))
        ]
        path.write_text(yaml.safe_dump(diff))

        pages = self._walk(_state_for(primary_conn), "get_diff", {})

        assert len(pages) > 1
        assert [c["table"] for p in pages for c in p["changes"]] == [
            f"seedbank.t{i:04d}" for i in reversed(range(900))
        ]
        assert "summary" in pages[0]
        assert all("summary" not in page for page in pages[1:])

    def test_an_item_larger_than_a_page_arrives_in_parts_that_rebuild_it(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        fqn = next(iter(manifest["tables"]))
        manifest["tables"][fqn]["statistics_params"] = {"note": 'xé"' * 15_000}
        path.write_text(yaml.safe_dump(manifest))

        pages = self._walk(_state_for(primary_conn), "get_manifest", {"pattern": fqn})
        parts = [page["tables"][fqn] for page in pages if fqn in page["tables"]]

        assert [part["part"] for part in parts] == list(range(1, len(parts) + 1))
        assert len(parts) > 1
        assert {part["parts"] for part in parts} == {len(parts)}
        assert all(len(json.dumps(page, indent=2, default=str)) <= 20_000 for page in pages)
        assert json.loads("".join(part["text"] for part in parts)) == manifest["tables"][fqn]

    def test_a_cursor_reused_under_other_filters_is_refused(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 600)
        state = _state_for(primary_conn)
        cursor = _dict_result(state, "list_tables", {"detail": True})["next_cursor"]

        with pytest.raises(McpError) as caught:
            dispatch(state, "list_tables", {"detail": True, "pattern": "x*", "cursor": cursor})

        assert caught.value.code == -32602
        assert "without `cursor`" in caught.value.detail

    def test_a_cursor_from_another_tool_is_refused(self, primary_conn: ConnectionConfig) -> None:
        self._wide_manifest(primary_conn, 600)
        state = _state_for(primary_conn)
        cursor = _dict_result(state, "get_manifest", {})["next_cursor"]

        with pytest.raises(McpError) as caught:
            dispatch(state, "list_tables", {"cursor": cursor})

        assert caught.value.code == -32602

    def test_a_cursor_over_a_rewritten_print_is_refused_naming_the_change(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 600)
        state = _state_for(primary_conn)
        cursor = _dict_result(state, "list_tables", {"detail": True})["next_cursor"]
        self._wide_manifest(primary_conn, 601)

        with pytest.raises(McpError) as caught:
            dispatch(state, "list_tables", {"detail": True, "cursor": cursor})

        assert caught.value.code == -32602
        assert "changed" in caught.value.detail

    @pytest.mark.parametrize("cursor", ["x", "e30", "bm90IGpzb24"])
    def test_a_cursor_no_reply_issued_is_refused(
        self,
        primary_conn: ConnectionConfig,
        cursor: str,
    ) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(_state_for(primary_conn), "list_tables", {"cursor": cursor})

        assert caught.value.code == -32602

    def test_the_removed_limit_is_an_unknown_argument(self, primary_conn: ConnectionConfig) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(_state_for(primary_conn), "search_columns", {"limit": 5})

        assert "takes no argument 'limit'" in caught.value.detail

    def test_a_diff_filters_by_table_including_the_relationship_events(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """The three relationship events carry `source_table`/`target_table`, never `table`."""

        path = primary_conn.output / primary_conn.name / "diff.yaml"
        diff = yaml.safe_load(path.read_text())
        diff["changes"] = [
            {"kind": "table_added", "table": "arboretum.seedbank.taxon"},
            {"kind": "table_added", "table": "seedbank.other"},
            {
                "kind": "relationship_added",
                "source_table": "arboretum.seedbank.taxon",
                "target_table": "seedbank.other",
            },
        ]
        path.write_text(yaml.safe_dump(diff))

        result = _dict_result(
            _state_for(primary_conn),
            "get_diff",
            {"table": "arboretum.seedbank.taxon"},
        )

        assert [c["kind"] for c in result["changes"]] == ["table_added", "relationship_added"]

    def test_a_diff_filters_by_kind(self, primary_conn: ConnectionConfig) -> None:
        path = primary_conn.output / primary_conn.name / "diff.yaml"
        diff = yaml.safe_load(path.read_text())
        diff["changes"] = [
            {"kind": "table_added", "table": "arboretum.seedbank.taxon"},
            {"kind": "table_removed", "table": "seedbank.other"},
        ]
        path.write_text(yaml.safe_dump(diff))

        result = _dict_result(
            _state_for(primary_conn),
            "get_diff",
            {"kind": "table_removed"},
        )

        assert [c["table"] for c in result["changes"]] == ["seedbank.other"]

    def test_a_misspelled_kind_is_refused_by_the_packaged_enum(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(_state_for(primary_conn), "get_diff", {"kind": "colum_added"})

        assert caught.value.code == -32602
        assert "column_added" in caught.value.detail

    @staticmethod
    def _wide_ddl(conn: ConnectionConfig) -> None:
        ddl = conn.output / conn.name / "arboretum" / "fixture" / "shape_probe" / "ddl.sql"
        ddl.write_text(ddl.read_text() + "".join(f"-- field note {i}\n" for i in range(2_000)))

    @pytest.mark.parametrize("purpose", ["profile", "query"])
    def test_a_wide_context_pages_and_omits_no_section(
        self,
        primary_conn: ConnectionConfig,
        purpose: Purpose,
    ) -> None:
        self._wide_ddl(primary_conn)
        arguments = {"table": "arboretum.fixture.shape_probe", "purpose": purpose}
        text_pages = _mcp_pages.pages(_state_for(primary_conn), "get_table_context", arguments)
        unbudgeted = assemble_context(
            manifest=yaml.safe_load(
                (primary_conn.output / primary_conn.name / "manifest.yaml").read_text(),
            ),
            print_root=primary_conn.output / primary_conn.name,
            tables=["arboretum.fixture.shape_probe"],
            options=AssemblyOptions(purpose=purpose),
        ).text
        document = _mcp_pages.joined(text_pages)
        whole = _mcp_pages.without_legend(unbudgeted)

        assert len(text_pages) > 1
        assert all(len(page) <= 20_000 for page in text_pages)
        assert "truncated" not in document
        assert sorted(document.split("\n\n")) == sorted(whole.split("\n\n"))

    def test_each_page_defines_only_the_terms_its_lines_print(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_ddl(primary_conn)
        arguments = {"table": "arboretum.fixture.shape_probe"}
        text_pages = _mcp_pages.pages(_state_for(primary_conn), "get_table_context", arguments)

        for number, page in enumerate(text_pages):
            body = _mcp_pages.without_legend(page, first=number == 0)
            found = re.search(r"## Terms\n\n((?:- [^\n]*\n?)*)", page)
            labels = [
                line[2:].split(": ", 1)[0] for line in (found[1] if found else "").splitlines()
            ]

            assert body != page or not found, number
            figures = re.sub(
                r"values \(top \d+, covering [^)]+\)",
                "values (top N, covering X)",
                body,
            )

            assert [label for label in labels if label not in figures] == [], number

        assert "## Terms" in text_pages[0]

    def test_a_profile_context_concatenates_to_the_unbudgeted_rendering(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_ddl(primary_conn)
        arguments = {"table": "arboretum.fixture.shape_probe"}
        text_pages = _mcp_pages.pages(_state_for(primary_conn), "get_table_context", arguments)
        unbudgeted = assemble_context(
            manifest=yaml.safe_load(
                (primary_conn.output / primary_conn.name / "manifest.yaml").read_text(),
            ),
            print_root=primary_conn.output / primary_conn.name,
            tables=["arboretum.fixture.shape_probe"],
            options=AssemblyOptions(),
        ).text

        assert _mcp_pages.joined(text_pages) == _mcp_pages.without_legend(unbudgeted)

    @pytest.mark.parametrize("fmt", ["json", "yaml"])
    def test_a_structured_context_pages_merge_to_the_unbudgeted_object(
        self,
        primary_conn: ConnectionConfig,
        fmt: str,
    ) -> None:
        root = primary_conn.output / primary_conn.name
        object_pages = _mcp_pages.pages(
            _state_for(primary_conn),
            "get_table_context",
            {"table": "arboretum.seedbank.accession", "format": fmt},
        )

        assert len(object_pages) > 1
        assert all(len(paging.serialized(page)) <= 20_000 for page in object_pages)
        assert _mcp_pages.merged(object_pages) == assemble_structured_context(
            manifest=yaml.safe_load((root / "manifest.yaml").read_text()),
            print_root=root,
            table="arboretum.seedbank.accession",
            options=AssemblyOptions(format=fmt),
        )

    @pytest.mark.parametrize("fmt", ["json", "yaml"])
    def test_a_value_longer_than_a_page_arrives_in_parts_byte_identical(
        self,
        primary_conn: ConnectionConfig,
        fmt: str,
    ) -> None:
        table_dir = primary_conn.output / primary_conn.name / "arboretum" / "seedbank" / "taxon"
        statistics = yaml.safe_load((table_dir / "statistics.yaml").read_text())
        column = next(name for name, col in statistics["columns"].items() if col.get("values"))
        long_value = 'pétale "' * 4_000
        statistics["columns"][column]["values"][0]["value"] = long_value
        (table_dir / "statistics.yaml").write_text(yaml.safe_dump(statistics))

        object_pages = _mcp_pages.pages(
            _state_for(primary_conn),
            "get_table_context",
            {"table": "arboretum.seedbank.taxon", "format": fmt},
        )
        rebuilt = _mcp_pages.merged(object_pages)["statistics"]["columns"][column]

        assert all(len(paging.serialized(page)) <= 20_000 for page in object_pages)
        assert rebuilt["values"][0]["value"] == long_value

    def test_a_context_cursor_over_a_rewritten_statistics_file_is_refused(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        state = _state_for(primary_conn)
        arguments = {"table": "arboretum.fixture.shape_probe", "format": "json"}
        cursor = _dict_result(state, "get_table_context", arguments)["next_cursor"]
        stats = (
            primary_conn.output
            / primary_conn.name
            / "arboretum"
            / "fixture"
            / "shape_probe"
            / "statistics.yaml"
        )
        stats.write_text(stats.read_text() + "\n")

        with pytest.raises(McpError) as caught:
            dispatch(state, "get_table_context", {**arguments, "cursor": cursor})

        assert "changed" in caught.value.detail


class TestEveryCallIsCheckedAgainstTheToolsOwnSchema:
    """MCP.md 8.2: the SDK runs no `inputSchema` check, so `dispatch` is where one has to be."""

    def test_an_unknown_key_is_refused_by_name(self, primary_conn: ConnectionConfig) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(_state_for(primary_conn), "search_columns", {"query": "curator"})

        assert caught.value.code == -32602
        assert "query" in caught.value.detail
        assert "pattern" in caught.value.detail

    def test_the_abbreviated_connection_argument_is_refused_naming_the_full_word(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(_state_for(primary_conn), "list_tables", {"conn": "primary"})

        assert "'conn'" in caught.value.detail
        assert "'connection'" in caught.value.detail

    def test_a_misspelled_required_key_names_what_was_sent(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(
                _state_for(primary_conn),
                "get_table_context",
                {"table_name": "arboretum.seedbank.taxon"},
            )

        assert "table_name" in caught.value.detail

    def test_a_missing_required_key_names_the_key_not_its_value(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(_state_for(primary_conn), "get_table_context", {})

        assert "requires 'table'" in caught.value.detail
        assert "None" not in caught.value.detail

    def test_a_wrong_type_names_the_declared_type(self, primary_conn: ConnectionConfig) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(_state_for(primary_conn), "list_tables", {"pattern": 7})

        assert "string" in caught.value.detail

    def test_a_string_boolean_never_silently_enables_a_section(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """`bool("false")` is True, so a string would turn the flag on."""

        with pytest.raises(McpError) as caught:
            dispatch(
                _state_for(primary_conn),
                "get_table_context",
                {"table": "arboretum.seedbank.taxon", "include_stats": "false"},
            )

        assert caught.value.code == -32602

    def test_a_string_boolean_on_a_filter_never_silently_empties_a_result(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """`"false"` equals neither True nor False, so a strict comparison matches nothing."""

        with pytest.raises(McpError) as caught:
            dispatch(_state_for(primary_conn), "search_columns", {"candidate_key": "false"})

        assert caught.value.code == -32602

    def test_the_removed_budget_is_an_unknown_argument(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(
                _state_for(primary_conn),
                "get_table_context",
                {"table": "arboretum.seedbank.taxon", "budget_tokens": 4000},
            )

        assert "takes no argument 'budget_tokens'" in caught.value.detail

    def test_a_value_outside_an_enum_names_the_accepted_set(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(
                _state_for(primary_conn),
                "get_table_context",
                {"table": "arboretum.seedbank.taxon", "format": "yml"},
            )

        assert "'md'" in caught.value.detail

    def test_an_enum_in_the_wrong_case_is_still_accepted(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """`format` and `purpose` fold; the schema check must not take that away."""

        result = dispatch(
            _state_for(primary_conn),
            "get_table_context",
            {"table": "arboretum.seedbank.taxon", "format": "MD", "purpose": "QUERY"},
        )

        assert isinstance(result, str)

    def test_a_non_string_where_an_enum_is_declared_names_the_type(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """`str(7).lower()` would turn a type error into an enum error and name the wrong fault."""

        with pytest.raises(McpError) as caught:
            dispatch(
                _state_for(primary_conn),
                "get_table_context",
                {"table": "arboretum.seedbank.taxon", "format": 7},
            )

        assert "'md'" not in caught.value.detail

    def test_a_boolean_is_not_an_integer(self, primary_conn: ConnectionConfig) -> None:
        """jsonschema's own type checker refuses it, so no extra guard is needed."""

        with pytest.raises(McpError) as caught:
            dispatch(
                _state_for(primary_conn),
                "get_table_context",
                {"table": "arboretum.seedbank.taxon", "budget_tokens": True},
            )

        assert caught.value.code == -32602

    def test_every_tool_still_answers_a_valid_call(self, primary_conn: ConnectionConfig) -> None:
        """The enforcement is a gate, not a narrowing: each tool's own valid call still works."""

        state = _state_for(primary_conn)
        calls = {
            "list_tables": {},
            "search_columns": {"pattern": "*"},
            "get_manifest": {},
            "get_diff": {},
            "get_reference": {"document": "spec", "section": "2.2.3"},
            "get_table_context": {"table": "arboretum.seedbank.taxon"},
            "resolve_value": {
                "table": "arboretum.seedbank.taxon",
                "column": "rank",
                "text": "genus",
            },
        }

        for name, arguments in calls.items():
            assert dispatch(state, name, arguments) is not None, name


class TestToolDispatch:
    def test_known_tool_succeeds(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "list_tables", {})
        assert "tables" in result

    def test_unknown_tool_raises(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)

        with pytest.raises(McpError) as exc_info:
            dispatch(state, "no_such_tool", {})

        assert exc_info.value.code == -32601


class TestResolveValue:
    """MCP.md 4.7: one column's answer to a phrase, read off the print alone."""

    def test_a_stored_value_comes_back_in_the_columns_own_spelling(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        state = _state_for(primary_conn)
        result = dispatch(
            state,
            "resolve_value",
            {"table": "arboretum.seedbank.taxon", "column": "rank", "text": "GENUS"},
        )

        assert isinstance(result, dict)
        assert result["match"] == "stored"
        assert result["spellings"][0]["value"] == "genus"

    def test_a_wide_domain_pages_after_the_answer_and_shortens_no_value(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        path = primary_conn.output / primary_conn.name / "arboretum/seedbank/taxon/statistics.yaml"
        statistics = yaml.safe_load(path.read_text())
        wide = [{"value": f"genus {i:02d} " + "x" * 2_000, "count": 1} for i in range(40)]
        statistics["columns"]["rank"]["values"] += wide
        path.write_text(yaml.safe_dump(statistics))
        arguments = {"table": "arboretum.seedbank.taxon", "column": "rank", "text": "GENUS"}

        result_pages = _mcp_pages.pages(_state_for(primary_conn), "resolve_value", arguments)
        keys = [key for page in result_pages for key in page if key in {"spellings", "domain"}]
        domain = [entry["value"] for page in result_pages for entry in page.get("domain", [])]

        assert len(result_pages) > 1
        assert all(len(json.dumps(page, indent=2)) <= 20_000 for page in result_pages)
        assert all(page["match"] == "stored" and page["listed"] == 43 for page in result_pages)
        assert keys.index("spellings") < keys.index("domain")
        assert domain == ["species", "genus", "family"] + [entry["value"] for entry in wide]

    def test_an_unknown_column_names_the_columns_the_table_has(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        state = _state_for(primary_conn)

        with pytest.raises(McpError) as excinfo:
            dispatch(
                state,
                "resolve_value",
                {"table": "arboretum.seedbank.taxon", "column": "rnk", "text": "genus"},
            )

        assert "rank" in excinfo.value.detail

    def test_an_unknown_table_is_refused(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)

        with pytest.raises(McpError):
            dispatch(
                state,
                "resolve_value",
                {"table": "seedbank.nowhere", "column": "rank", "text": "genus"},
            )

    def test_a_missing_argument_is_refused(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)

        with pytest.raises(McpError) as excinfo:
            dispatch(
                state,
                "resolve_value",
                {"table": "arboretum.seedbank.taxon", "column": "rank"},
            )

        assert "text" in excinfo.value.detail

    def test_an_empty_exhaustive_list_answers_none_over_an_empty_domain(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """SPEC 7.4: `values: []` at coverage 1.0 says the column holds no value - an answer."""

        self._edit_rank(primary_conn, values=[], values_coverage=1.0)

        result = _dict_result(
            _state_for(primary_conn),
            "resolve_value",
            {"table": "arboretum.seedbank.taxon", "column": "rank", "text": "genus"},
        )

        assert (result["match"], result["exhaustive"], result["domain"]) == ("none", True, [])

    def test_a_lost_value_list_is_unavailable_and_says_unmeasured(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._edit_rank(primary_conn, unmeasured=["values", "values_coverage"])

        result = _dict_result(
            _state_for(primary_conn),
            "resolve_value",
            {"table": "arboretum.seedbank.taxon", "column": "rank", "text": "genus"},
        )

        assert result["match"] == "unavailable"
        assert "unmeasured" in result["reason"]

    @staticmethod
    def _edit_rank(conn: ConnectionConfig, **fields: Any) -> None:
        path = conn.output / conn.name / "arboretum/seedbank/taxon/statistics.yaml"
        statistics = yaml.safe_load(path.read_text())
        column = statistics["columns"]["rank"]

        for dropped in ("values", "values_coverage", "values_coverage_method"):
            column.pop(dropped, None)

        column.update(fields)
        path.write_text(yaml.safe_dump(statistics))


class TestGetTableContext:
    def test_md_format(self, primary_conn: ConnectionConfig) -> None:
        """MCP.md 4.1: md returns a bare markdown string, not an envelope."""

        state = _state_for(primary_conn)
        result = dispatch(
            state,
            "get_table_context",
            {"table": "arboretum.seedbank.collector", "format": "md"},
        )

        assert isinstance(result, str)
        assert "arboretum.seedbank.collector" in result
        assert "CREATE TABLE" in result

    def test_purpose_query_returns_the_value_table_and_no_statistics(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """MCP.md 4.1: `query` is the selection a caller reads before writing SQL."""

        state = _state_for(primary_conn)
        result = dispatch(
            state,
            "get_table_context",
            {"table": "arboretum.seedbank.collector", "purpose": "query"},
        )

        assert isinstance(result, str)
        assert "## Column values" in result
        assert "Cardinality" not in result

    def test_purpose_defaults_to_profile(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = dispatch(state, "get_table_context", {"table": "arboretum.seedbank.collector"})

        assert isinstance(result, str)
        assert "Cardinality" in result
        assert "## Column values" not in result

    def test_an_unknown_purpose_is_refused(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)

        with pytest.raises(McpError) as excinfo:
            dispatch(
                state,
                "get_table_context",
                {"table": "arboretum.seedbank.collector", "purpose": "explain"},
            )

        assert "purpose" in excinfo.value.detail

    def test_md_carries_the_scanned_set_of_a_narrowed_read(
        self,
        scoped_conn: ConnectionConfig,
    ) -> None:
        """A qualifier a consumer needs to read the counts must not be CLI-only."""

        _narrow_the_seeded_read(scoped_conn, rows_scanned=2)
        state = _state_for(scoped_conn)
        result = dispatch(state, "get_table_context", {"table": "public.curator", "format": "md"})

        assert isinstance(result, str)
        assert "Scanned: 2 of 5 rows (40%)" in result

    def test_md_names_its_own_sql_dialect(self, primary_conn: ConnectionConfig) -> None:
        """A single-table MCP fragment never reaches a document-level provenance block."""

        state = _state_for(primary_conn)
        result = dispatch(
            state,
            "get_table_context",
            {"table": "arboretum.seedbank.collector", "format": "md"},
        )

        assert isinstance(result, str)
        assert "Adapter: postgres" in result

    def test_unknown_table_raises(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)

        with pytest.raises(McpError) as exc_info:
            dispatch(state, "get_table_context", {"table": "public.missing"})

        assert exc_info.value.code == -32602
        assert "missing" in exc_info.value.detail

    def test_json_format_returns_the_structured_object(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """MCP.md 4.1: json returns table/ddl/description/stats/relationships, not a string."""

        state = _state_for(primary_conn)
        result = _mcp_pages.merged(
            _mcp_pages.pages(
                state,
                "get_table_context",
                {"table": "arboretum.fixture.shape_probe", "format": "json"},
            ),
        )

        assert result["table"] == "arboretum.fixture.shape_probe"
        assert "CREATE TABLE" in result["ddl"]
        assert result["statistics"]["table"] == "arboretum.fixture.shape_probe"
        # A list, not asserted empty: `probe_id`'s value set nests inside other tables' keys, so
        # this table carries measured edges; the shape under test is the envelope, not the count.
        assert isinstance(result["relationships"]["refers_to"], list)
        assert "text" not in result
        assert "description" not in result  # not declared for this table
        assert "annotations" not in result

    def test_yaml_format_returns_the_same_object_emitted_as_yaml(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """MCP.md 4.1: yaml is the same structured object as json, serialized as YAML text."""

        state = _state_for(primary_conn)
        arguments = {"table": "arboretum.seedbank.collector"}
        json_pages = _mcp_pages.pages(state, "get_table_context", {**arguments, "format": "json"})
        yaml_pages = _mcp_pages.pages(state, "get_table_context", {**arguments, "format": "yaml"})

        assert all(isinstance(page, str) for page in yaml_pages)
        assert _mcp_pages.merged(yaml_pages) == _mcp_pages.merged(json_pages)

    def test_json_format_respects_include_flags(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(
            state,
            "get_table_context",
            {
                "table": "arboretum.seedbank.collector",
                "format": "json",
                "include_stats": False,
                "include_relationships": False,
            },
        )

        assert "statistics" not in result
        assert "relationships" not in result
        assert "ddl" in result

    def test_md_format_is_unaffected_by_the_structured_path(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        state = _state_for(primary_conn)
        result = dispatch(
            state,
            "get_table_context",
            {"table": "arboretum.seedbank.collector", "format": "md"},
        )

        assert isinstance(result, str)
        assert "CREATE TABLE" in result

    @staticmethod
    def _author_annotations(conn: ConnectionConfig) -> None:
        """arboretum.seedbank.collector ships with no statistics.annotations.yaml for real."""

        import yaml

        table_dir = conn.output / conn.name / "arboretum" / "seedbank" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {"format_version": 1, "columns": {"email": {"note": "always lowercase"}}},
            ),
        )
        manifest_path = conn.output / conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["arboretum.seedbank.collector"]["artifacts"][
            "statistics_annotations"
        ] = "statistics.annotations.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest))

    def test_json_format_includes_annotations_when_authored(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._author_annotations(primary_conn)
        state = _state_for(primary_conn)
        result = _dict_result(
            state,
            "get_table_context",
            {"table": "arboretum.seedbank.collector", "format": "json"},
        )

        assert result["annotations"] == {"email": {"note": "always lowercase"}}

    def test_include_annotations_false_omits_the_key(self, primary_conn: ConnectionConfig) -> None:
        self._author_annotations(primary_conn)
        state = _state_for(primary_conn)
        result = _dict_result(
            state,
            "get_table_context",
            {
                "table": "arboretum.seedbank.collector",
                "format": "json",
                "include_annotations": False,
            },
        )

        assert "annotations" not in result


class TestCorruptedArtifactPassesThroughUnchanged:
    """`_corrupted` is the assembler's own key (MCP.md 4.1) - the tool must not recompute or
    overwrite it - a second, independent computation is what makes two formats disagree.
    """

    @staticmethod
    def _corrupt_statistics(conn: ConnectionConfig) -> None:
        path = conn.output / conn.name / "arboretum" / "seedbank" / "collector" / "statistics.yaml"
        path.write_text("columns: [unterminated")

    def test_json_format_carries_the_assemblers_corrupted_mapping(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._corrupt_statistics(primary_conn)
        state = _state_for(primary_conn)
        result = _dict_result(
            state,
            "get_table_context",
            {"table": "arboretum.seedbank.collector", "format": "json"},
        )

        assert isinstance(result["_corrupted"], dict)
        assert set(result["_corrupted"]) == {"statistics"}
        assert isinstance(result["_corrupted"]["statistics"], str)

    def test_yaml_format_carries_the_same_mapping_as_json(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._corrupt_statistics(primary_conn)
        state = _state_for(primary_conn)
        json_result = _dict_result(
            state,
            "get_table_context",
            {"table": "arboretum.seedbank.collector", "format": "json"},
        )
        yaml_result = dispatch(
            state,
            "get_table_context",
            {"table": "arboretum.seedbank.collector", "format": "yaml"},
        )

        assert isinstance(yaml_result, str)
        assert yaml.safe_load(yaml_result)["_corrupted"] == json_result["_corrupted"]

    def test_md_format_names_it_exactly_once(self, primary_conn: ConnectionConfig) -> None:
        """The assembler's own header already states `Unreadable: statistics` - the tool must
        not prepend a second note built from a second, independent parse of the same file.
        """

        self._corrupt_statistics(primary_conn)
        state = _state_for(primary_conn)
        result = dispatch(
            state,
            "get_table_context",
            {"table": "arboretum.seedbank.collector", "format": "md"},
        )

        assert isinstance(result, str)
        assert result.count("Unreadable:") == 1
        assert "Unreadable: statistics (present on disk, failed to parse)" in result


class TestListTables:
    def test_default_pattern_returns_all(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "list_tables", {})
        assert "arboretum.seedbank.collector" in result["tables"]

    def test_pattern_filters(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "list_tables", {"pattern": "arboretum.seedbank.*"})
        assert "arboretum.seedbank.collector" in result["tables"]
        assert "arboretum.fixture.shape_probe" not in result["tables"]
        result_none = _dict_result(state, "list_tables", {"pattern": "other.*"})
        assert result_none["tables"] == []

    def test_no_detail_is_byte_identical_to_before(self, primary_conn: ConnectionConfig) -> None:
        """The exact sorted table set the committed print ships - not a subset check."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "list_tables", {})
        assert result["tables"] == [
            "arboretum.fixture.shape_probe",
            "arboretum.seedbank.accession",
            "arboretum.seedbank.accession_summary",
            "arboretum.seedbank.collector",
            "arboretum.seedbank.germination_by_taxon_mv",
            "arboretum.seedbank.germination_trial",
            "arboretum.seedbank.specimen_image",
            "arboretum.seedbank.storage_reading",
            "arboretum.seedbank.taxon",
            "arboretum.seedbank.vault",
        ]
        assert all(isinstance(t, str) for t in result["tables"])

    def test_detail_projects_the_manifest_entry(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "list_tables", {"detail": True})
        entry = next(t for t in result["tables"] if t["table"] == "arboretum.seedbank.collector")

        assert entry["type"] == "table"
        assert entry["row_count"] == 400
        assert entry["columns"] == 10
        assert entry["profiled_at"]

    def test_detail_reads_no_second_file(self, primary_conn: ConnectionConfig) -> None:
        """The manifest already carries every field `detail` projects - no per-table read."""

        table_dir = primary_conn.output / primary_conn.name / "arboretum" / "seedbank" / "collector"
        (table_dir / "statistics.yaml").unlink()

        state = _state_for(primary_conn)
        result = _dict_result(state, "list_tables", {"detail": True})
        entry = next(t for t in result["tables"] if t["table"] == "arboretum.seedbank.collector")

        assert entry["row_count"] == 400


def _edit_manifest_entries(conn: ConnectionConfig, **changes: dict[str, Any]) -> None:
    """Overwrite (or, with a None value, drop) keys of named manifest entries."""

    path = conn.output / conn.name / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text())

    for fqn, fields in changes.items():
        entry = manifest["tables"][fqn.replace("__", ".")]

        for key, value in fields.items():
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value

    path.write_text(yaml.safe_dump(manifest, sort_keys=False))


def _detail(conn: ConnectionConfig) -> dict[str, dict[str, Any]]:
    result = _dict_result(_state_for(conn), "list_tables", {"detail": True})

    return {entry["table"]: entry for entry in result["tables"]}


class TestListTablesFreshness:
    """`detail: true` carries the verdict `dbprint list` buckets a table into."""

    def test_a_table_inside_its_threshold_is_live(self, primary_conn: ConnectionConfig) -> None:
        hour_ago = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _edit_manifest_entries(primary_conn, arboretum__seedbank__taxon={"profiled_at": hour_ago})

        entry = _detail(primary_conn)["arboretum.seedbank.taxon"]

        assert entry["freshness"] == "live"
        assert entry["max_age_days"] == 1
        assert 0 < entry["age_days"] < 0.1

    def test_a_table_past_its_threshold_is_stale(self, primary_conn: ConnectionConfig) -> None:
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _edit_manifest_entries(
            primary_conn,
            arboretum__seedbank__vault={"profiled_at": ten_days_ago, "max_age_days": 7},
        )

        entry = _detail(primary_conn)["arboretum.seedbank.vault"]

        assert entry["freshness"] == "stale"
        assert entry["max_age_days"] == 7
        assert 9.9 < entry["age_days"] < 10.1

    def test_an_unreadable_stamp_is_dormant_with_no_age(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        _edit_manifest_entries(
            primary_conn,
            arboretum__seedbank__taxon={"profiled_at": "not a timestamp"},
        )

        entry = _detail(primary_conn)["arboretum.seedbank.taxon"]

        assert entry["freshness"] == "dormant"
        assert entry["age_days"] is None

    def test_a_refused_threshold_carries_its_reason_and_no_verdict(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        _edit_manifest_entries(primary_conn, arboretum__seedbank__taxon={"max_age_days": -1})

        entries = _detail(primary_conn)
        refused = entries["arboretum.seedbank.taxon"]

        assert "freshness" not in refused
        assert "max_age_days is -1" in refused["threshold_error"]
        assert entries["arboretum.seedbank.vault"]["freshness"] == "stale"

    def test_a_size_gated_threshold_is_warned_about(self, primary_conn: ConnectionConfig) -> None:
        _edit_manifest_entries(primary_conn, arboretum__seedbank__taxon={"max_age_days": None})
        gated = replace(
            primary_conn,
            rules=(RuleConfig(include=("arboretum.seedbank.taxon",), min_rows=10, max_age_days=3),),
        )

        result = _dict_result(_state_for(gated), "list_tables", {"detail": True})

        assert len(result["warnings"]) == 1
        assert "arboretum.seedbank.taxon" in result["warnings"][0]
        assert "min_rows" in result["warnings"][0]

    def test_no_size_gate_means_no_warnings_key(self, primary_conn: ConnectionConfig) -> None:
        assert "warnings" not in _dict_result(
            _state_for(primary_conn),
            "list_tables",
            {"detail": True},
        )


class TestSearchColumnsText:
    """`text` reads what a human wrote about a column, not only what the column is called."""

    def _annotate(self, conn: ConnectionConfig) -> None:
        table_dir = conn.output / conn.name / "arboretum" / "seedbank" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            "format_version: 1\n"
            "columns:\n"
            "  street_address:\n"
            "    note: Where to send correspondence.\n",
        )
        manifest_path = conn.output / conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["arboretum.seedbank.collector"]["artifacts"][
            "statistics_annotations"
        ] = "statistics.annotations.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

    def test_a_note_mentioning_the_text_matches_a_column_named_otherwise(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._annotate(primary_conn)

        result = _dict_result(
            _state_for(primary_conn),
            "search_columns",
            {"text": "correspondence"},
        )

        assert [(m["table"], m["column"]) for m in result["matches"]] == [
            ("arboretum.seedbank.collector", "street_address"),
        ]
        assert result["matches"][0]["annotation"] == "Where to send correspondence."

    def test_the_match_ignores_case(self, primary_conn: ConnectionConfig) -> None:
        self._annotate(primary_conn)
        state = _state_for(primary_conn)

        upper = _dict_result(state, "search_columns", {"text": "CORRESPONDENCE"})
        lower = _dict_result(state, "search_columns", {"text": "correspondence"})

        assert upper["matches"] == lower["matches"]

    def test_a_per_value_note_match_carries_the_matching_values(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        result = _dict_result(_state_for(primary_conn), "search_columns", {"text": "no-treatment"})

        match = next(m for m in result["matches"] if m["column"] == "medium")

        assert match["table"] == "arboretum.seedbank.germination_trial"
        assert [entry["value"] for entry in match["value_notes"]] == ["control"]

    def test_a_name_match_carries_no_value_notes(self, primary_conn: ConnectionConfig) -> None:
        result = _dict_result(_state_for(primary_conn), "search_columns", {"text": "EMAIL"})

        assert {m["column"] for m in result["matches"]} == {"email", "institution_email"}
        assert all("value_notes" not in m for m in result["matches"])

    def test_text_is_anded_with_the_other_filters(self, primary_conn: ConnectionConfig) -> None:
        result = _dict_result(
            _state_for(primary_conn),
            "search_columns",
            {"text": "email", "pattern": "institution_*"},
        )

        assert [m["column"] for m in result["matches"]] == ["institution_email"]


class TestSearchColumns:
    def test_match_email(self, primary_conn: ConnectionConfig) -> None:
        """`email` is an exact (wildcard-free) pattern - arboretum.seedbank.collector is the one match."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "email"})
        assert any(m["column"] == "email" for m in result["matches"])
        match = next(m for m in result["matches"] if m["column"] == "email")
        assert match["table"] == "arboretum.seedbank.collector"
        assert match["classification"] == "text"

    def test_wildcard_pattern(self, primary_conn: ConnectionConfig) -> None:
        """`search_columns` sweeps the whole print, not one table - a subset check, not equality."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "*"})
        cols = {m["column"] for m in result["matches"]}
        assert {"collector_id", "email"}.issubset(cols)

    def test_match_carries_the_fields_it_was_filtered_on(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """A sensitivity/redacted/candidate_key sweep must return the matched category, not
        just a bare column name - the same evidence-carrying pattern `looks_like` already has."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "email"})
        match = next(m for m in result["matches"] if m["column"] == "email")

        assert match["sensitivity"] == "contact"
        assert match["redacted"] == "mask"
        assert match["candidate_key"] is True

    def test_a_missing_path_key_still_resolves_the_table(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        del manifest["tables"]["arboretum.seedbank.collector"]["path"]
        manifest_path.write_text(yaml.safe_dump(manifest))

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "email"})

        assert any(m["column"] == "email" for m in result["matches"])
        assert "arboretum.seedbank.collector" not in result.get("unreadable_tables", [])

    def test_an_annotated_column_carries_its_annotation(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """arboretum.seedbank.collector ships with no statistics.annotations.yaml for real."""

        import yaml

        table_dir = primary_conn.output / primary_conn.name / "arboretum" / "seedbank" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {"format_version": 1, "columns": {"email": {"note": "always lowercase"}}},
            ),
        )
        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["arboretum.seedbank.collector"]["artifacts"][
            "statistics_annotations"
        ] = "statistics.annotations.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest))

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "email"})
        match = next(m for m in result["matches"] if m["column"] == "email")

        assert match["annotation"] == "always lowercase"

    def test_an_unannotated_column_carries_no_annotation_key(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "email"})
        match = next(m for m in result["matches"] if m["column"] == "email")

        assert "annotation" not in match

    def test_a_stale_annotation_key_on_a_table_is_not_a_match(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """A table has statistics, so a key naming a dropped column is stale, not a column."""

        import yaml

        table_dir = primary_conn.output / primary_conn.name / "arboretum" / "seedbank" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {"format_version": 1, "columns": {"not_a_real_column": {"note": "stale"}}},
            ),
        )
        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["arboretum.seedbank.collector"]["artifacts"][
            "statistics_annotations"
        ] = "statistics.annotations.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest))

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "*"})

        assert not any(m["column"] == "not_a_real_column" for m in result["matches"])

    def test_a_views_annotated_column_is_reachable_with_no_statistics(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """A plain view has no statistics.yaml - its annotation is the only column name known."""

        import yaml

        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["public.active_v"] = {
            "type": "view",
            "path": "public/active_v",
            "artifacts": {
                "ddl": "ddl.sql",
                "statistics_annotations": "statistics.annotations.yaml",
            },
            "columns": 1,
            "profiled_at": manifest["tables"]["arboretum.seedbank.collector"]["profiled_at"],
        }
        manifest_path.write_text(yaml.safe_dump(manifest))
        view_dir = primary_conn.output / primary_conn.name / "public" / "active_v"
        view_dir.mkdir(parents=True)
        (view_dir / "ddl.sql").write_text("CREATE VIEW public.active_v AS SELECT shelf_location;\n")
        (view_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {
                    "format_version": 1,
                    "columns": {"shelf_location": {"note": "snapshot at query time"}},
                },
            ),
        )

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "shelf_location"})
        match = next(m for m in result["matches"] if m["table"] == "public.active_v")

        assert match["column"] == "shelf_location"
        assert match["annotation"] == "snapshot at query time"
        assert match["sql_type"] == ""
        assert match["classification"] == ""

    def test_default_call_keeps_every_original_field_unchanged(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """Original keys unchanged; `row_count` is new (SPEC 2.2.8).

        `rows_scanned` is absent because this table's file carries no `scope` block.
        """

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "email"})
        match = next(m for m in result["matches"] if m["column"] == "email")

        assert match["table"] == "arboretum.seedbank.collector"
        assert match["column"] == "email"
        assert match["sql_type"] == "character varying(320)"
        assert match["classification"] == "text"
        assert match["row_count"] == 400
        assert "rows_scanned" not in match
        assert "truncated" not in result
        assert "unreadable_tables" not in result

    def test_classification_filter_ands_with_pattern(self, primary_conn: ConnectionConfig) -> None:
        """Filters AND, never OR: the pattern matches `hired_on`, the classification excludes it."""

        state = _state_for(primary_conn)
        excluded = _dict_result(
            state,
            "search_columns",
            {"pattern": "hired_on", "classification": "categorical"},
        )
        assert excluded["matches"] == []

        included = _dict_result(
            state,
            "search_columns",
            {"pattern": "hired_on", "classification": "temporal"},
        )
        cols = {m["column"] for m in included["matches"]}

        assert cols == {"hired_on"}

    def test_pattern_is_optional(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"classification": "text"})
        matches = {(m["table"], m["column"]) for m in result["matches"]}

        assert ("arboretum.seedbank.collector", "email") in matches

    def test_sql_type_filter(self, primary_conn: ConnectionConfig) -> None:
        """uuid also names accession's and germination_trial's FKs, all named collector_id."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"sql_type": "uuid"})
        cols = {m["column"] for m in result["matches"]}

        assert cols == {"collector_id"}

    def test_candidate_key_filter(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"candidate_key": True})
        matches = {(m["table"], m["column"]) for m in result["matches"]}

        assert ("arboretum.seedbank.collector", "email") in matches
        # institution's cardinality_ratio is 0.0375 - not unique, so not a candidate key.
        assert ("arboretum.seedbank.collector", "institution") not in matches

    def test_candidate_key_false_excludes_columns_never_tested(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """`false` must mean "tested, confirmed not a key", not "no verdict either way" -
        `payload_bytes` is `unsupported` and carries no `inferred` block, so it answers neither.
        """

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"candidate_key": False})
        matches = {(m["table"], m["column"]) for m in result["matches"]}

        assert ("arboretum.seedbank.collector", "postal_code") in matches
        assert ("arboretum.fixture.shape_probe", "payload_bytes") not in matches

    def test_candidate_key_false_includes_a_measured_column_with_no_inferred_block(
        self,
        scoped_conn: ConnectionConfig,
    ) -> None:
        """A boolean, temporal or plain numeric column draws no `looks_like` sample and matches no
        sensitivity rule, so `inferred` is absent - `candidate_key: false` must still match it.
        """

        state = _state_for(scoped_conn)
        result = _dict_result(state, "search_columns", {"candidate_key": False})
        matches = {(m["table"], m["column"]) for m in result["matches"]}

        assert ("public.curator", "email") in matches

    def test_looks_like_filter_carries_the_verdict_and_its_evidence(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """arboretum.seedbank.collector.collector_id carries `looks_like: uuid` for real, no patch needed."""

        state = _state_for(primary_conn)
        result = _dict_result(
            state,
            "search_columns",
            {"looks_like": "uuid", "pattern": "collector_id"},
        )
        match = next(m for m in result["matches"] if m["table"] == "arboretum.seedbank.collector")

        assert match["looks_like"] == "uuid"
        assert match["sampled"] == 400
        assert match["matched"] == 400

    def test_sensitivity_filter(self, primary_conn: ConnectionConfig) -> None:
        """email, phone and institution_email are the print's three contact-sensitive columns."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"sensitivity": "contact"})
        cols = {m["column"] for m in result["matches"]}

        assert cols == {"email", "phone", "institution_email"}

    def test_sensitivity_wildcard_sweeps_every_detection(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """`sensitivity: "*"` finds every column carrying any detection, not a literal '*'."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"sensitivity": "*"})
        cols = {m["column"] for m in result["matches"]}

        assert cols == {
            "logger_ipv4",
            "deployed_at",
            "full_name",
            "email",
            "phone",
            "institution_email",
            "street_address",
            "vernacular_name",
            "site_name",
        }
        # collector_id carries no sensitivity, so the wildcard must not match its absence.
        assert "collector_id" not in cols

    def test_redacted_filter(self, primary_conn: ConnectionConfig) -> None:
        """email, phone, institution_email and shape_probe's logger_ipv4 carry `redacted: mask`."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"redacted": "mask"})
        cols = {m["column"] for m in result["matches"]}

        assert cols == {"logger_ipv4", "email", "phone", "institution_email"}

    def test_unknown_filter_value_is_an_empty_result_not_an_error(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"classification": "nope"})

        assert result["matches"] == []

    def test_a_scoped_match_carries_row_count_and_rows_scanned(
        self,
        scoped_conn: ConnectionConfig,
    ) -> None:
        _narrow_the_seeded_read(scoped_conn, rows_scanned=2)

        state = _state_for(scoped_conn)
        result = _dict_result(state, "search_columns", {"pattern": "email"})
        match = next(m for m in result["matches"] if m["column"] == "email")

        assert match["row_count"] == 5
        assert match["rows_scanned"] == 2


class TestGetManifest:
    def test_returns_parsed_dict(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "get_manifest", {})
        assert result["format_version"] == 1
        assert "arboretum.seedbank.collector" in result["tables"]


class TestGetDiff:
    def test_returns_parsed_diff(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "get_diff", {})
        assert result["format_version"] == 1
        assert result["target"]["source"] == "live_database"

    def test_a_relationship_event_about_a_rejected_edge_is_withheld(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        root = primary_conn.output / primary_conn.name
        trial = "arboretum.seedbank.germination_trial"
        manifest_path = root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"][trial]["artifacts"]["relationships_annotations"] = (
            "relationships.annotations.yaml"
        )
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        edge = {
            "source_table": trial,
            "source_column": ["collector_id"],
            "target_table": "arboretum.seedbank.collector",
            "target_column": ["collector_id"],
        }
        (root / "arboretum/seedbank/germination_trial/relationships.annotations.yaml").write_text(
            yaml.safe_dump(
                {
                    "format_version": 1,
                    "refers_to": [
                        {
                            "column": edge["source_column"],
                            "target_table": edge["target_table"],
                            "target_column": edge["target_column"],
                            "verdict": "rejected",
                        },
                    ],
                },
            ),
        )
        diff = yaml.safe_load((root / "diff.yaml").read_text())
        diff["changes"] = [
            {"kind": "relationship_added", **edge, "detection": "inferred"},
            {
                "kind": "relationship_added",
                **edge,
                "source_column": ["taxon_id"],
                "target_table": "arboretum.seedbank.taxon",
                "target_column": ["taxon_id"],
                "detection": "inferred",
            },
        ]
        diff["summary"] = {**diff.get("summary", {}), "relationships_changed": 2}
        (root / "diff.yaml").write_text(yaml.safe_dump(diff, sort_keys=False))

        result = _dict_result(_state_for(primary_conn), "get_diff", {})

        assert [c["source_column"] for c in result["changes"]] == [["taxon_id"]]
        assert result["summary"]["relationships_changed"] == 1
        assert result["total"] == 1


class TestToolDefinitions:
    def test_tool_names_match_definitions(self) -> None:

        assert tuple(t.name for t in TOOL_DEFINITIONS) == TOOL_NAMES

    def test_every_input_schema_property_carries_a_description(self) -> None:
        """Stops the next parameter shipping bare - worth more than any wording assertion."""

        bare = [
            f"{tool.name}.{name}"
            for tool in TOOL_DEFINITIONS
            for name, prop in (tool.input_schema.get("properties") or {}).items()
            if not prop.get("description")
        ]
        assert bare == []

    def test_every_description_names_a_tool_for_the_neighbouring_task(self) -> None:
        names = {tool.name for tool in TOOL_DEFINITIONS}

        unrouted = [
            tool.name
            for tool in TOOL_DEFINITIONS
            if not any(other in tool.description for other in names - {tool.name})
        ]
        assert unrouted == []


class TestRedactedColumnParity:
    """`get_table_context` and `dbprint context` render a redacted column identically.

    `arboretum.seedbank.collector.email` carries `redacted: mask`, its values already `[redacted]`.
    """

    def test_the_tool_renders_no_fabricated_literal(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = dispatch(
            state,
            "get_table_context",
            {"table": "arboretum.seedbank.collector", "format": "md"},
        )
        assert isinstance(result, str)
        row = next(line for line in result.splitlines() if line.startswith("| email |"))

        assert "NULL" not in row
        assert "redacted: mask" in row


class TestGetReference:
    """Depends on no connection or print - `_state_for` is never called here."""

    _EMPTY_STATE = ServedConnections(served={}, default=None)

    def test_unknown_document_raises(self) -> None:
        with pytest.raises(McpError):
            dispatch(self._EMPTY_STATE, "get_reference", {"document": "readme"})

    def test_missing_document_raises(self) -> None:
        with pytest.raises(McpError):
            dispatch(self._EMPTY_STATE, "get_reference", {})

    def test_no_section_and_a_section_both_resolve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_read` monkeypatched past the editable-install packaging gap; dispatch runs for real."""

        from dbprint.mcp import reference as reference_module

        monkeypatch.setattr(
            reference_module,
            "_read",
            lambda document: "## 1. One\n\nBody one.\n\n## 2. Two\n\nBody two.\n",
        )

        tree = dispatch(self._EMPTY_STATE, "get_reference", {"document": "spec"})
        assert isinstance(tree, str)
        assert "Body one." not in tree
        assert "1. One" in tree
        assert "2. Two" in tree

        section = dispatch(
            self._EMPTY_STATE,
            "get_reference",
            {"document": "spec", "section": "1"},
        )
        assert isinstance(section, str)
        assert "Body one." in section
        assert "Body two." not in section

    def test_unknown_section_raises_naming_the_available_ones(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from dbprint.mcp import reference as reference_module

        monkeypatch.setattr(reference_module, "_read", lambda document: "## 1. One\n\nBody.\n")

        with pytest.raises(McpError) as excinfo:
            dispatch(self._EMPTY_STATE, "get_reference", {"document": "spec", "section": "9.9"})

        assert "9.9" in str(excinfo.value)

    def test_a_verbatim_spec_ref_citation_resolves_through_dispatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A `section` copied straight off a finding's `spec_ref` resolves through `dispatch`."""

        from dbprint.mcp import reference as reference_module

        monkeypatch.setattr(reference_module, "_read", lambda document: "## 1. One\n\nBody.\n")

        section = dispatch(
            self._EMPTY_STATE,
            "get_reference",
            {"document": "spec", "section": "§1"},
        )
        assert "Body." in section

    def test_empty_string_section_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Distinct from omitting `section`, which means the heading tree: `""` is malformed."""

        from dbprint.mcp import reference as reference_module

        monkeypatch.setattr(reference_module, "_read", lambda document: "## 1. One\n\nBody.\n")

        with pytest.raises(McpError):
            dispatch(self._EMPTY_STATE, "get_reference", {"document": "spec", "section": ""})

    def test_a_section_longer_than_a_page_pages_and_rejoins_exactly(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from dbprint.mcp import reference as reference_module

        body = "".join(f"Line {i} of a long leaf section.\n" for i in range(3_000))
        document = f"## 1. One\n\n{body}\n## 2. Two\n\nBody two.\n"
        monkeypatch.setattr(reference_module, "_read", lambda document_: document)

        text_pages = _mcp_pages.pages(
            self._EMPTY_STATE,
            "get_reference",
            {"document": "spec", "section": "1"},
        )

        assert len(text_pages) > 1
        assert all(len(page) <= 20_000 for page in text_pages)
        assert _mcp_pages.joined(text_pages) == f"## 1. One\n\n{body}"


class TestGetReferenceServesTheGuide:
    """`document: guide` answers from the installed guide, with no connection served."""

    _EMPTY_STATE = ServedConnections(served={}, default=None)

    def test_no_section_returns_the_heading_tree(self) -> None:
        tree = dispatch(self._EMPTY_STATE, "get_reference", {"document": "guide"})

        assert isinstance(tree, str)
        assert "- Reading a dbprint print" in tree
        assert "  - Vocabulary" in tree

    def test_a_heading_returns_its_text(self) -> None:
        text = dispatch(
            self._EMPTY_STATE,
            "get_reference",
            {"document": "guide", "section": "VOCABULARY"},
        )

        assert isinstance(text, str)
        assert text.startswith("## Vocabulary")
        assert "## Fields that are easy to misread" not in text

    def test_an_unknown_heading_fails_naming_the_headings(self) -> None:
        with pytest.raises(McpError) as excinfo:
            dispatch(
                self._EMPTY_STATE,
                "get_reference",
                {"document": "guide", "section": "No such heading"},
            )

        message = str(excinfo.value)
        assert "'No such heading' not found in guide" in message
        assert "'Vocabulary'" in message


class TestResolveValueReadsTheStatisticsItWasPromised:
    """A broken statistics file reports as broken, never as an unknown column (MCP.md 4.7)."""

    @staticmethod
    def _statistics_path(conn: ConnectionConfig, table_dir: str) -> Any:
        return conn.output / conn.name / table_dir / "statistics.yaml"

    def test_a_corrupt_statistics_file_is_a_parse_error(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._statistics_path(primary_conn, "arboretum/seedbank/taxon").write_text(
            "columns: [unbalanced\n",
        )

        with pytest.raises(McpError) as excinfo:
            dispatch(
                _state_for(primary_conn),
                "resolve_value",
                {"table": "arboretum.seedbank.taxon", "column": "rank", "text": "genus"},
            )

        assert "YAML parse error" in excinfo.value.detail
        assert "not found" not in excinfo.value.detail

    def test_an_absent_statistics_file_names_the_manifest_reference(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._statistics_path(primary_conn, "arboretum/seedbank/taxon").unlink()

        with pytest.raises(McpError) as excinfo:
            dispatch(
                _state_for(primary_conn),
                "resolve_value",
                {"table": "arboretum.seedbank.taxon", "column": "rank", "text": "genus"},
            )

        assert "manifest references statistics.yaml but file is absent" in excinfo.value.detail

    def test_a_corrupt_annotations_file_is_a_parse_error(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """The notes are part of the answer; losing them silently is the same defect."""

        path = primary_conn.output / primary_conn.name / "arboretum/seedbank/germination_trial"
        (path / "statistics.annotations.yaml").write_text("columns: {medium: [\n")

        with pytest.raises(McpError) as excinfo:
            dispatch(
                _state_for(primary_conn),
                "resolve_value",
                {
                    "table": "arboretum.seedbank.germination_trial",
                    "column": "medium",
                    "text": "control",
                },
            )

        assert "statistics.annotations.yaml" in excinfo.value.detail
        assert "YAML parse error" in excinfo.value.detail

    def test_a_table_without_a_statistics_artifact_is_unavailable(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        del manifest["tables"]["arboretum.seedbank.taxon"]["artifacts"]["statistics"]
        manifest_path.write_text(yaml.safe_dump(manifest))

        result = _dict_result(
            _state_for(primary_conn),
            "resolve_value",
            {"table": "arboretum.seedbank.taxon", "column": "rank", "text": "genus"},
        )

        assert result["match"] == "unavailable"
        assert "declares no statistics artifact" in result["reason"]

    def test_a_numeric_list_equal_to_its_exact_cardinality_is_exhaustive(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """SPEC 2.2.5: no `values_coverage` on a numeric column; `frequencies.listed` decides."""

        path = self._statistics_path(primary_conn, "arboretum/seedbank/germination_trial")
        statistics = yaml.safe_load(path.read_text())
        column = statistics["columns"]["sown_count"]
        column["cardinality"] = column["frequencies"]["listed"]
        column["cardinality_method"] = "exact"
        path.write_text(yaml.safe_dump(statistics))

        result = _dict_result(
            _state_for(primary_conn),
            "resolve_value",
            {"table": "arboretum.seedbank.germination_trial", "column": "sown_count", "text": "20"},
        )

        assert result["coverage"] is None
        assert result["exhaustive"] is True
        assert "sample_caveat" not in result
        assert len(result["domain"]) == column["frequencies"]["listed"]

    def test_a_numeric_list_short_of_its_cardinality_is_a_sample(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        result = _dict_result(
            _state_for(primary_conn),
            "resolve_value",
            {"table": "arboretum.seedbank.germination_trial", "column": "sown_count", "text": "20"},
        )

        assert result["coverage"] is None
        assert result["exhaustive"] is False
        assert "not evidence" in result["sample_caveat"]
        assert "domain" not in result


_SCOPE_BLOCK = {"rows_scanned": 2, "filter": "id < 3"}


class TestAScopedFileIsReadAsScopedWhicheverSignalItCarries:
    """The block decides and the echo alone still scopes (SPEC 2.2.8)."""

    @staticmethod
    def _scope(conn: ConnectionConfig, *, block: bool, echo: bool, scanned: int = 2) -> None:
        path = conn.output / conn.name / "public" / "curator" / "statistics.yaml"
        statistics = yaml.safe_load(path.read_text())
        email = statistics["columns"]["email"]
        email.update(classification="categorical", cardinality=2, values_coverage=1.0)
        email["values"] = [{"value": "open", "count": 1}, {"value": "closed", "count": 1}]

        if scanned == 0:
            email.update(cardinality=0, values=[])

        if block:
            statistics["scope"] = {**_SCOPE_BLOCK, "rows_scanned": scanned}

        if echo:
            for column in statistics["columns"].values():
                column["rows_scanned"] = scanned

        path.write_text(yaml.safe_dump(statistics))

    @staticmethod
    def _resolve(conn: ConnectionConfig, text: str) -> dict[str, Any]:
        return _dict_result(
            _state_for(conn),
            "resolve_value",
            {"table": "public.curator", "column": "email", "text": text},
        )

    @pytest.mark.parametrize(
        ("block", "echo", "reply_block"),
        [(True, True, _SCOPE_BLOCK), (True, False, _SCOPE_BLOCK), (False, True, {})],
        ids=["block-and-echo", "echo-stripped", "echo-without-block"],
    )
    @pytest.mark.parametrize(("text", "match"), [("refunded", "none"), ("OPEN", "stored")])
    def test_resolve_value_is_the_scanned_domain(
        self,
        scoped_conn: ConnectionConfig,
        block: bool,
        echo: bool,
        reply_block: dict[str, Any],
        text: str,
        match: str,
    ) -> None:
        self._scope(scoped_conn, block=block, echo=echo)

        reply = self._resolve(scoped_conn, text)

        assert (reply["match"], reply["exhaustive"]) == (match, False)
        assert (reply["scope"], reply["row_count"]) == (reply_block, 5)
        assert "the list is the whole domain over the rows scanned" in reply["sample_caveat"]

    @pytest.mark.parametrize(
        ("block", "echo"),
        [(True, False), (False, True)],
        ids=["echo-stripped", "echo-without-block"],
    )
    def test_search_columns_and_the_notes_stay_qualified(
        self,
        scoped_conn: ConnectionConfig,
        block: bool,
        echo: bool,
    ) -> None:
        self._scope(scoped_conn, block=block, echo=echo)
        state = _state_for(scoped_conn)

        matches = _dict_result(state, "search_columns", {"candidate_key": True})["matches"]
        md = dispatch(state, "get_table_context", {"table": "public.curator", "format": "md"})

        assert [m["column"] for m in matches if "scope" in m] == ["id"]
        assert isinstance(md, str)
        assert "values (complete over the rows scanned): 'open' (50%), 'closed' (50%)" in md
        assert "candidate key over the rows scanned" in md

    def test_an_empty_scan_is_unavailable_and_lists_nothing(
        self,
        scoped_conn: ConnectionConfig,
    ) -> None:
        self._scope(scoped_conn, block=True, echo=True, scanned=0)

        reply = self._resolve(scoped_conn, "open")
        md = dispatch(
            _state_for(scoped_conn),
            "get_table_context",
            {"table": "public.curator", "format": "md"},
        )

        assert (reply["match"], reply["exhaustive"], reply["row_count"]) == (
            "unavailable",
            False,
            5,
        )
        assert reply["scope"]["rows_scanned"] == 0
        assert "domain" not in reply
        assert "no rows" in reply["reason"]
        assert isinstance(md, str)
        assert "values (complete over the rows scanned): none" in md

    def test_an_unscoped_file_carries_no_scope(self, scoped_conn: ConnectionConfig) -> None:
        self._scope(scoped_conn, block=False, echo=False)

        reply = self._resolve(scoped_conn, "refunded")

        assert (reply["match"], reply["exhaustive"]) == ("none", True)
        assert "scope" not in reply


class TestAnUnwalkableManifestEntry:
    """One entry that is not a mapping drops its own table and nothing else, in both modes."""

    @staticmethod
    def _corrupt_one(conn: ConnectionConfig) -> tuple[str, list[str]]:
        path = conn.output / conn.name / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        broken, *rest = sorted(manifest["tables"])
        manifest["tables"][broken] = "garbage"
        path.write_text(yaml.safe_dump(manifest))

        return broken, rest

    @pytest.mark.parametrize("detail", [True, False])
    def test_the_other_tables_are_listed(
        self,
        primary_conn: ConnectionConfig,
        detail: bool,
    ) -> None:
        broken, rest = self._corrupt_one(primary_conn)
        reply = _dict_result(_state_for(primary_conn), "list_tables", {"detail": detail})
        listed = [t["table"] if detail else t for t in reply["tables"]]

        assert broken not in listed
        assert listed == rest

    def test_a_context_request_for_the_unwalkable_table_names_it_unknown(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        broken, _ = self._corrupt_one(primary_conn)

        with pytest.raises(McpError):
            dispatch(_state_for(primary_conn), "get_table_context", {"table": broken})


class TestAnnotationsWithoutReadableStatistics:
    def test_search_finds_an_annotated_column_when_statistics_are_corrupt(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        table_dir = primary_conn.output / primary_conn.name / "arboretum" / "seedbank" / "collector"
        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["arboretum.seedbank.collector"]["artifacts"][
            "statistics_annotations"
        ] = "statistics.annotations.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest))
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {"format_version": 1, "columns": {"email": {"note": "a field station address"}}},
            ),
        )
        (table_dir / "statistics.yaml").write_text("not: valid: yaml: [")

        reply = _dict_result(_state_for(primary_conn), "search_columns", {"text": "field station"})

        assert [(m["table"], m["column"]) for m in reply["matches"]] == [
            ("arboretum.seedbank.collector", "email"),
        ]
