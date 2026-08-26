"""Tool dispatch + per-tool behavior per MCP.md 4."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from dbprint.config import ConnectionConfig
from dbprint.engine import AssemblyOptions, assemble_context
from dbprint.mcp import McpError, ServedConnections, dispatch
from dbprint.mcp.tools import TOOL_NAMES


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
        assert result["relationships"]["refers_to"] == []
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
