"""Tool dispatch + per-tool behavior per MCP.md 4."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml

from dbprint.config import ConnectionConfig
from dbprint.config.project import RuleConfig
from dbprint.mcp import McpError, ServedConnections, dispatch
from dbprint.mcp.tools import (
    TOOL_DEFINITIONS,
    TOOL_NAMES,
)


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


class TestNoToolReturnsAnUnboundedReply:
    """A reply past a client's ceiling is not a large answer but no answer, plus a turn."""

    @staticmethod
    def _wide_manifest(conn: ConnectionConfig, tables: int) -> None:
        """Grow the manifest past every listing cap, reusing one seeded table's entry."""

        path = conn.output / conn.name / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        entry = next(iter(manifest["tables"].values()))
        manifest["tables"] = {f"seedbank.t{i:04d}": dict(entry) for i in range(tables)}
        path.write_text(yaml.safe_dump(manifest))

    def test_a_catalogue_listing_is_capped_and_says_what_it_was_cut_from(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 600)
        result = _dict_result(_state_for(primary_conn), "list_tables", {})

        assert len(result["tables"]) == 500
        assert result["truncated"] is True
        assert result["total"] == 600

    def test_a_narrowed_listing_under_the_cap_carries_no_marker(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 600)
        result = _dict_result(
            _state_for(primary_conn),
            "list_tables",
            {"pattern": "seedbank.t000*"},
        )

        assert len(result["tables"]) == 10
        assert "truncated" not in result

    def test_a_manifest_caps_its_table_map_and_keeps_the_rest_whole(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 600)
        result = _dict_result(_state_for(primary_conn), "get_manifest", {})

        assert len(result["tables"]) == 500
        assert result["truncated"] is True
        assert result["total"] == 600
        assert result["format_version"] == 1
        assert "generated_at" in result

    def test_a_manifest_narrowed_by_pattern_reaches_past_the_cap(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 600)
        result = _dict_result(
            _state_for(primary_conn),
            "get_manifest",
            {"pattern": "seedbank.t059*"},
        )

        assert sorted(result["tables"]) == [f"seedbank.t059{i}" for i in range(10)]
        assert "truncated" not in result

    def test_a_column_search_is_capped_and_an_explicit_limit_wins(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 600)
        state = _state_for(primary_conn)
        default = _dict_result(state, "search_columns", {})
        explicit = _dict_result(state, "search_columns", {"limit": 5})

        assert len(default["matches"]) == 200
        assert default["truncated"] is True
        assert len(explicit["matches"]) == 5
        assert explicit["truncated"] is True
        assert explicit["total"] == default["total"] > 200

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

    def test_a_table_context_call_carries_a_default_budget(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """An unbudgeted caller gets a truncation marker rather than an unbounded reply."""

        result = dispatch(
            _state_for(primary_conn),
            "get_table_context",
            {"table": "arboretum.seedbank.taxon"},
        )

        assert isinstance(result, str)
        assert "# Table: arboretum.seedbank.taxon" in result


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

    def test_a_value_below_a_declared_minimum_is_refused(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(_state_for(primary_conn), "search_columns", {"limit": 0})

        assert ">= 1" in caught.value.detail

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
        result = _dict_result(
            state,
            "get_table_context",
            {"table": "arboretum.fixture.shape_probe", "format": "json"},
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

        import yaml

        state = _state_for(primary_conn)
        json_result = dispatch(
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
        assert yaml.safe_load(yaml_result) == json_result

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

    def test_json_format_budget_drops_whole_sections(self, primary_conn: ConnectionConfig) -> None:
        """budget_tokens still applies to structured output - sections drop, identity survives."""

        state = _state_for(primary_conn)
        result = _dict_result(
            state,
            "get_table_context",
            {"table": "arboretum.seedbank.collector", "format": "json", "budget_tokens": 1},
        )

        assert result["table"] == "arboretum.seedbank.collector"
        assert "ddl" not in result
        assert "statistics" not in result
        assert result["_truncated"]

    def test_md_format_is_unaffected_by_the_structured_path(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """md is an independent code path from json/yaml; budget_tokens must not affect it."""

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

    def test_limit_caps_and_signals_truncation(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "*", "limit": 1})

        assert len(result["matches"]) == 1
        assert result["truncated"] is True

    def test_limit_above_the_match_count_does_not_signal_truncation(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """1000 comfortably exceeds the committed print's total column count across every table."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "*", "limit": 1000})

        assert "truncated" not in result


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
        assert "redacted (mask)" in row


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
        assert "2 distinct over the rows scanned: open / closed" in md
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
        assert "0 distinct over the rows scanned" in md

    def test_an_unscoped_file_carries_no_scope(self, scoped_conn: ConnectionConfig) -> None:
        self._scope(scoped_conn, block=False, echo=False)

        reply = self._resolve(scoped_conn, "refunded")

        assert (reply["match"], reply["exhaustive"]) == ("none", True)
        assert "scope" not in reply
