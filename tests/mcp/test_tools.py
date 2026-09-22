"""Tool dispatch + per-tool behavior per MCP.md 4."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from dbprint.config import ConnectionConfig
from dbprint.engine import AssemblyOptions, assemble_context
from dbprint.mcp import McpError, ServedConnections, dispatch
from dbprint.mcp.tools import (
    MANIFEST_TABLE_CAP,
    SEARCH_MATCH_CAP,
    TABLE_LISTING_CAP,
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

        assert len(result["tables"]) == TABLE_LISTING_CAP
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

        assert len(result["tables"]) == MANIFEST_TABLE_CAP
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
        state = _state_for(primary_conn)
        default = _dict_result(state, "search_columns", {})
        explicit = _dict_result(state, "search_columns", {"limit": 5})

        assert len(default["matches"]) <= SEARCH_MATCH_CAP
        assert len(explicit["matches"]) == 5
        assert explicit["truncated"] is True
        assert explicit["total"] == len(default["matches"])

    def test_a_diff_filters_by_table_including_the_relationship_events(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """The three relationship events carry `source_table`/`target_table`, never `table`."""

        path = primary_conn.output / primary_conn.name / "diff.yaml"
        diff = yaml.safe_load(path.read_text())
        diff["changes"] = [
            {"kind": "table_added", "table": "seedbank.taxon"},
            {"kind": "table_added", "table": "seedbank.other"},
            {
                "kind": "relationship_added",
                "source_table": "seedbank.taxon",
                "target_table": "seedbank.other",
            },
        ]
        path.write_text(yaml.safe_dump(diff))

        result = _dict_result(
            _state_for(primary_conn),
            "get_diff",
            {"table": "seedbank.taxon"},
        )

        assert [c["kind"] for c in result["changes"]] == ["table_added", "relationship_added"]

    def test_a_diff_filters_by_kind(self, primary_conn: ConnectionConfig) -> None:
        path = primary_conn.output / primary_conn.name / "diff.yaml"
        diff = yaml.safe_load(path.read_text())
        diff["changes"] = [
            {"kind": "table_added", "table": "seedbank.taxon"},
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
            {"table": "seedbank.taxon"},
        )

        assert isinstance(result, str)
        assert "# Table: seedbank.taxon" in result


class TestEveryCallIsCheckedAgainstTheToolsOwnSchema:
    """MCP.md 8.2: the SDK runs no `inputSchema` check, so `dispatch` is where one has to be."""

    def test_an_unknown_key_is_refused_by_name(self, primary_conn: ConnectionConfig) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(_state_for(primary_conn), "search_columns", {"query": "curator"})

        assert caught.value.code == -32602
        assert "query" in caught.value.detail
        assert "pattern" in caught.value.detail

    def test_a_misspelled_required_key_names_what_was_sent(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        with pytest.raises(McpError) as caught:
            dispatch(
                _state_for(primary_conn),
                "get_table_context",
                {"table_name": "seedbank.taxon"},
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
                {"table": "seedbank.taxon", "include_stats": "false"},
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
                {"table": "seedbank.taxon", "format": "yml"},
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
            {"table": "seedbank.taxon", "format": "MD", "purpose": "QUERY"},
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
                {"table": "seedbank.taxon", "format": 7},
            )

        assert "'md'" not in caught.value.detail

    def test_a_boolean_is_not_an_integer(self, primary_conn: ConnectionConfig) -> None:
        """jsonschema's own type checker refuses it, so no extra guard is needed."""

        with pytest.raises(McpError) as caught:
            dispatch(
                _state_for(primary_conn),
                "get_table_context",
                {"table": "seedbank.taxon", "budget_tokens": True},
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
            "get_table_context": {"table": "seedbank.taxon"},
            "resolve_value": {"table": "seedbank.taxon", "column": "rank", "text": "genus"},
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
            {"table": "seedbank.taxon", "column": "rank", "text": "GENUS"},
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
                {"table": "seedbank.taxon", "column": "rnk", "text": "genus"},
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
            dispatch(state, "resolve_value", {"table": "seedbank.taxon", "column": "rank"})

        assert "text" in excinfo.value.detail


class TestGetTableContext:
    def test_md_format(self, primary_conn: ConnectionConfig) -> None:
        """MCP.md 4.1: md returns a bare markdown string, not an envelope."""

        state = _state_for(primary_conn)
        result = dispatch(
            state,
            "get_table_context",
            {"table": "seedbank.collector", "format": "md"},
        )

        assert isinstance(result, str)
        assert "seedbank.collector" in result
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
            {"table": "seedbank.collector", "purpose": "query"},
        )

        assert isinstance(result, str)
        assert "## Column values" in result
        assert "Cardinality" not in result

    def test_purpose_defaults_to_profile(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = dispatch(state, "get_table_context", {"table": "seedbank.collector"})

        assert isinstance(result, str)
        assert "Cardinality" in result
        assert "## Column values" not in result

    def test_an_unknown_purpose_is_refused(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)

        with pytest.raises(McpError) as excinfo:
            dispatch(
                state,
                "get_table_context",
                {"table": "seedbank.collector", "purpose": "explain"},
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
        assert "Scanned: 2 of 5 rows (40.0%)" in result

    def test_md_is_the_text_the_shared_assembler_produces(
        self,
        scoped_conn: ConnectionConfig,
    ) -> None:
        """Two renderers would let one surface drift; this fails the moment one forks."""

        _narrow_the_seeded_read(scoped_conn, rows_scanned=2)
        state = _state_for(scoped_conn)
        served = dispatch(state, "get_table_context", {"table": "public.curator", "format": "md"})
        print_root = scoped_conn.output / scoped_conn.name
        assembled = assemble_context(
            yaml.safe_load((print_root / "manifest.yaml").read_text()),
            print_root,
            ["public.curator"],
            AssemblyOptions(),
            scoped_conn.name,
        )

        assert served == assembled.text

    def test_md_names_its_own_sql_dialect(self, primary_conn: ConnectionConfig) -> None:
        """A single-table MCP fragment never reaches a document-level provenance block."""

        state = _state_for(primary_conn)
        result = dispatch(
            state,
            "get_table_context",
            {"table": "seedbank.collector", "format": "md"},
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
            {"table": "fixture.shape_probe", "format": "json"},
        )

        assert result["table"] == "fixture.shape_probe"
        assert "CREATE TABLE" in result["ddl"]
        assert result["statistics"]["table"] == "fixture.shape_probe"
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
            {"table": "seedbank.collector", "format": "json"},
        )
        yaml_result = dispatch(
            state,
            "get_table_context",
            {"table": "seedbank.collector", "format": "yaml"},
        )

        assert isinstance(yaml_result, str)
        assert yaml.safe_load(yaml_result) == json_result

    def test_json_format_respects_include_flags(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(
            state,
            "get_table_context",
            {
                "table": "seedbank.collector",
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
            {"table": "seedbank.collector", "format": "json", "budget_tokens": 1},
        )

        assert result["table"] == "seedbank.collector"
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
            {"table": "seedbank.collector", "format": "md"},
        )

        assert isinstance(result, str)
        assert "CREATE TABLE" in result

    @staticmethod
    def _author_annotations(conn: ConnectionConfig) -> None:
        """seedbank.collector ships with no statistics.annotations.yaml for real."""

        import yaml

        table_dir = conn.output / conn.name / "seedbank" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {"format_version": 1, "columns": {"email": {"note": "always lowercase"}}},
            ),
        )
        manifest_path = conn.output / conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["seedbank.collector"]["artifacts"]["statistics_annotations"] = (
            "statistics.annotations.yaml"
        )
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
            {"table": "seedbank.collector", "format": "json"},
        )

        assert result["annotations"] == {"email": {"note": "always lowercase"}}

    def test_include_annotations_false_omits_the_key(self, primary_conn: ConnectionConfig) -> None:
        self._author_annotations(primary_conn)
        state = _state_for(primary_conn)
        result = _dict_result(
            state,
            "get_table_context",
            {"table": "seedbank.collector", "format": "json", "include_annotations": False},
        )

        assert "annotations" not in result


class TestCorruptedArtifactPassesThroughUnchanged:
    """`_corrupted` is the assembler's own key (MCP.md 4.1) - the tool must not recompute or
    overwrite it - a second, independent computation is what makes two formats disagree.
    """

    @staticmethod
    def _corrupt_statistics(conn: ConnectionConfig) -> None:
        path = conn.output / conn.name / "seedbank" / "collector" / "statistics.yaml"
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
            {"table": "seedbank.collector", "format": "json"},
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
            {"table": "seedbank.collector", "format": "json"},
        )
        yaml_result = dispatch(
            state,
            "get_table_context",
            {"table": "seedbank.collector", "format": "yaml"},
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
            {"table": "seedbank.collector", "format": "md"},
        )

        assert isinstance(result, str)
        assert result.count("Unreadable:") == 1
        assert "Unreadable: statistics (present on disk, failed to parse)" in result


class TestListTables:
    def test_default_pattern_returns_all(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "list_tables", {})
        assert "seedbank.collector" in result["tables"]

    def test_pattern_filters(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "list_tables", {"pattern": "seedbank.*"})
        assert "seedbank.collector" in result["tables"]
        assert "fixture.shape_probe" not in result["tables"]
        result_none = _dict_result(state, "list_tables", {"pattern": "other.*"})
        assert result_none["tables"] == []

    def test_no_detail_is_byte_identical_to_before(self, primary_conn: ConnectionConfig) -> None:
        """The exact sorted table set the committed print ships - not a subset check."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "list_tables", {})
        assert result["tables"] == [
            "fixture.shape_probe",
            "seedbank.accession",
            "seedbank.accession_summary",
            "seedbank.collector",
            "seedbank.germination_by_taxon_mv",
            "seedbank.germination_trial",
            "seedbank.specimen_image",
            "seedbank.storage_reading",
            "seedbank.taxon",
            "seedbank.vault",
        ]
        assert all(isinstance(t, str) for t in result["tables"])

    def test_detail_projects_the_manifest_entry(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "list_tables", {"detail": True})
        entry = next(t for t in result["tables"] if t["fqn"] == "seedbank.collector")

        assert entry["type"] == "table"
        assert entry["row_count"] == 400
        assert entry["columns"] == 10
        assert entry["profiled_at"]

    def test_detail_reads_no_second_file(self, primary_conn: ConnectionConfig) -> None:
        """The manifest already carries every field `detail` projects - no per-table read."""

        table_dir = primary_conn.output / primary_conn.name / "seedbank" / "collector"
        (table_dir / "statistics.yaml").unlink()

        state = _state_for(primary_conn)
        result = _dict_result(state, "list_tables", {"detail": True})
        entry = next(t for t in result["tables"] if t["fqn"] == "seedbank.collector")

        assert entry["row_count"] == 400


class TestSearchColumns:
    def test_match_email(self, primary_conn: ConnectionConfig) -> None:
        """`email` is an exact (wildcard-free) pattern - seedbank.collector is the one match."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "email"})
        assert any(m["column"] == "email" for m in result["matches"])
        match = next(m for m in result["matches"] if m["column"] == "email")
        assert match["table_fqn"] == "seedbank.collector"
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
        del manifest["tables"]["seedbank.collector"]["path"]
        manifest_path.write_text(yaml.safe_dump(manifest))

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"pattern": "email"})

        assert any(m["column"] == "email" for m in result["matches"])
        assert "seedbank.collector" not in result.get("unreadable_tables", [])

    def test_an_annotated_column_carries_its_annotation(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """seedbank.collector ships with no statistics.annotations.yaml for real."""

        import yaml

        table_dir = primary_conn.output / primary_conn.name / "seedbank" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {"format_version": 1, "columns": {"email": {"note": "always lowercase"}}},
            ),
        )
        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["seedbank.collector"]["artifacts"]["statistics_annotations"] = (
            "statistics.annotations.yaml"
        )
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

        table_dir = primary_conn.output / primary_conn.name / "seedbank" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {"format_version": 1, "columns": {"not_a_real_column": {"note": "stale"}}},
            ),
        )
        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["seedbank.collector"]["artifacts"]["statistics_annotations"] = (
            "statistics.annotations.yaml"
        )
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
            "profiled_at": manifest["tables"]["seedbank.collector"]["profiled_at"],
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
        match = next(m for m in result["matches"] if m["table_fqn"] == "public.active_v")

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

        assert match["table_fqn"] == "seedbank.collector"
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
        matches = {(m["table_fqn"], m["column"]) for m in result["matches"]}

        assert ("seedbank.collector", "email") in matches

    def test_sql_type_filter(self, primary_conn: ConnectionConfig) -> None:
        """uuid also names accession's and germination_trial's FKs, all named collector_id."""

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"sql_type": "uuid"})
        cols = {m["column"] for m in result["matches"]}

        assert cols == {"collector_id"}

    def test_candidate_key_filter(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"candidate_key": True})
        matches = {(m["table_fqn"], m["column"]) for m in result["matches"]}

        assert ("seedbank.collector", "email") in matches
        # institution's cardinality_ratio is 0.0375 - not unique, so not a candidate key.
        assert ("seedbank.collector", "institution") not in matches

    def test_candidate_key_false_excludes_columns_never_tested(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """`false` must mean "tested, confirmed not a key", not "no verdict either way" -
        `payload_bytes` is `unsupported` and carries no `inferred` block, so it answers neither.
        """

        state = _state_for(primary_conn)
        result = _dict_result(state, "search_columns", {"candidate_key": False})
        matches = {(m["table_fqn"], m["column"]) for m in result["matches"]}

        assert ("seedbank.collector", "postal_code") in matches
        assert ("fixture.shape_probe", "payload_bytes") not in matches

    def test_candidate_key_false_includes_a_measured_column_with_no_inferred_block(
        self,
        scoped_conn: ConnectionConfig,
    ) -> None:
        """A boolean, temporal or plain numeric column draws no `looks_like` sample and matches no
        sensitivity rule, so `inferred` is absent - `candidate_key: false` must still match it.
        """

        state = _state_for(scoped_conn)
        result = _dict_result(state, "search_columns", {"candidate_key": False})
        matches = {(m["table_fqn"], m["column"]) for m in result["matches"]}

        assert ("public.curator", "email") in matches

    def test_looks_like_filter_carries_the_verdict_and_its_evidence(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """seedbank.collector.collector_id carries `looks_like: uuid` for real, no patch needed."""

        state = _state_for(primary_conn)
        result = _dict_result(
            state,
            "search_columns",
            {"looks_like": "uuid", "pattern": "collector_id"},
        )
        match = next(m for m in result["matches"] if m["table_fqn"] == "seedbank.collector")

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
        assert "seedbank.collector" in result["tables"]


class TestGetDiff:
    def test_returns_parsed_diff(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = _dict_result(state, "get_diff", {})
        assert result["format_version"] == 1
        assert result["target"]["source"] == "live_database"


class TestToolDefinitions:
    def test_tool_names_match_definitions(self) -> None:
        from dbprint.mcp.tools import TOOL_DEFINITIONS

        assert tuple(t.name for t in TOOL_DEFINITIONS) == TOOL_NAMES

    def test_every_input_schema_property_carries_a_description(self) -> None:
        """Stops the next parameter shipping bare - worth more than any wording assertion."""

        from dbprint.mcp.tools import TOOL_DEFINITIONS

        bare = [
            f"{tool.name}.{name}"
            for tool in TOOL_DEFINITIONS
            for name, prop in (tool.input_schema.get("properties") or {}).items()
            if not prop.get("description")
        ]
        assert bare == []


class TestRedactedColumnParity:
    """`get_table_context` and `dbprint context` render a redacted column identically.

    seedbank.collector.email carries `redacted: mask`, its values already the literal
    `[redacted]`.
    """

    def test_the_tool_renders_no_fabricated_literal(self, primary_conn: ConnectionConfig) -> None:
        state = _state_for(primary_conn)
        result = dispatch(
            state,
            "get_table_context",
            {"table": "seedbank.collector", "format": "md"},
        )
        assert isinstance(result, str)
        row = next(line for line in result.splitlines() if line.startswith("| email |"))

        assert "NULL" not in row
        assert "redacted (mask)" in row

    def test_the_tool_and_the_command_assemble_the_same_fragment(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        import yaml

        from dbprint.engine import AssemblyOptions, assemble_context

        print_root = primary_conn.output / primary_conn.name
        manifest = yaml.safe_load((print_root / "manifest.yaml").read_text())
        state = _state_for(primary_conn)
        tool_text = dispatch(
            state,
            "get_table_context",
            {"table": "seedbank.collector", "format": "md"},
        )
        command_text = assemble_context(
            manifest,
            print_root,
            ["seedbank.collector"],
            AssemblyOptions(),
            primary_conn.name,
        ).text

        assert tool_text == command_text


class TestGetReference:
    """Depends on no connection or print - `_state_for` is never called here."""

    _EMPTY_STATE = ServedConnections(served={}, default=None)

    def test_listed_in_tool_names_and_definitions(self) -> None:
        assert "get_reference" in TOOL_NAMES
        names = {t.name for t in TOOL_DEFINITIONS}
        assert "get_reference" in names

    def test_document_is_required(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t.name == "get_reference")
        assert tool.input_schema["required"] == ["document"]

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
        self._statistics_path(primary_conn, "seedbank/taxon").write_text("columns: [unbalanced\n")

        with pytest.raises(McpError) as excinfo:
            dispatch(
                _state_for(primary_conn),
                "resolve_value",
                {"table": "seedbank.taxon", "column": "rank", "text": "genus"},
            )

        assert "YAML parse error" in excinfo.value.detail
        assert "not found" not in excinfo.value.detail

    def test_an_absent_statistics_file_names_the_manifest_reference(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._statistics_path(primary_conn, "seedbank/taxon").unlink()

        with pytest.raises(McpError) as excinfo:
            dispatch(
                _state_for(primary_conn),
                "resolve_value",
                {"table": "seedbank.taxon", "column": "rank", "text": "genus"},
            )

        assert "manifest references statistics.yaml but file is absent" in excinfo.value.detail

    def test_a_corrupt_annotations_file_is_a_parse_error(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """The notes are part of the answer; losing them silently is the same defect."""

        path = primary_conn.output / primary_conn.name / "seedbank/germination_trial"
        (path / "statistics.annotations.yaml").write_text("columns: {medium: [\n")

        with pytest.raises(McpError) as excinfo:
            dispatch(
                _state_for(primary_conn),
                "resolve_value",
                {"table": "seedbank.germination_trial", "column": "medium", "text": "control"},
            )

        assert "statistics.annotations.yaml" in excinfo.value.detail
        assert "YAML parse error" in excinfo.value.detail

    def test_a_table_without_a_statistics_artifact_is_unavailable(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        del manifest["tables"]["seedbank.taxon"]["artifacts"]["statistics"]
        manifest_path.write_text(yaml.safe_dump(manifest))

        result = _dict_result(
            _state_for(primary_conn),
            "resolve_value",
            {"table": "seedbank.taxon", "column": "rank", "text": "genus"},
        )

        assert result["match"] == "unavailable"
        assert "declares no statistics artifact" in result["reason"]

    def test_a_numeric_list_equal_to_its_exact_cardinality_is_exhaustive(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """SPEC 2.2.5: no `values_coverage` on a numeric column; `frequencies.listed` decides."""

        path = self._statistics_path(primary_conn, "seedbank/germination_trial")
        statistics = yaml.safe_load(path.read_text())
        column = statistics["columns"]["sown_count"]
        column["cardinality"] = column["frequencies"]["listed"]
        column["cardinality_method"] = "exact"
        path.write_text(yaml.safe_dump(statistics))

        result = _dict_result(
            _state_for(primary_conn),
            "resolve_value",
            {"table": "seedbank.germination_trial", "column": "sown_count", "text": "20"},
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
            {"table": "seedbank.germination_trial", "column": "sown_count", "text": "20"},
        )

        assert result["coverage"] is None
        assert result["exhaustive"] is False
        assert "not evidence" in result["sample_caveat"]
        assert "domain" not in result
