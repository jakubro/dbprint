"""context_assembler end-to-end shape tests using a synthetic on-disk print."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, ClassVar

import yaml

from dbprint.engine import AssemblyOptions, assemble_context, assemble_structured_context
from dbprint.engine.context_assembler import (
    TableArtifacts,
    _escape_cell,
    _markdown_catalog_only_columns,
    _markdown_null_patterns,
    _render_table_markdown,
    _stripped_statistics,
    fk_target_map,
)
from dbprint.engine.context_assembler import (
    _markdown_relationships as _relationship_lines,
)


MANIFEST: dict[str, object] = {
    "format_version": 1,
    "generated_at": "2026-06-09T00:00:00Z",
    "connection": "primary",
    "adapter": "postgres",
    "dbprint_version": "0.1.0",
    "tables": {
        "herbarium.public.collector": {
            "type": "table",
            "path": "herbarium/public/collector",
            "artifacts": {
                "ddl": "ddl.sql",
                "statistics": "statistics.yaml",
                "relationships": "relationships.yaml",
                "description": "description.md",
            },
            "row_count": 100,
            "columns": 3,
            "profiled_at": "2026-06-09T00:00:00Z",
        },
    },
}

STATS: dict[str, object] = {
    "format_version": 1,
    "table": "herbarium.public.collector",
    "type": "table",
    "profiled_at": "2026-06-09T00:00:00Z",
    "row_count": 100,
    "row_count_method": "exact",
    "columns": {
        "collector_id": {
            "sql_type": "uuid",
            "nullable": False,
            "null_count": 0,
            "null_rate": 0.0,
            "cardinality": 100,
            "cardinality_ratio": 1.0,
            "cardinality_method": "exact",
            "classification": "text",
            "values": [
                {"value": "00000000-0000-7000-8000-000000000001", "count": 1},
                {"value": "00000000-0000-7000-8000-000000000002", "count": 1},
            ],
            "values_coverage": 0.02,
            "distribution": "uniform",
            "inferred": {"candidate_key": True, "looks_like": "uuid"},
        },
        "rank": {
            "sql_type": "varchar(20)",
            "nullable": False,
            "null_count": 0,
            "null_rate": 0.0,
            "cardinality": 3,
            "cardinality_ratio": 0.03,
            "cardinality_method": "exact",
            "classification": "categorical",
            "values": [
                {"value": "trainee", "count": 60},
                {"value": "certified", "count": 30},
                {"value": "senior", "count": 10},
            ],
            "values_coverage": 1.0,
            "distribution": "imbalanced",
        },
        "seed_count": {
            "sql_type": "integer",
            "nullable": True,
            "null_count": 5,
            "null_rate": 0.05,
            "cardinality": 80,
            "cardinality_ratio": 0.8,
            "cardinality_method": "exact",
            "classification": "numeric",
            "range": {"min": 18, "max": 99},
            "percentiles": {"p50": 42},
            "distribution": "uniform",
        },
    },
}

RELATIONSHIPS: dict[str, object] = {
    "format_version": 1,
    "table": "herbarium.public.collector",
    "profiled_at": "2026-06-09T00:00:00Z",
    "refers_to": [],
    "referenced_by": [],
}


def _seed_print(tmp_path: Path) -> Path:
    print_root = tmp_path / "prints" / "primary"
    table_dir = print_root / "herbarium" / "public" / "collector"
    table_dir.mkdir(parents=True)
    (print_root / "manifest.yaml").write_text(yaml.safe_dump(MANIFEST))
    (table_dir / "ddl.sql").write_text("CREATE TABLE collector (collector_id uuid PRIMARY KEY);\n")
    (table_dir / "statistics.yaml").write_text(yaml.safe_dump(STATS))
    (table_dir / "relationships.yaml").write_text(yaml.safe_dump(RELATIONSHIPS))
    (table_dir / "description.md").write_text("Collector roster. Stable, append-only.\n")

    return print_root


class TestMarkdown:
    def test_full_render_single_table(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )
        assert "# Table: herbarium.public.collector  (100 rows, 3 columns)" in result.text
        assert "## DDL" in result.text
        assert "## Description" in result.text
        assert "## Cardinality & key columns" in result.text
        assert "; candidate key" in result.text
        assert "P50: 42" in result.text

    def test_no_ddl_omits_ddl_section(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        opts = AssemblyOptions(include_ddl=False)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            opts,
            "primary",
        )
        assert "## DDL" not in result.text

    def test_no_stats_omits_cardinality_table(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        opts = AssemblyOptions(include_stats=False)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            opts,
            "primary",
        )
        assert "## Cardinality & key columns" not in result.text

    def test_tight_budget_truncates_sections(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        opts = AssemblyOptions(budget=20)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            opts,
            "primary",
        )
        assert "<!-- truncated:" in result.text

    def test_a_budget_too_tight_for_the_header_is_not_counted_as_included(
        self,
        tmp_path: Path,
    ) -> None:
        """The entire output is a truncation-marker comment - a caller checking
        `tables_included == 0` must see that, not a false "one table rendered".
        """

        print_root = _seed_print(tmp_path)
        opts = AssemblyOptions(budget=1)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            opts,
            "primary",
        )

        assert result.tables_included == 0
        assert result.truncated == ("herbarium.public.collector",)
        assert "<!-- truncated:" in result.text


class TestCorruptArtifact:
    """A present-but-unparseable artifact must not read as never-profiled (SPEC 2.5)."""

    def test_malformed_yaml_states_unreadable_not_absent(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        stats_path = print_root / "herbarium" / "public" / "collector" / "statistics.yaml"
        stats_path.write_text("row_count: [1, 2\n")  # unterminated flow sequence

        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "Unreadable: statistics (present on disk, failed to parse)" in result.text
        assert "## Cardinality & key columns" not in result.text
        assert "Missing:" not in result.text

    def test_wrong_shaped_yaml_states_unreadable_too(self, tmp_path: Path) -> None:
        """Valid YAML that parses to a list, not a mapping, is misshapen, not absent."""

        print_root = _seed_print(tmp_path)
        stats_path = print_root / "herbarium" / "public" / "collector" / "statistics.yaml"
        stats_path.write_text("- a\n- b\n")

        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "Unreadable: statistics" in result.text

    def test_a_genuinely_absent_file_still_reports_missing(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        stats_path = print_root / "herbarium" / "public" / "collector" / "statistics.yaml"
        stats_path.unlink()

        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "Missing: statistics (declared but missing from disk)" in result.text
        assert "Unreadable:" not in result.text


TWO_TABLE_MANIFEST: dict[str, object] = {
    "format_version": 1,
    "generated_at": "2026-06-09T00:00:00Z",
    "connection": "primary",
    "adapter": "postgres",
    "dbprint_version": "0.1.0",
    "tables": {
        **MANIFEST["tables"],
        "herbarium.public.specimen_loan": {
            "type": "table",
            "path": "herbarium/public/specimen_loan",
            "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
            "row_count": 5,
            "columns": 1,
            "profiled_at": "2026-06-09T00:00:00Z",
        },
    },
}

SPECIMEN_LOAN_STATS: dict[str, object] = {
    "format_version": 1,
    "table": "herbarium.public.specimen_loan",
    "type": "table",
    "profiled_at": "2026-06-09T00:00:00Z",
    "row_count": 5,
    "row_count_method": "exact",
    "columns": {
        "id": {
            "sql_type": "integer",
            "nullable": False,
            "null_count": 0,
            "null_rate": 0.0,
            "cardinality": 5,
            "cardinality_ratio": 1.0,
            "cardinality_method": "exact",
            "classification": "numeric",
            "range": {"min": 1, "max": 5},
            "percentiles": {"p50": 3},
        },
    },
}


def _seed_two_table_print(tmp_path: Path, notes: str | None = None) -> Path:
    """A two-table print, optionally carrying connection-grain notes (SPEC 2.7.3)."""

    print_root = _seed_print(tmp_path)
    (print_root / "manifest.yaml").write_text(yaml.safe_dump(TWO_TABLE_MANIFEST))
    specimen_loan_dir = print_root / "herbarium" / "public" / "specimen_loan"
    specimen_loan_dir.mkdir(parents=True)
    (specimen_loan_dir / "ddl.sql").write_text("CREATE TABLE specimen_loan (id int PRIMARY KEY);\n")
    (specimen_loan_dir / "statistics.yaml").write_text(yaml.safe_dump(SPECIMEN_LOAN_STATS))

    if notes is not None:
        (print_root / "manifest.annotations.yaml").write_text(
            yaml.safe_dump({"format_version": 1, "notes": notes}),
        )

    return print_root


class TestConnectionNotes:
    """SPEC 2.7.3: `manifest.annotations.yaml` carries once, only when rendering >1 table."""

    def test_notes_appear_once_in_a_multi_table_render(self, tmp_path: Path) -> None:
        print_root = _seed_two_table_print(tmp_path, notes="Warehouse-wide fact.")
        result = assemble_context(
            TWO_TABLE_MANIFEST,
            print_root,
            ["herbarium.public.collector", "herbarium.public.specimen_loan"],
            AssemblyOptions(),
            "primary",
        )

        assert result.text.count("Warehouse-wide fact.") == 1

    def test_absent_manifest_annotations_renders_no_notes_block(self, tmp_path: Path) -> None:
        print_root = _seed_two_table_print(tmp_path, notes=None)
        result = assemble_context(
            TWO_TABLE_MANIFEST,
            print_root,
            ["herbarium.public.collector", "herbarium.public.specimen_loan"],
            AssemblyOptions(),
            "primary",
        )

        assert "# Context for connection primary" in result.text

    def test_corrupt_manifest_annotations_is_reported_not_silently_dropped(
        self,
        tmp_path: Path,
    ) -> None:
        """A parse failure is distinguishable from "no notes were ever written"."""

        print_root = _seed_two_table_print(tmp_path, notes=None)
        (print_root / "manifest.annotations.yaml").write_text("not: [valid: yaml")

        result = assemble_context(
            TWO_TABLE_MANIFEST,
            print_root,
            ["herbarium.public.collector", "herbarium.public.specimen_loan"],
            AssemblyOptions(),
            "primary",
        )

        assert "Unreadable: manifest_annotations (present on disk, failed to parse)" in result.text

    def test_a_single_table_render_carries_no_connection_notes(self, tmp_path: Path) -> None:
        """A single-table render has no connection-wide slot; the fact is out of scope for it."""

        print_root = _seed_two_table_print(tmp_path, notes="Warehouse-wide fact.")
        result = assemble_context(
            TWO_TABLE_MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "Warehouse-wide fact." not in result.text


FULL_PROVENANCE_MANIFEST: dict[str, object] = {
    "format_version": 1,
    "generated_at": "2026-06-09T00:00:00Z",
    "connection": "primary",
    "adapter": "postgres",
    "dbprint_version": "0.1.0",
    "default_collation": "en_US.utf8",
    "redaction_rules_configured": 2,
    "selectors": {"include": ["herbarium.public.*"], "exclude": []},
    "statistics_params": {"percentiles": [5, 25, 75, 95]},
    "tables": {
        **MANIFEST["tables"],
        "herbarium.public.specimen_loan": {
            "type": "table",
            "path": "herbarium/public/specimen_loan",
            "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
            "row_count": 5,
            "columns": 1,
            "profiled_at": "2026-06-09T00:00:00Z",
        },
    },
}


def _seed_full_provenance_print(tmp_path: Path) -> Path:
    print_root = _seed_print(tmp_path)
    (print_root / "manifest.yaml").write_text(yaml.safe_dump(FULL_PROVENANCE_MANIFEST))
    specimen_loan_dir = print_root / "herbarium" / "public" / "specimen_loan"
    specimen_loan_dir.mkdir(parents=True)
    (specimen_loan_dir / "ddl.sql").write_text("CREATE TABLE specimen_loan (id int PRIMARY KEY);\n")
    (specimen_loan_dir / "statistics.yaml").write_text(yaml.safe_dump(SPECIMEN_LOAN_STATS))

    return print_root


class TestProvenance:
    """SPEC 2.5: the manifest parameters that decided what was measured."""

    def test_multi_table_render_carries_the_provenance_block_once(self, tmp_path: Path) -> None:
        print_root = _seed_full_provenance_print(tmp_path)
        result = assemble_context(
            FULL_PROVENANCE_MANIFEST,
            print_root,
            ["herbarium.public.collector", "herbarium.public.specimen_loan"],
            AssemblyOptions(),
            "primary",
        )

        assert result.text.count("## Provenance") == 1
        assert "- Adapter: postgres" in result.text
        assert "- dbprint version: 0.1.0" in result.text
        assert "- Default collation: en_US.utf8" in result.text
        assert "- Redaction configured: 2 rules" in result.text
        assert "- Selectors applied to this print: include herbarium.public.*" in result.text
        assert "- Percentiles configured: P5, P25, P75, P95" in result.text

    def test_single_table_render_carries_no_provenance_block(self, tmp_path: Path) -> None:
        """It gets its own adapter line instead."""

        print_root = _seed_full_provenance_print(tmp_path)
        result = assemble_context(
            FULL_PROVENANCE_MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "## Provenance" not in result.text

    def test_single_table_render_still_states_its_adapter(self, tmp_path: Path) -> None:
        print_root = _seed_full_provenance_print(tmp_path)
        result = assemble_context(
            FULL_PROVENANCE_MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "Adapter: postgres" in result.text

    def test_connection_name_mismatch_is_stated(self, tmp_path: Path) -> None:
        print_root = _seed_full_provenance_print(tmp_path)
        result = assemble_context(
            FULL_PROVENANCE_MANIFEST,
            print_root,
            ["herbarium.public.collector", "herbarium.public.specimen_loan"],
            AssemblyOptions(),
            "not-primary",
        )

        assert "Connection name mismatch" in result.text

    def test_no_selectors_or_redaction_renders_no_line_for_them(self, tmp_path: Path) -> None:
        print_root = _seed_two_table_print(tmp_path)
        result = assemble_context(
            TWO_TABLE_MANIFEST,
            print_root,
            ["herbarium.public.collector", "herbarium.public.specimen_loan"],
            AssemblyOptions(),
            "primary",
        )

        assert "Selectors applied" not in result.text
        assert "Redaction configured" not in result.text


class TestStatisticsParamsOverride:
    """A table-level `statistics_params` override is never silently applied."""

    def test_a_differing_table_override_is_stated_on_that_table(self, tmp_path: Path) -> None:
        manifest: dict[str, Any] = copy.deepcopy(FULL_PROVENANCE_MANIFEST)
        manifest["statistics_params"] = {"top_n_values": 10}
        manifest["tables"]["herbarium.public.collector"]["statistics_params"] = {"top_n_values": 3}
        print_root = _seed_full_provenance_print(tmp_path)
        (print_root / "manifest.yaml").write_text(yaml.safe_dump(manifest))
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "Statistics params override: top_n_values=3" in result.text

    def test_an_identical_override_states_nothing(self, tmp_path: Path) -> None:
        manifest: dict[str, Any] = copy.deepcopy(FULL_PROVENANCE_MANIFEST)
        manifest["statistics_params"] = {"top_n_values": 10}
        manifest["tables"]["herbarium.public.collector"]["statistics_params"] = {"top_n_values": 10}
        print_root = _seed_full_provenance_print(tmp_path)
        (print_root / "manifest.yaml").write_text(yaml.safe_dump(manifest))
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "Statistics params override" not in result.text


SCOPED_MANIFEST: dict[str, object] = {
    "format_version": 1,
    "connection": "primary",
    "adapter": "snowflake",
    "tables": {
        "herbarium.public.field_log": {
            "type": "table",
            "path": "herbarium/public/field_log",
            "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
            "row_count": 4_000_000,
            "columns": 2,
        },
    },
}


def _scoped_column(cardinality: int, rows_scanned: int) -> dict[str, object]:
    """One `text` column of a scoped file, which echoes its population (SPEC 2.2.8)."""

    return {
        "sql_type": "varchar(64)",
        "nullable": False,
        "null_count": 0,
        "null_rate": 0.0,
        "cardinality": cardinality,
        # SPEC 2.2.2: the ratio is 0 where nothing was scanned, not undefined.
        "cardinality_ratio": round(cardinality / rows_scanned, 6) if rows_scanned else 0.0,
        "cardinality_method": "exact",
        "classification": "text",
        "rows_scanned": rows_scanned,
        "values": [],
        "values_coverage": 1.0,
        "distribution": "uniform",
    }


def _seed_scoped_print(tmp_path: Path, scope: dict[str, object]) -> Path:
    """A one-table print whose statistics describe part of the table."""

    print_root = tmp_path / "prints" / "primary"
    table_dir = print_root / "herbarium" / "public" / "field_log"
    table_dir.mkdir(parents=True)
    (print_root / "manifest.yaml").write_text(yaml.safe_dump(SCOPED_MANIFEST))
    (table_dir / "ddl.sql").write_text("CREATE TABLE field_log (trace_id varchar(64));\n")
    (table_dir / "statistics.yaml").write_text(
        yaml.safe_dump(
            {
                "format_version": 1,
                "table": "herbarium.public.field_log",
                "type": "table",
                "profiled_at": "2026-06-09T00:00:00Z",
                "row_count": 4_000_000,
                "row_count_method": "exact",
                "scope": scope,
                "columns": {
                    "trace_id": _scoped_column(400_000, 400_000),
                    "region": _scoped_column(37, 400_000),
                },
            },
        ),
    )

    return print_root


def _render_scoped(
    print_root: Path,
    budget: int | None = None,
    include_stats: bool = True,
) -> str:
    return assemble_context(
        SCOPED_MANIFEST,
        print_root,
        ["herbarium.public.field_log"],
        AssemblyOptions(budget=budget, include_stats=include_stats),
        "primary",
    ).text


class TestScopeQualifier:
    """A narrowed read is stated where a reader meets the numbers it qualifies (SPEC 2.2.8)."""

    def test_the_header_states_the_scanned_set_against_the_whole_table(
        self,
        tmp_path: Path,
    ) -> None:
        root = _seed_scoped_print(tmp_path, {"rows_scanned": 400_000, "sample": 0.1})

        assert "Scanned: 400000 of 4000000 rows (10%)" in _render_scoped(root)

    def test_a_sampled_read_says_sampled_and_never_the_fraction_asked_for(
        self,
        tmp_path: Path,
    ) -> None:
        root = _seed_scoped_print(tmp_path, {"rows_scanned": 520_000, "sample": 0.1234567890123456})
        text = _render_scoped(root)

        assert "Scanned: 520000 of 4000000 rows (13%); sampled\n" in text
        assert "0.123" not in text

    def test_a_filtered_read_carries_the_predicate_verbatim(self, tmp_path: Path) -> None:
        predicate = "created_at >= '2024-01-01'"
        root = _seed_scoped_print(tmp_path, {"rows_scanned": 400_000, "filter": predicate})
        text = _render_scoped(root)

        assert f"Scanned: 400000 of 4000000 rows (10%); filtered by `{predicate}`" in text
        assert "sample" not in text

    def test_an_unscoped_table_carries_no_scanned_set_line(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "Scanned:" not in result.text

    def test_the_qualifier_survives_a_budget_that_drops_every_other_section(
        self,
        tmp_path: Path,
    ) -> None:
        """A count is unreadable without it, so it rides the section nothing drops."""

        root = _seed_scoped_print(tmp_path, {"rows_scanned": 400_000, "sample": 0.1})
        text = _render_scoped(root, budget=40)

        assert "Scanned: 400000 of 4000000 rows (10%)" in text
        assert "## Cardinality & key columns" not in text

    def test_dropping_the_statistics_drops_the_qualifier_with_them(self, tmp_path: Path) -> None:
        root = _seed_scoped_print(tmp_path, {"rows_scanned": 400_000, "sample": 0.1})

        assert "Scanned:" not in _render_scoped(root, include_stats=False)


class TestCardinalityCueNamesItsPopulation:
    """The saturation cue compares like with like, and says which set it compared."""

    def test_a_scoped_column_saturating_its_draw_names_the_scanned_set(
        self,
        tmp_path: Path,
    ) -> None:
        root = _seed_scoped_print(tmp_path, {"rows_scanned": 400_000, "sample": 0.1})

        assert "| trace_id | 400000 (= scanned rows) |" in _render_scoped(root)

    def test_a_scoped_column_below_its_draw_carries_no_cue(self, tmp_path: Path) -> None:
        root = _seed_scoped_print(tmp_path, {"rows_scanned": 400_000, "sample": 0.1})

        assert "| region | 37 |" in _render_scoped(root)

    def test_a_scoped_column_is_never_compared_against_the_whole_table(
        self,
        tmp_path: Path,
    ) -> None:
        """`row_count` exceeds the draw by the unread remainder, so it can never match."""

        root = _seed_scoped_print(tmp_path, {"rows_scanned": 400_000, "sample": 0.1})

        assert "(= row count)" not in _render_scoped(root)

    def test_an_unscoped_column_still_compares_against_the_table(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "| collector_id | 100 (= row count) |" in result.text

    def test_an_approximate_count_is_marked_an_estimate(self, tmp_path: Path) -> None:
        """`cardinality_method: approximate` marks an estimate; exact is unmarked."""

        print_root = tmp_path / "prints" / "primary"
        table_dir = print_root / "herbarium" / "public" / "collector"
        table_dir.mkdir(parents=True)
        stats = json.loads(json.dumps(STATS))
        stats["columns"]["seed_count"]["cardinality_method"] = "approximate"
        (print_root / "manifest.yaml").write_text(yaml.safe_dump(MANIFEST))
        (table_dir / "ddl.sql").write_text(
            "CREATE TABLE collector (collector_id uuid PRIMARY KEY);\n",
        )
        (table_dir / "statistics.yaml").write_text(yaml.safe_dump(stats))
        (table_dir / "relationships.yaml").write_text(yaml.safe_dump(RELATIONSHIPS))

        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "| seed_count | 80 (approx) |" in result.text
        assert "| collector_id | 100 (= row count) |" in result.text

    def test_a_read_that_matched_no_rows_saturates_nothing(self, tmp_path: Path) -> None:
        """SPEC 2.2.7's empty scanned set: `0 == 0` must not read as a full domain."""

        print_root = tmp_path / "prints" / "primary"
        table_dir = print_root / "herbarium" / "public" / "field_log"
        table_dir.mkdir(parents=True)
        (print_root / "manifest.yaml").write_text(yaml.safe_dump(SCOPED_MANIFEST))
        (table_dir / "ddl.sql").write_text("CREATE TABLE field_log (trace_id varchar(64));\n")
        (table_dir / "statistics.yaml").write_text(
            yaml.safe_dump(
                {
                    "format_version": 1,
                    "table": "herbarium.public.field_log",
                    "type": "table",
                    "profiled_at": "2026-06-09T00:00:00Z",
                    "row_count": 4_000_000,
                    "row_count_method": "exact",
                    "scope": {"rows_scanned": 0, "filter": "region = 'nowhere'"},
                    "columns": {"trace_id": _scoped_column(0, 0)},
                },
            ),
        )
        text = _render_scoped(print_root)

        assert "Scanned: 0 of 4000000 rows (0%)" in text
        assert "| trace_id | 0 |" in text


def _seed_timeline_print(tmp_path: Path, coverage: float) -> Path:
    """A one-table print whose statistics carry a `timeline` block (SPEC 2.2.16)."""

    print_root = tmp_path / "prints" / "primary"
    table_dir = print_root / "herbarium" / "public" / "field_log"
    table_dir.mkdir(parents=True)
    (print_root / "manifest.yaml").write_text(yaml.safe_dump(SCOPED_MANIFEST))
    (table_dir / "ddl.sql").write_text("CREATE TABLE field_log (trace_id varchar(64));\n")
    (table_dir / "statistics.yaml").write_text(
        yaml.safe_dump(
            {
                "format_version": 1,
                "table": "herbarium.public.field_log",
                "type": "table",
                "profiled_at": "2026-06-09T00:00:00Z",
                "row_count": 4_000_000,
                "row_count_method": "exact",
                "timeline": {
                    "column": "created_at",
                    "unit": "day",
                    "buckets": [{"start": "2024-01-01", "count": 1}],
                    "coverage": coverage,
                },
                "columns": {"trace_id": _scoped_column(4_000_000, 4_000_000)},
            },
        ),
    )

    return print_root


class TestTimelineCoverageQualifier:
    """A `<1.0` coverage never reads as complete (SPEC 2.2.16) - a null anchor value counts
    toward `rows_scanned` but no bucket, the one way coverage falls short of `1.0`.
    """

    def test_a_coverage_short_of_1_never_renders_as_100_percent(self, tmp_path: Path) -> None:
        root = _seed_timeline_print(tmp_path, 0.996)
        text = _render_scoped(root)

        assert "99.6% of scanned rows" in text
        assert "100% of scanned rows" not in text

    def test_the_clamp_ceiling_itself_never_renders_as_100_percent(self, tmp_path: Path) -> None:
        """`0.999999` is the ceiling a `<1.0` coverage is clamped to, and rounding to one decimal
        would send it to the `100%` the words above are withheld for claiming.
        """

        root = _seed_timeline_print(tmp_path, 0.999999)
        text = _render_scoped(root)

        assert "99.9999% of scanned rows" in text
        assert "100% of scanned rows" not in text
        assert "every scanned row" not in text

    def test_a_coverage_of_exactly_1_renders_every_scanned_row(self, tmp_path: Path) -> None:
        root = _seed_timeline_print(tmp_path, 1.0)

        assert "every scanned row" in _render_scoped(root)


def _markdown_relationships(a: TableArtifacts) -> str:
    return "\n".join(line.text for line in _relationship_lines(a))


class TestRelationshipsMarkdown:
    """Every rendered edge states its `detection` (SPEC 2.3); `on_delete=` never on a guess."""

    @staticmethod
    def _artifacts(
        relationships: dict[str, Any],
        relationship_annotations: list[dict[str, Any]] | None = None,
    ) -> TableArtifacts:
        return TableArtifacts(
            fqn="public.t",
            table_type="table",
            row_count=10,
            column_count=1,
            ddl="",
            statistics=None,
            relationships=relationships,
            description=None,
            annotations=None,
            annotated_grain=None,
            relationship_annotations=relationship_annotations,
            missing=(),
            corrupted={},
            statistics_params_override=None,
        )

    def test_a_declared_refers_to_edge_states_its_detection_and_action(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["a_id"],
                    "target_table": "public.a",
                    "target_column": ["id"],
                    "on_delete": "CASCADE",
                    "detection": "declared",
                },
            ],
            "referenced_by": [],
        }
        line = _markdown_relationships(self._artifacts(relationships))

        assert "- -> public.a.id (declared); via: a_id; on delete: CASCADE" in line

    def test_an_inferred_refers_to_edge_states_its_detection_and_no_action(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["a_id"],
                    "target_table": "public.a",
                    "target_column": ["id"],
                    "detection": "inferred",
                },
            ],
            "referenced_by": [],
        }
        line = _markdown_relationships(self._artifacts(relationships))

        assert "- -> public.a.id (inferred); via: a_id" in line
        assert "on delete" not in line

    def test_a_declared_referenced_by_edge_states_its_detection_and_action(self) -> None:
        relationships = {
            "refers_to": [],
            "referenced_by": [
                {
                    "column": ["id"],
                    "referencer_table": "public.b",
                    "referencer_column": ["a_id"],
                    "on_delete": "RESTRICT",
                    "detection": "declared",
                },
            ],
        }
        line = _markdown_relationships(self._artifacts(relationships))

        assert "- <- public.b.a_id (declared); on delete: RESTRICT" in line

    def test_an_inferred_referenced_by_edge_states_its_detection_and_no_action(self) -> None:
        relationships = {
            "refers_to": [],
            "referenced_by": [
                {
                    "column": ["id"],
                    "referencer_table": "public.b",
                    "referencer_column": ["a_id"],
                    "detection": "inferred",
                },
            ],
        }
        line = _markdown_relationships(self._artifacts(relationships))

        assert "<- public.b.a_id (inferred)" in line
        assert "on_delete" not in line

    def test_declared_and_inferred_edges_on_one_table_are_distinguishable(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["a_id"],
                    "target_table": "public.a",
                    "target_column": ["id"],
                    "on_delete": "CASCADE",
                    "detection": "declared",
                },
                {
                    "column": ["b_id"],
                    "target_table": "public.b",
                    "target_column": ["id"],
                    "detection": "inferred",
                },
            ],
            "referenced_by": [],
        }
        lines = _markdown_relationships(self._artifacts(relationships)).splitlines()
        declared_line = next(line for line in lines if "a_id" in line)
        inferred_line = next(line for line in lines if "b_id" in line)

        assert declared_line != inferred_line
        assert "declared" in declared_line
        assert "inferred" in inferred_line

    def test_a_missing_detection_defaults_to_the_weaker_claim(self) -> None:
        """A hand-edited artifact missing the required field must not read as a guarantee."""

        relationships = {
            "refers_to": [
                {"column": ["a_id"], "target_table": "public.a", "target_column": ["id"]},
            ],
            "referenced_by": [],
        }
        line = _markdown_relationships(self._artifacts(relationships))

        assert "inferred" in line
        assert "declared" not in line

    def test_no_relationships_at_all_reads_as_the_generic_none(self) -> None:
        relationships = {"refers_to": [], "referenced_by": []}
        line = _markdown_relationships(self._artifacts(relationships))

        assert "- (none)" in line
        assert "join target" not in line

    def test_an_ineligible_target_states_why_nothing_references_it(self) -> None:
        """SPEC 2.3.8: `eligible_target: false` is a stronger claim than a bare "(none)"."""

        relationships = {"refers_to": [], "referenced_by": [], "eligible_target": False}
        line = _markdown_relationships(self._artifacts(relationships))

        assert "- (none - not a join target, no declared-unique column)" in line


class TestObservedRendering:
    """SPEC 2.3.10: what an edge costs rides beside its declared shape, never replacing it."""

    @staticmethod
    def _artifacts(relationships: dict[str, Any]) -> TableArtifacts:
        return TableArtifacts(
            fqn="public.t",
            table_type="table",
            row_count=10,
            column_count=1,
            ddl="",
            statistics=None,
            relationships=relationships,
            description=None,
            annotations=None,
            annotated_grain=None,
            relationship_annotations=None,
            missing=(),
            corrupted={},
            statistics_params_override=None,
        )

    def test_every_distinct_value_compared_reads_exact(self) -> None:
        observed = {
            "fanout_avg": 1.0,
            "target_coverage": 1.0,
            "containment": 1.0,
            "answerable_count": 40,
        }
        a = self._artifacts(self._refers_to(observed))
        a.statistics = {"columns": {"a_id": {"cardinality": 40}}}

        assert "; contained: 100% of referencing values (exact)" in _markdown_relationships(a)

    def test_an_incoming_edge_decides_exactness_by_the_referencers_column(self) -> None:
        observed = {
            "fanout_avg": 1.0,
            "target_coverage": 1.0,
            "containment": 1.0,
            "answerable_count": 40,
        }
        edge = {
            "column": ["id"],
            "referencer_table": "public.b",
            "referencer_column": ["a_id"],
            "detection": "declared",
            "observed": observed,
        }
        a = self._artifacts({"refers_to": [], "referenced_by": [edge]})
        a.incoming_cardinalities = {("public.b", "a_id"): 40}

        assert "; contained: 100% of referencing values (exact)" in _markdown_relationships(a)

    def _refers_to(self, observed: dict[str, Any] | None) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "column": ["a_id"],
            "target_table": "public.a",
            "target_column": ["id"],
            "detection": "declared",
        }

        if observed is not None:
            entry["observed"] = observed

        return {"refers_to": [entry], "referenced_by": []}

    def test_a_computed_edge_states_fanout_and_coverage(self) -> None:
        observed = {
            "fanout_avg": 10.0,
            "fanout_max": 15,
            "target_coverage": 0.4,
            "coherent": True,
            "scope_compatible": True,
        }
        line = _markdown_relationships(self._artifacts(self._refers_to(observed)))

        assert "\n  fanout avg: 10.0; fanout max: 15; covers: 40% of target values" in line

    def test_an_absent_fanout_max_renders_no_max_clause(self) -> None:
        observed = {"fanout_avg": 1.0, "target_coverage": 1.0, "scope_compatible": True}
        line = _markdown_relationships(self._artifacts(self._refers_to(observed)))

        assert "\n  fanout avg: 1.0; covers: 100% of target values" in line
        assert "max" not in line

    def test_an_incoherent_edge_carries_a_visible_marker(self) -> None:
        observed = {
            "fanout_avg": 1.5,
            "target_coverage": 1.5,
            "coherent": False,
            "scope_compatible": True,
        }
        line = _markdown_relationships(self._artifacts(self._refers_to(observed)))

        assert line.endswith("; incoherent (more distinct referencing values than target values)")

    def test_scope_incompatible_states_the_scopes_were_compared(self) -> None:
        """Distinct from `no_observed_block_renders_nothing_extra` below: this edge WAS
        measured, and found incomparable - it must not read as merely unmeasured."""

        line = _markdown_relationships(
            self._artifacts(self._refers_to({"scope_compatible": False})),
        )

        assert "\n  not measured (one side was read in part)" in line
        assert "covers" not in line

    def test_no_observed_block_renders_nothing_extra(self) -> None:
        line = _markdown_relationships(self._artifacts(self._refers_to(None)))

        assert "observed" not in line

    def test_containment_carries_its_answerable_count(self) -> None:
        observed = {
            "fanout_avg": 1.0,
            "target_coverage": 1.0,
            "containment": 0.6,
            "answerable_count": 42,
            "scope_compatible": True,
        }
        line = _markdown_relationships(self._artifacts(self._refers_to(observed)))

        assert "; contained: 60% of referencing values (\u00b115.4%, 42 compared)" in line

    def test_containment_with_no_answerable_count_renders_the_ratio_alone(self) -> None:
        observed = {
            "fanout_avg": 1.0,
            "target_coverage": 1.0,
            "containment": 0.6,
            "scope_compatible": True,
        }
        line = _markdown_relationships(self._artifacts(self._refers_to(observed)))

        assert "; contained: 60% of referencing values" in line
        assert "answerable" not in line


class TestNullPatternsMarkdown:
    """SPEC 2.2.10: the table-level coverage line states when its own share is a clamp."""

    @staticmethod
    def _artifacts(null_patterns: dict[str, Any]) -> TableArtifacts:
        return TableArtifacts(
            fqn="public.t",
            table_type="table",
            row_count=10,
            column_count=1,
            ddl="",
            statistics={"null_patterns": null_patterns},
            relationships=None,
            description=None,
            annotations=None,
            annotated_grain=None,
            relationship_annotations=None,
            missing=(),
            corrupted={},
            statistics_params_override=None,
        )

    @staticmethod
    def _text(null_patterns: dict[str, Any], row_count: int | None = 1000) -> str:
        a = TestNullPatternsMarkdown._artifacts(null_patterns)
        assert a.statistics is not None
        a.statistics["row_count"] = row_count

        return "\n".join(line.text for line in _markdown_null_patterns(a))

    @staticmethod
    def _patterns(counts: list[int]) -> list[dict[str, Any]]:
        return [{"columns": [f"c{i}"], "count": n} for i, n in enumerate(counts)]

    def test_rows_are_shares_and_the_footer_covers_only_the_rows_shown(self) -> None:
        counts = [229, 228, 115, 114, 58, 57, 38, 38, 29, 28, 15, 14, 8, 7, 1, 1]
        text = self._text({"patterns": self._patterns(counts), "coverage": 0.98})

        assert "| 22.9% | c0 |" in text
        assert "| 3.8% | c7 |" in text
        assert "c8" not in text
        assert "further" not in text
        assert text.endswith("\n\nShown combinations cover 87.7% of scanned rows.")

    def test_every_row_listed_and_shown_reads_every_scanned_row(self) -> None:
        text = self._text({"patterns": self._patterns([600, 300, 100]), "coverage": 1.0})

        assert text.endswith("Shown combinations cover every scanned row.")

    def test_a_bounded_census_keeps_its_hedge(self) -> None:
        null_patterns = {
            "patterns": self._patterns([600, 300]),
            "coverage": 0.9,
            "coverage_method": "bounded",
        }

        assert self._text(null_patterns).endswith("cover 90% of scanned rows (bounded).")

    def test_an_overrun_is_clamped_to_every_scanned_row(self) -> None:
        null_patterns = {
            "patterns": self._patterns([700, 400]),
            "coverage": 0.999999,
            "coverage_method": "bounded",
        }

        assert self._text(null_patterns).endswith("cover every scanned row (bounded).")

    def test_with_no_denominator_rows_keep_their_counts_and_no_footer(self) -> None:
        text = self._text({"patterns": self._patterns([6, 3]), "coverage": 0}, row_count=None)

        assert "| 6 | c0 |" in text
        assert "Shown combinations" not in text


class TestAPartsPresenceNamesItsPopulation:
    @staticmethod
    def _presences() -> list[str]:
        from dbprint.engine.context_assembler import _markdown_parts

        parts = {
            ".site": {"classification": "json", "occurrences": 60, "null_count": 0},
            ".site.cohort": {"classification": "text", "occurrences": 40, "null_count": 0},
            ".readings": {"classification": "composite", "occurrences": 60, "null_count": 0},
            ".readings[*]": {"classification": "json", "occurrences": 140, "null_count": 0},
            ".readings[*].depth": {"classification": "numeric", "occurrences": 80, "null_count": 0},
        }
        a = TestRelationshipsMarkdown._artifacts({})
        a.statistics = {
            "row_count": 100,
            "columns": {"field_log": {"classification": "json", "null_count": 20, "parts": parts}},
        }

        return [
            line.text.rsplit("; ", 1)[-1].rstrip(" |")
            for line in _markdown_parts(a, AssemblyOptions())
            if "present:" in line.text
        ]

    def test_a_top_level_member_is_a_share_of_rows(self) -> None:
        assert "present: 60% of rows" in self._presences()

    def test_a_nested_member_names_its_parent_and_its_row_share(self) -> None:
        assert "present: 66.7% of field_log.site (40% of rows)" in self._presences()

    def test_a_member_under_an_array_states_no_row_share(self) -> None:
        assert "present: 57.1% of field_log.readings[*]" in self._presences()


class TestRelationshipsAreRanked:
    def test_outgoing_and_incoming_lines_run_declared_inferred_measured(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["a"],
                    "target_table": "m",
                    "target_column": ["id"],
                    "detection": "measured",
                },
                {
                    "column": ["a"],
                    "target_table": "d",
                    "target_column": ["id"],
                    "detection": "declared",
                },
            ],
            "referenced_by": [
                {
                    "column": ["id"],
                    "referencer_table": "r",
                    "referencer_column": ["x"],
                    "detection": "inferred",
                },
                {
                    "column": ["id"],
                    "referencer_table": "s",
                    "referencer_column": ["y"],
                    "detection": "declared",
                },
            ],
        }
        lines = _markdown_relationships(
            TestRelationshipsMarkdown._artifacts(relationships),
        ).splitlines()

        assert lines == [
            "## Relationships",
            "- -> d.id (declared); via: a",
            "- -> m.id (measured); via: a",
            "- <- s.y (declared)",
            "- <- r.x (inferred)",
        ]


class TestFkTargetMap:
    """The Notes-cell `FK ->` target states its detection too (SPEC 2.3)."""

    def test_declared_edge(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["herbarium_id"],
                    "target_table": "public.herbarium",
                    "target_column": ["id"],
                    "detection": "declared",
                },
            ],
        }

        assert fk_target_map(relationships) == {
            "herbarium_id": ["public.herbarium.id (declared)"],
        }

    def test_inferred_edge(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["herbarium_id"],
                    "target_table": "public.herbarium",
                    "target_column": ["id"],
                    "detection": "inferred",
                },
            ],
        }

        assert fk_target_map(relationships) == {
            "herbarium_id": ["public.herbarium.id (inferred)"],
        }

    def test_missing_detection_defaults_to_inferred(self) -> None:
        relationships = {
            "refers_to": [
                {"column": ["cultivar_id"], "target_table": "t", "target_column": ["id"]},
            ],
        }

        assert fk_target_map(relationships)["cultivar_id"] == ["t.id (inferred)"]

    def test_a_composite_edge_is_listed_under_each_member(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["a", "b"],
                    "target_table": "t",
                    "target_column": ["x", "y"],
                    "detection": "declared",
                },
            ],
        }

        assert fk_target_map(relationships) == {
            "a": ["(a, b) -> t.(x, y) (declared)"],
            "b": ["(a, b) -> t.(x, y) (declared)"],
        }

    def test_every_edge_ranks_declared_before_inferred_before_measured(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["a"],
                    "target_table": "m",
                    "target_column": ["id"],
                    "detection": "measured",
                },
                {
                    "column": ["a"],
                    "target_table": "i",
                    "target_column": ["id"],
                    "detection": "inferred",
                },
                {
                    "column": ["a"],
                    "target_table": "d",
                    "target_column": ["id"],
                    "detection": "declared",
                },
                {
                    "column": ["a"],
                    "target_table": "n",
                    "target_column": ["id"],
                    "detection": "measured",
                },
            ],
        }

        assert fk_target_map(relationships) == {
            "a": ["d.id (declared)", "i.id (inferred)", "m.id (measured)", "n.id (measured)"],
        }

    def test_a_rejected_edge_is_left_out(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["a"],
                    "target_table": "i",
                    "target_column": ["id"],
                    "detection": "inferred",
                },
                {
                    "column": ["a"],
                    "target_table": "m",
                    "target_column": ["id"],
                    "detection": "measured",
                },
            ],
        }
        verdicts = [
            {"column": ["a"], "target_table": "i", "target_column": ["id"], "verdict": "rejected"},
        ]

        assert fk_target_map(relationships, verdicts) == {"a": ["m.id (measured)"]}

    def test_absent_relationships_map_to_nothing(self) -> None:
        assert fk_target_map(None) == {}


INFERRED_EDGE_RELATIONSHIPS: dict[str, object] = {
    "refers_to": [
        {
            "column": ["garden_id"],
            "target_table": "public.garden",
            "target_column": ["garden_code"],
            "detection": "inferred",
        },
    ],
    "referenced_by": [],
}


class TestStructured:
    def test_json_single_table_is_object(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        opts = AssemblyOptions(format="json")
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            opts,
            "primary",
        )
        parsed = json.loads(result.text)
        assert parsed["table"] == "herbarium.public.collector"
        assert "statistics" in parsed
        assert "ddl" in parsed

    def test_yaml_multi_document_for_multi_table(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        opts = AssemblyOptions(format="yaml")
        # Reuse the same table twice to exercise the multi-doc path
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector", "herbarium.public.collector"],
            opts,
            "primary",
        )
        docs = list(yaml.safe_load_all(result.text))
        assert len(docs) == 2


class TestStructuredBudget:
    """`--budget` applies to json/yaml through the same builder `assemble_structured` uses."""

    def test_json_tight_budget_truncates_and_reports_included(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(format="json", budget=20),
            "primary",
        )
        parsed = json.loads(result.text)

        assert result.tables_included == 1
        assert result.truncated == ("herbarium.public.collector",)
        assert "_truncated" in parsed
        assert parsed["table"] == "herbarium.public.collector"

    def test_yaml_tight_budget_truncates_and_reports_included(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(format="yaml", budget=20),
            "primary",
        )
        parsed = yaml.safe_load(result.text)

        assert result.tables_included == 1
        assert "_truncated" in parsed

    def test_a_split_that_floors_to_zero_excludes_every_table(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector", "herbarium.public.collector"],
            AssemblyOptions(format="json", budget=1),
            "primary",
        )

        assert result.tables_included == 0
        assert set(result.truncated) == {"herbarium.public.collector"}

    def test_unbudgeted_json_includes_every_table_and_carries_no_marker(
        self,
        tmp_path: Path,
    ) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(format="json"),
            "primary",
        )

        assert result.tables_included == 1
        assert result.truncated == ()
        assert "_truncated" not in json.loads(result.text)


class TestSketchStrippedFromStructuredPayload:
    """The assembled structured payload strips each column's sketch, on a copy."""

    _SKETCH: ClassVar[dict[str, str]] = {"method": "kmv_md5_lo64", "values": "A" * 8000}

    @classmethod
    def _stats_with_sketch(cls) -> dict[str, Any]:
        stats: dict[str, Any] = copy.deepcopy(STATS)
        stats["columns"]["collector_id"]["sketch"] = cls._SKETCH

        return stats

    @classmethod
    def _seed(cls, tmp_path: Path) -> Path:
        print_root = tmp_path / "prints" / "primary"
        table_dir = print_root / "herbarium" / "public" / "collector"
        table_dir.mkdir(parents=True)
        (print_root / "manifest.yaml").write_text(yaml.safe_dump(MANIFEST))
        (table_dir / "ddl.sql").write_text(
            "CREATE TABLE collector (collector_id uuid PRIMARY KEY);\n",
        )
        (table_dir / "statistics.yaml").write_text(yaml.safe_dump(cls._stats_with_sketch()))
        (table_dir / "relationships.yaml").write_text(yaml.safe_dump(RELATIONSHIPS))
        (table_dir / "description.md").write_text("Collector roster. Stable, append-only.\n")

        return print_root

    def test_stripped_statistics_is_a_new_object_with_no_sketch_key(self) -> None:
        stats = self._stats_with_sketch()
        stripped = _stripped_statistics(stats)

        assert "sketch" not in stripped["columns"]["collector_id"]
        assert stripped["columns"]["collector_id"]["cardinality"] == 100
        assert stats["columns"]["collector_id"]["sketch"] == self._SKETCH  # input untouched

    def test_json_carries_no_sketch_key(self, tmp_path: Path) -> None:
        print_root = self._seed(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(format="json"),
            "primary",
        )
        parsed = json.loads(result.text)
        column = parsed["statistics"]["columns"]["collector_id"]

        assert "sketch" not in column
        assert column["cardinality"] == 100

    def test_yaml_carries_no_sketch_key(self, tmp_path: Path) -> None:
        print_root = self._seed(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(format="yaml"),
            "primary",
        )
        parsed = yaml.safe_load(result.text)

        assert "sketch" not in parsed["statistics"]["columns"]["collector_id"]

    def test_assemble_structured_context_carries_no_sketch_key(self, tmp_path: Path) -> None:
        """The object MCP's get_table_context returns directly, not the json text round-trip."""

        print_root = self._seed(tmp_path)
        payload = assemble_structured_context(
            MANIFEST,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(format="json"),
        )

        assert "sketch" not in payload["statistics"]["columns"]["collector_id"]

    def test_md_format_is_unaffected_by_a_sketch_on_disk(self, tmp_path: Path) -> None:
        with_sketch = self._seed(tmp_path)
        result_with = assemble_context(
            MANIFEST,
            with_sketch,
            ["herbarium.public.collector"],
            AssemblyOptions(format="md"),
            "primary",
        )

        without_sketch = tmp_path / "control"
        without_sketch.mkdir()
        table_dir = without_sketch / "prints" / "primary" / "herbarium" / "public" / "collector"
        table_dir.mkdir(parents=True)
        (without_sketch / "prints" / "primary" / "manifest.yaml").write_text(
            yaml.safe_dump(MANIFEST),
        )
        (table_dir / "ddl.sql").write_text(
            "CREATE TABLE collector (collector_id uuid PRIMARY KEY);\n",
        )
        (table_dir / "statistics.yaml").write_text(yaml.safe_dump(STATS))
        (table_dir / "relationships.yaml").write_text(yaml.safe_dump(RELATIONSHIPS))
        (table_dir / "description.md").write_text("Collector roster. Stable, append-only.\n")
        result_without = assemble_context(
            MANIFEST,
            without_sketch / "prints" / "primary",
            ["herbarium.public.collector"],
            AssemblyOptions(format="md"),
            "primary",
        )

        assert result_with.text == result_without.text

    def test_a_budget_that_drops_relationships_unstripped_survives_once_stripped(
        self,
        tmp_path: Path,
    ) -> None:
        """The sketch payload no longer counts against the relationships block's budget."""

        print_root = self._seed(tmp_path)
        # Large enough to cover ddl + description + stripped statistics + relationships,
        # nowhere near enough to also cover the unstripped sketch payload.
        budget = 400
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(format="json", budget=budget),
            "primary",
        )
        parsed = json.loads(result.text)

        assert "relationships" in parsed
        assert "sketch" not in parsed.get("statistics", {}).get("columns", {}).get(
            "collector_id",
            {},
        )


class TestAssembleStructured:
    """`assemble_structured_context` - the object MCP's get_table_context returns directly."""

    def test_returns_the_seeded_prints_fields_as_objects(self, tmp_path: Path) -> None:
        """The payload MCP hands back directly - no text round-trip to parse."""

        print_root = _seed_print(tmp_path)
        direct = assemble_structured_context(
            MANIFEST,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(format="json"),
        )

        assert (direct["table"], direct["type"], direct["columns_count"], direct["row_count"]) == (
            "herbarium.public.collector",
            "table",
            3,
            100,
        )
        assert direct["statistics"]["columns"]["rank"]["values"][0] == {
            "count": 60,
            "value": "trainee",
        }
        assert direct["statistics"]["columns"]["seed_count"]["range"] == {"max": 99, "min": 18}

    def test_identity_fields_present_even_at_zero_budget(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_structured_context(
            MANIFEST,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(budget=1),
        )
        assert result["table"] == "herbarium.public.collector"
        assert result["row_count"] == 100
        assert "ddl" not in result
        assert result["_truncated"]

    def test_generous_budget_includes_every_section(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_structured_context(
            MANIFEST,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(budget=100_000),
        )
        assert "ddl" in result
        assert "description" in result
        assert "statistics" in result
        assert "relationships" in result
        assert "_truncated" not in result

    def test_no_budget_includes_every_section(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_structured_context(
            MANIFEST,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(),
        )
        assert "ddl" in result
        assert "_truncated" not in result

    def test_include_flags_drop_sections_before_budgeting(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_structured_context(
            MANIFEST,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(include_ddl=False, include_description=False),
        )
        assert "ddl" not in result
        assert "description" not in result
        assert "statistics" in result


ANNOTATIONS: dict[str, object] = {
    "format_version": 1,
    "columns": {
        "rank": {
            "note": "Derived from the collector's field rank.",
        },
    },
}


def _seed_print_with_annotations(tmp_path: Path) -> Path:
    print_root = _seed_print(tmp_path)
    manifest_path = print_root / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["tables"]["herbarium.public.collector"]["artifacts"]["statistics_annotations"] = (
        "statistics.annotations.yaml"
    )
    manifest_path.write_text(yaml.safe_dump(manifest))
    table_dir = print_root / "herbarium" / "public" / "collector"
    (table_dir / "statistics.annotations.yaml").write_text(yaml.safe_dump(ANNOTATIONS))

    return print_root


class TestAnnotations:
    def test_annotated_column_appears_after_description_before_cardinality(
        self,
        tmp_path: Path,
    ) -> None:
        print_root = _seed_print_with_annotations(tmp_path)
        manifest = yaml.safe_load((print_root / "manifest.yaml").read_text())
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "## Annotations" in result.text
        assert "Derived from the collector's field rank." in result.text
        idx_description = result.text.index("## Description")
        idx_annotations = result.text.index("## Annotations")
        idx_cardinality = result.text.index("## Cardinality & key columns")
        assert idx_description < idx_annotations < idx_cardinality

    def test_absent_file_adds_nothing(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )
        assert "## Annotations" not in result.text

    def test_no_annotations_flag_omits_the_section(self, tmp_path: Path) -> None:
        print_root = _seed_print_with_annotations(tmp_path)
        manifest = yaml.safe_load((print_root / "manifest.yaml").read_text())
        opts = AssemblyOptions(include_annotations=False)
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            opts,
            "primary",
        )
        assert "## Annotations" not in result.text

    def test_a_corrupt_annotations_file_costs_only_its_own_section(self, tmp_path: Path) -> None:
        print_root = _seed_print_with_annotations(tmp_path)
        manifest = yaml.safe_load((print_root / "manifest.yaml").read_text())
        table_dir = print_root / "herbarium" / "public" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text("not: [valid, - yaml")
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )
        assert "## Annotations" not in result.text
        assert "## Description" in result.text
        # The parse failure is stated, not silently dropped alongside the missing section.
        assert (
            "Unreadable: statistics_annotations (present on disk, failed to parse)" in result.text
        )

    @staticmethod
    def _seed_annotations(tmp_path: Path, columns: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        print_root = _seed_print(tmp_path)
        manifest_path = print_root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["herbarium.public.collector"]["artifacts"]["statistics_annotations"] = (
            "statistics.annotations.yaml"
        )
        manifest_path.write_text(yaml.safe_dump(manifest))
        table_dir = print_root / "herbarium" / "public" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump({"format_version": 1, "columns": columns}),
        )

        return print_root, manifest

    def test_a_claims_only_entry_still_names_its_column(self, tmp_path: Path) -> None:
        """SPEC 2.7.1: a note-less entry still needs a header naming which column it is."""

        print_root, manifest = self._seed_annotations(
            tmp_path,
            {"rank": {"claims": {"candidate_key": True}}},
        )
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "- **rank**\n" in result.text
        assert "- **rank**:" not in result.text
        assert "claims: candidate_key: true" in result.text

    def test_a_values_only_entry_still_names_its_column(self, tmp_path: Path) -> None:
        print_root, manifest = self._seed_annotations(
            tmp_path,
            {"rank": {"values": [{"value": "field", "note": "the pre-digitization scheme"}]}},
        )
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "- **rank**\n" in result.text
        assert "- **rank**:" not in result.text
        assert "the pre-digitization scheme" in result.text

    def test_a_note_only_entry_still_carries_its_colon(self, tmp_path: Path) -> None:
        print_root, manifest = self._seed_annotations(
            tmp_path,
            {"rank": {"note": "Derived from the collector's field rank."}},
        )
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "- **rank**: Derived from the collector's field rank." in result.text

    def test_a_list_predicate_renders_inline(self, tmp_path: Path) -> None:
        print_root, manifest = self._seed_annotations(
            tmp_path,
            {"rank": {"claims": {"accepted_values": ["a", "b"]}}},
        )
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "claims: accepted_values: [a, b]" in result.text

    def test_a_range_predicate_renders_as_the_grammars_own_shape(self, tmp_path: Path) -> None:
        print_root, manifest = self._seed_annotations(
            tmp_path,
            {"rank": {"claims": {"null_rate": {"max": 0.01}}}},
        )
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "claims: null_rate: {max: 0.01}" in result.text

    def test_two_predicates_render_joined_in_file_order(self, tmp_path: Path) -> None:
        print_root, manifest = self._seed_annotations(
            tmp_path,
            {"rank": {"claims": {"candidate_key": True, "cardinality": 100}}},
        )
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "claims: candidate_key: true, cardinality: 100" in result.text

    def test_a_boolean_value_note_renders_lowercase(self, tmp_path: Path) -> None:
        """Not Python's `True` - the annotation grammar's own spelling of the same value."""

        print_root, manifest = self._seed_annotations(
            tmp_path,
            {"rank": {"values": [{"value": True, "note": "a legacy sentinel"}]}},
        )
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "- true: a legacy sentinel" in result.text
        assert "True" not in result.text

    def test_a_numeric_looking_string_value_note_stays_quoted(self, tmp_path: Path) -> None:
        """Correct YAML, not `repr` - unquoted `0` would read back as an integer."""

        print_root, manifest = self._seed_annotations(
            tmp_path,
            {"rank": {"values": [{"value": "0", "note": "the unranked sentinel"}]}},
        )
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "- '0': the unranked sentinel" in result.text

    def test_a_null_value_note_renders_the_word_null(self, tmp_path: Path) -> None:
        """The same word every redaction and null-value surface in this codebase spells."""

        print_root, manifest = self._seed_annotations(
            tmp_path,
            {"rank": {"values": [{"value": None, "note": "never recorded"}]}},
        )
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "- NULL: never recorded" in result.text

    def test_an_entry_with_nothing_omits_the_whole_section(self, tmp_path: Path) -> None:
        """SPEC 2.7.1 permits an empty entry; it must not earn the section a header."""

        print_root, manifest = self._seed_annotations(tmp_path, {"rank": {}})
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "## Annotations" not in result.text

    def test_grain_key_note_reaches_the_markdown_grain_line(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        manifest_path = print_root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["herbarium.public.collector"]["artifacts"]["statistics_annotations"] = (
            "statistics.annotations.yaml"
        )
        manifest_path.write_text(yaml.safe_dump(manifest))
        table_dir = print_root / "herbarium" / "public" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {
                    "format_version": 1,
                    "columns": {},
                    "grain": {
                        "keys": [{"columns": ["rank"], "note": "business key, not enforced"}],
                    },
                },
            ),
        )
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )
        assert "Grain:" in result.text
        assert "business key, not enforced" in result.text

    def test_grain_key_with_no_note_renders_the_same_grain_line_as_before(
        self,
        tmp_path: Path,
    ) -> None:
        print_root = _seed_print(tmp_path)
        manifest_path = print_root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["herbarium.public.collector"]["artifacts"]["statistics_annotations"] = (
            "statistics.annotations.yaml"
        )
        manifest_path.write_text(yaml.safe_dump(manifest))
        table_dir = print_root / "herbarium" / "public" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {"format_version": 1, "columns": {}, "grain": {"keys": [{"columns": ["rank"]}]}},
            ),
        )
        with_note = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "Grain: (rank) annotated" in with_note.text

    def test_a_stale_key_is_omitted_from_the_rendered_section(self, tmp_path: Path) -> None:
        """A key naming a column not in statistics.yaml does not render (SPEC 2.7.1)."""

        print_root = _seed_print(tmp_path)
        manifest_path = print_root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["herbarium.public.collector"]["artifacts"]["statistics_annotations"] = (
            "statistics.annotations.yaml"
        )
        manifest_path.write_text(yaml.safe_dump(manifest))
        table_dir = print_root / "herbarium" / "public" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {
                    "format_version": 1,
                    "columns": {
                        "rank": {
                            "note": "Derived from the collector's field rank.",
                        },
                        "not_a_real_column": {"note": "stale"},
                    },
                },
            ),
        )
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "Derived from the collector's field rank." in result.text
        assert "not_a_real_column" not in result.text
        assert "stale" not in result.text

    def test_a_wholly_stale_annotations_file_is_absent_from_structured_output(
        self,
        tmp_path: Path,
    ) -> None:
        """Every key stale -> the section drops, matching Markdown (not `{}`)."""

        print_root = _seed_print(tmp_path)
        manifest_path = print_root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["herbarium.public.collector"]["artifacts"]["statistics_annotations"] = (
            "statistics.annotations.yaml"
        )
        manifest_path.write_text(yaml.safe_dump(manifest))
        table_dir = print_root / "herbarium" / "public" / "collector"
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {"format_version": 1, "columns": {"not_a_real_column": {"note": "stale"}}},
            ),
        )
        result = assemble_structured_context(
            manifest,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(),
        )
        assert "annotations" not in result

    def test_structured_json_includes_annotations_between_description_and_statistics(
        self,
        tmp_path: Path,
    ) -> None:
        print_root = _seed_print_with_annotations(tmp_path)
        manifest = yaml.safe_load((print_root / "manifest.yaml").read_text())
        result = assemble_structured_context(
            manifest,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(),
        )
        assert result["annotations"] == {
            "rank": {
                "note": "Derived from the collector's field rank.",
            },
        }
        assert list(result).index("annotations") < list(result).index("statistics")

    def test_a_corrupt_relationships_annotations_file_is_reported(self, tmp_path: Path) -> None:
        """The same distinguishable-corruption fix, for the relationships side."""

        print_root = _seed_print(tmp_path)
        table_dir = print_root / "herbarium" / "public" / "collector"
        manifest_path = print_root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["herbarium.public.collector"]["artifacts"][
            "relationships_annotations"
        ] = "relationships.annotations.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest))
        (table_dir / "relationships.annotations.yaml").write_text("not: [valid, - yaml")

        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert (
            "Unreadable: relationships_annotations (present on disk, failed to parse)"
            in result.text
        )

    def test_a_corrupt_artifact_reaches_the_structured_header(self, tmp_path: Path) -> None:
        """The markdown path already names a corrupt artifact - `--format json`/`yaml` must carry
        the same fact, not drop the section like a table that never declared the artifact.
        """

        print_root = _seed_print(tmp_path)
        table_dir = print_root / "herbarium" / "public" / "collector"
        manifest_path = print_root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["herbarium.public.collector"]["artifacts"][
            "relationships_annotations"
        ] = "relationships.annotations.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest))
        (table_dir / "relationships.annotations.yaml").write_text("not: [valid, - yaml")

        result = assemble_structured_context(
            manifest,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(format="json"),
        )

        assert set(result["_corrupted"]) == {"relationships_annotations"}
        assert result["_corrupted"]["relationships_annotations"].strip()

    def test_a_non_mapping_artifact_reports_why(self, tmp_path: Path) -> None:
        """A YAML-valid file that isn't a mapping is corrupt for a different reason than a
        syntax error - both must carry a reason string in the same `_corrupted` shape.
        """

        print_root = _seed_print(tmp_path)
        table_dir = print_root / "herbarium" / "public" / "collector"
        (table_dir / "statistics.yaml").write_text(yaml.safe_dump(["not", "a", "mapping"]))

        result = assemble_structured_context(
            yaml.safe_load((print_root / "manifest.yaml").read_text()),
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(format="json"),
        )

        assert set(result["_corrupted"]) == {"statistics"}
        assert result["_corrupted"]["statistics"].strip()

    def test_structured_json_includes_relationship_annotations(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        table_dir = print_root / "herbarium" / "public" / "collector"
        rel_path = table_dir / "relationships.yaml"
        relationships = yaml.safe_load(rel_path.read_text())
        relationships["refers_to"] = [
            {
                "column": ["herbarium_id"],
                "target_table": "public.herbarium",
                "target_column": ["id"],
                "detection": "inferred",
            },
        ]
        rel_path.write_text(yaml.safe_dump(relationships))

        manifest_path = print_root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["herbarium.public.collector"]["artifacts"][
            "relationships_annotations"
        ] = "relationships.annotations.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest))
        (table_dir / "relationships.annotations.yaml").write_text(
            yaml.safe_dump(
                {
                    "format_version": 1,
                    "refers_to": [
                        {
                            "column": ["herbarium_id"],
                            "target_table": "public.herbarium",
                            "target_column": ["id"],
                            "verdict": "rejected",
                            "note": "herbarium_id is a display label, not a foreign key",
                        },
                    ],
                },
            ),
        )

        result = assemble_structured_context(
            manifest,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(),
        )

        assert "relationship_annotations" not in result
        assert result["relationships"]["refers_to"] == []


def _seed_print_with_value_note(tmp_path: Path) -> Path:
    print_root = _seed_print(tmp_path)
    manifest_path = print_root / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["tables"]["herbarium.public.collector"]["artifacts"]["statistics_annotations"] = (
        "statistics.annotations.yaml"
    )
    manifest_path.write_text(yaml.safe_dump(manifest))
    table_dir = print_root / "herbarium" / "public" / "collector"
    (table_dir / "statistics.annotations.yaml").write_text(
        yaml.safe_dump(
            {
                "format_version": 1,
                "columns": {
                    "rank": {
                        "values": [{"value": "trainee", "note": "not yet field-certified"}],
                    },
                },
            },
        ),
    )

    return print_root


class TestValueNotes:
    """A value-grain note reaches both rendered surfaces (SPEC 2.7.1)."""

    def test_the_note_appears_beside_the_value_in_markdown(self, tmp_path: Path) -> None:
        print_root = _seed_print_with_value_note(tmp_path)
        manifest = yaml.safe_load((print_root / "manifest.yaml").read_text())
        result = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "not yet field-certified" in result.text

    def test_the_note_appears_in_the_structured_payload(self, tmp_path: Path) -> None:
        print_root = _seed_print_with_value_note(tmp_path)
        manifest = yaml.safe_load((print_root / "manifest.yaml").read_text())
        result = assemble_structured_context(
            manifest,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(),
        )

        assert result["annotations"]["rank"]["values"] == [
            {"value": "trainee", "note": "not yet field-certified"},
        ]


class TestColumnOrdering:
    def test_categorical_before_numeric_before_text(self, tmp_path: Path) -> None:
        """SPEC 3.2 priority: categorical, then numeric, then text (`_COLUMN_ORDER_PRIORITY`)."""

        print_root = _seed_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )
        idx_rank = result.text.index("| rank |")
        idx_seed_count = result.text.index("| seed_count |")
        idx_id = result.text.index("| collector_id |")
        assert idx_rank < idx_seed_count < idx_id


REDACTED_STATS: dict[str, object] = {
    "format_version": 1,
    "table": "herbarium.public.collector",
    "type": "table",
    "profiled_at": "2026-06-09T00:00:00Z",
    "row_count": 100,
    "row_count_method": "exact",
    "columns": {
        "is_active": {
            "sql_type": "boolean",
            "nullable": False,
            "null_count": 0,
            "null_rate": 0.0,
            "cardinality": 2,
            "cardinality_ratio": 0.02,
            "cardinality_method": "exact",
            "classification": "boolean",
            "redacted": "drop",
            "values": [{"count": 70}, {"count": 30}],
            "values_coverage": 1.0,
            "distribution": "imbalanced",
        },
        "status": {
            "sql_type": "varchar(20)",
            "nullable": False,
            "null_count": 0,
            "null_rate": 0.0,
            "cardinality": 3,
            "cardinality_ratio": 0.03,
            "cardinality_method": "exact",
            "classification": "categorical",
            "redacted": "drop",
            "values": [{"count": 60}, {"count": 30}, {"count": 10}],
            "values_coverage": 1.0,
            "distribution": "imbalanced",
        },
        "rank": {
            "sql_type": "varchar(20)",
            "nullable": False,
            "null_count": 0,
            "null_rate": 0.0,
            "cardinality": 3,
            "cardinality_ratio": 0.03,
            "cardinality_method": "exact",
            "classification": "categorical",
            "values": [
                {"value": "trainee", "count": 60},
                {"value": "certified", "count": 30},
                {"value": "senior", "count": 10},
            ],
            "values_coverage": 1.0,
            "distribution": "imbalanced",
        },
    },
}


def _seed_redacted_print(tmp_path: Path) -> Path:
    print_root = tmp_path / "prints" / "primary"
    table_dir = print_root / "herbarium" / "public" / "collector"
    table_dir.mkdir(parents=True)
    (print_root / "manifest.yaml").write_text(yaml.safe_dump(MANIFEST))
    (table_dir / "ddl.sql").write_text("CREATE TABLE collector (collector_id uuid PRIMARY KEY);\n")
    (table_dir / "statistics.yaml").write_text(yaml.safe_dump(REDACTED_STATS))
    (table_dir / "relationships.yaml").write_text(yaml.safe_dump(RELATIONSHIPS))

    return print_root


def _row_for(markdown: str, column: str) -> str:
    return next(line for line in markdown.splitlines() if line.startswith(f"| {column} |"))


class TestARedactedColumnReadsAsRedacted:
    """The assembled table is what an operator reads and what an agent is handed."""

    def test_a_populated_boolean_does_not_report_zero_of_both(self, tmp_path: Path) -> None:
        print_root = _seed_redacted_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )
        row = _row_for(result.text, "is_active")

        assert "0 true / 0 false" not in row
        assert "70" in row and "30" in row

    def test_no_redacted_row_renders_null(self, tmp_path: Path) -> None:
        print_root = _seed_redacted_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        for column in ("is_active", "status"):
            assert "NULL" not in _row_for(result.text, column), column

    def test_the_cardinality_cell_still_reports_the_true_measurement(self, tmp_path: Path) -> None:
        """`cardinality` is untouched by redaction, so the number beside the cell is real."""

        print_root = _seed_redacted_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )

        assert "| status | 3 |" in result.text

    def test_an_unredacted_column_in_the_same_table_is_unchanged(self, tmp_path: Path) -> None:
        """Redaction is per column: one covered column changes one row and nothing else."""

        print_root = _seed_redacted_print(tmp_path)
        result = assemble_context(
            MANIFEST,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )
        row = _row_for(result.text, "rank")

        assert "values (complete): 'trainee' (60%), 'certified' (30%), 'senior' (10%)" in row
        assert "redacted" not in row


def _query(print_root: Path, table: str, manifest: dict[str, Any], **kwargs: Any) -> str:
    return assemble_context(
        manifest,
        print_root,
        [table],
        AssemblyOptions(purpose="query", **kwargs),
        "primary",
    ).text


class TestQueryPurpose:
    """The selection a query writer reads: what the columns mean and what they hold."""

    def test_values_render_with_their_counts(self, tmp_path: Path) -> None:
        text = _query(_seed_print(tmp_path), "herbarium.public.collector", MANIFEST)

        assert "## Column values" in text
        assert _row_for(text, "rank").startswith(
            "| rank | 'trainee' (60), 'certified' (30), 'senior' (10) |",
        )

    def test_an_exhaustive_list_states_the_whole_domain(self, tmp_path: Path) -> None:
        text = _query(_seed_print(tmp_path), "herbarium.public.collector", MANIFEST)

        assert "100% - the list is the whole domain" in _row_for(text, "rank")

    def test_a_truncated_list_states_that_it_is_a_sample(self, tmp_path: Path) -> None:
        text = _query(_seed_print(tmp_path), "herbarium.public.collector", MANIFEST)

        assert "2% - a sample of the most frequent values" in _row_for(text, "collector_id")

    def test_a_column_without_values_has_no_row(self, tmp_path: Path) -> None:
        """`seed_count` is numeric with no value list; an empty cell would read as no values."""

        text = _query(_seed_print(tmp_path), "herbarium.public.collector", MANIFEST)

        assert "| seed_count |" not in text

    def test_the_statistics_and_relationships_are_absent(self, tmp_path: Path) -> None:
        text = _query(_seed_print(tmp_path), "herbarium.public.collector", MANIFEST)

        for absent in ("Cardinality", "## Relationships", "p50", "distribution"):
            assert absent not in text, absent

    def test_the_data_dictionary_carries_the_description_and_the_column_notes(
        self,
        tmp_path: Path,
    ) -> None:
        print_root = _seed_print_with_annotations(tmp_path)
        manifest = yaml.safe_load((print_root / "manifest.yaml").read_text())
        text = _query(print_root, "herbarium.public.collector", manifest)
        dictionary = text.split("## Data dictionary", 1)[-1].split("## Column values", 1)[0]

        assert "Collector roster" in dictionary
        assert "- rank:" in dictionary

    def test_a_value_note_renders_beside_its_value(self, tmp_path: Path) -> None:
        print_root = _seed_print_with_value_note(tmp_path)
        manifest = yaml.safe_load((print_root / "manifest.yaml").read_text())
        text = _query(print_root, "herbarium.public.collector", manifest)

        assert "'trainee' (60) = 'not yet field-certified'" in _row_for(text, "rank")

    def test_a_redacted_column_publishes_counts_and_no_literal(self, tmp_path: Path) -> None:
        """SPEC 2.2.9: the counts are real under redaction; the values are withheld."""

        text = _query(_seed_redacted_print(tmp_path), "herbarium.public.collector", MANIFEST)
        row = _row_for(text, "status")

        assert "redacted: drop; values: withheld (60), withheld (30), withheld (10)" in row

    def test_a_scoped_table_states_the_domain_over_the_rows_scanned(self, tmp_path: Path) -> None:
        """SPEC 2.2.4: under `scope`, an exhaustive list is exhaustive over what was scanned."""

        print_root = _seed_scoped_print(tmp_path, {"rows_scanned": 400_000, "sample": 0.1})
        statistics_path = print_root / "herbarium" / "public" / "field_log" / "statistics.yaml"
        statistics = yaml.safe_load(statistics_path.read_text())
        statistics["columns"]["region"]["values"] = [{"value": "north", "count": 400_000}]
        statistics_path.write_text(yaml.safe_dump(statistics))

        text = _query(print_root, "herbarium.public.field_log", SCOPED_MANIFEST)

        assert "100% - the list is the whole domain over the rows scanned" in _row_for(
            text,
            "region",
        )
        assert "Scanned: 400000 of 4000000 rows (10%)" in text

    def test_identity_rides_a_budget_too_small_for_anything_else(self, tmp_path: Path) -> None:
        text = _query(_seed_print(tmp_path), "herbarium.public.collector", MANIFEST, budget=20)

        assert "# Table: herbarium.public.collector" in text
        assert "## DDL" not in text
        assert "truncated:" in text

    def test_a_budget_keeps_the_ddl_before_the_prose(self, tmp_path: Path) -> None:
        text = _query(_seed_print(tmp_path), "herbarium.public.collector", MANIFEST, budget=40)

        assert "# Table: herbarium.public.collector" in text
        assert "## DDL" in text
        assert "## Data dictionary" not in text
        assert "truncated:" in text

    def test_the_structured_payload_carries_values_and_no_statistics(self, tmp_path: Path) -> None:
        print_root = _seed_print(tmp_path)
        payload = assemble_structured_context(
            MANIFEST,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(format="json", purpose="query"),
        )

        assert payload["values"]["rank"]["entries"][0] == {"value": "trainee", "count": 60}
        assert payload["values"]["rank"]["coverage_statement"] == "the list is the whole domain"
        assert "statistics" not in payload
        assert "relationships" not in payload


def _seed_print_with_a_spelling_group(tmp_path: Path) -> Path:
    """`rank`'s three values become four entries, two of them one category."""

    print_root = _seed_print(tmp_path)
    stats_path = print_root / "herbarium" / "public" / "collector" / "statistics.yaml"
    stats = yaml.safe_load(stats_path.read_text())
    stats["columns"]["rank"]["values"] = [
        {"value": "trainee", "count": 60},
        {"value": "certified", "count": 25},
        {"value": "senior", "count": 10},
        {"value": "Certified", "count": 5, "spelling_of": "certified"},
    ]
    stats["columns"]["rank"]["cardinality"] = 4
    stats_path.write_text(yaml.safe_dump(stats))

    return print_root


class TestSpellingGroupRendering:
    """A group is one category stored twice, so it reads as one row (SPEC 2.2.4)."""

    def test_the_query_value_table_renders_the_group_as_one_row(self, tmp_path: Path) -> None:
        text = _query(
            _seed_print_with_a_spelling_group(tmp_path),
            "herbarium.public.collector",
            MANIFEST,
        )
        row = _row_for(text.split("## Column values", 1)[-1], "rank")

        assert "'certified' (30) {'certified' (25), 'Certified' (5)}" in row
        assert "| Certified |" not in text

    def test_the_notes_summary_marks_the_group(self, tmp_path: Path) -> None:
        result = assemble_context(
            MANIFEST,
            _seed_print_with_a_spelling_group(tmp_path),
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        )
        row = _row_for(result.text, "rank")

        assert "'certified' (30%, 2 spellings)" in row
        assert "'Certified'" not in row

    def test_the_structured_payload_carries_the_link(self, tmp_path: Path) -> None:
        payload = assemble_structured_context(
            MANIFEST,
            _seed_print_with_a_spelling_group(tmp_path),
            "herbarium.public.collector",
            AssemblyOptions(format="json", purpose="query"),
        )
        entries = payload["values"]["rank"]["entries"]

        assert entries[-1] == {"value": "Certified", "count": 5, "spelling_of": "certified"}


class TestTheStructuredQueryPayloadStatesItsPopulation:
    """SPEC 2.2.8: `query` drops `statistics`, which is where `scope` rode on the profile path."""

    def test_a_scoped_table_carries_the_scope_block(self, tmp_path: Path) -> None:
        print_root = _seed_scoped_print(tmp_path, {"rows_scanned": 400_000, "sample": 0.1})

        payload = assemble_structured_context(
            SCOPED_MANIFEST,
            print_root,
            "herbarium.public.field_log",
            AssemblyOptions(format="json", purpose="query"),
        )

        assert payload["scope"] == {"rows_scanned": 400_000, "sample": 0.1}
        assert "statistics" not in payload

    def test_an_unscoped_table_carries_no_scope_key(self, tmp_path: Path) -> None:
        payload = assemble_structured_context(
            MANIFEST,
            _seed_print(tmp_path),
            "herbarium.public.collector",
            AssemblyOptions(format="json", purpose="query"),
        )

        assert "scope" not in payload


def _seed_print_with_a_sampled_column(tmp_path: Path) -> Path:
    """`institution` lists eight of many values, a spelling pair among them, at coverage 0.8."""

    print_root = _seed_print(tmp_path)
    stats_path = print_root / "herbarium" / "public" / "collector" / "statistics.yaml"
    stats = yaml.safe_load(stats_path.read_text())
    stats["columns"]["institution"] = {
        "sql_type": "varchar(80)",
        "nullable": False,
        "null_count": 0,
        "null_rate": 0.0,
        "cardinality": 90,
        "cardinality_ratio": 0.9,
        "cardinality_method": "exact",
        "classification": "text",
        "values": [
            {"value": "Kew", "count": 30},
            {"value": "Leiden", "count": 20},
            {"value": "KEW", "count": 10, "spelling_of": "Kew"},
            {"value": "Geneva", "count": 5},
            {"value": "Meise", "count": 5},
            {"value": "Paris", "count": 5},
            {"value": "Uppsala", "count": 5},
        ],
        "values_coverage": 0.8,
        "distribution": "imbalanced",
    }
    stats["columns"]["seed_count"]["values"] = [{"value": 42, "count": 3}, {"value": 7, "count": 2}]
    stats_path.write_text(yaml.safe_dump(stats))

    return print_root


class TestTheQueryValueTableShowsWhatAPredicateCanUse:
    """An exhaustive list whole, a sampled one cut to its most frequent, an uncovered one absent."""

    def test_a_sampled_list_shows_five_categories_and_their_own_share(self, tmp_path: Path) -> None:
        text = _query(
            _seed_print_with_a_sampled_column(tmp_path),
            "herbarium.public.collector",
            MANIFEST,
        )
        row = _row_for(text.split("## Column values", 1)[-1], "institution")

        assert (
            "'Kew' (40) {'Kew' (30), 'KEW' (10)}, 'Leiden' (20), 'Geneva' (5), 'Meise' (5), "
            "'Paris' (5)"
        ) in row
        assert "Uppsala" not in row
        assert "75% - a sample of the most frequent values" in row

    def test_a_column_with_values_but_no_coverage_has_no_row(self, tmp_path: Path) -> None:
        """A numeric list is a frequency sample with no stated share (SPEC 2.2.3) - not a domain."""

        text = _query(
            _seed_print_with_a_sampled_column(tmp_path),
            "herbarium.public.collector",
            MANIFEST,
        )

        assert "| seed_count |" not in text
        assert "coverage not published" not in text

    def test_a_member_of_an_unlisted_value_stands_as_its_own_row(self, tmp_path: Path) -> None:
        """Dropping it would hide a literal the column holds; the validator reports the entry."""

        print_root = _seed_print(tmp_path)
        stats_path = print_root / "herbarium" / "public" / "collector" / "statistics.yaml"
        stats = yaml.safe_load(stats_path.read_text())
        stats["columns"]["rank"]["values"] = [
            {"value": "trainee", "count": 60},
            {"value": "Senior", "count": 10, "spelling_of": "senior"},
        ]
        stats_path.write_text(yaml.safe_dump(stats))

        row = _row_for(_query(print_root, "herbarium.public.collector", MANIFEST), "rank")

        assert "'trainee' (60), 'Senior' (10)" in row

    def test_the_structured_payload_shows_the_same_five_and_their_share(
        self,
        tmp_path: Path,
    ) -> None:
        payload = assemble_structured_context(
            MANIFEST,
            _seed_print_with_a_sampled_column(tmp_path),
            "herbarium.public.collector",
            AssemblyOptions(format="json", purpose="query"),
        )
        institution = payload["values"]["institution"]

        assert [e["value"] for e in institution["entries"]] == [
            "Kew",
            "KEW",
            "Leiden",
            "Geneva",
            "Meise",
            "Paris",
        ]
        assert institution["coverage"] == 0.8  # noqa: RUF069 - the expected value is an exact literal
        assert institution["shown_coverage"] == 0.75  # noqa: RUF069 - the expected value is an exact literal
        assert "seed_count" not in payload["values"]
        assert "shown_coverage" not in payload["values"]["rank"]


EDGES_OF_EVERY_DETECTION: dict[str, object] = {
    "refers_to": [
        {
            "column": ["seed_count"],
            "target_table": "public.batch",
            "target_column": ["batch_no"],
            "detection": "measured",
            "observed": {"fanout_avg": 3.0, "target_coverage": 0.5},
        },
        {
            "column": ["garden_id"],
            "target_table": "public.garden",
            "target_column": ["garden_code"],
            "detection": "inferred",
        },
        {
            "column": ["site_id", "plot_no"],
            "target_table": "public.field_site",
            "target_column": ["site_id", "plot_no"],
            "detection": "declared",
            "on_delete": "CASCADE",
        },
    ],
    "referenced_by": [
        {
            "column": ["collector_id"],
            "referencer_table": "public.accession",
            "referencer_column": ["collector_id"],
            "detection": "inferred",
        },
    ],
}


class TestTheQueryJoinsList:
    """Every unrejected edge, the surest first, and nothing measured about the edge itself."""

    def test_edges_render_declared_first_with_their_detection(self) -> None:
        from dbprint.engine.context_assembler import _markdown_joins

        lines = _markdown_joins(TestRelationshipsMarkdown._artifacts(EDGES_OF_EVERY_DETECTION))

        assert [line.text for line in lines] == [
            "## Joins",
            "- (site_id, plot_no) -> public.field_site.(site_id, plot_no) (declared)",
            "- garden_id -> public.garden.garden_code (inferred)",
            "- seed_count -> public.batch.batch_no (measured)",
            "- collector_id <- public.accession.collector_id (inferred)",
        ]

    def test_a_table_with_no_edge_says_none_found(self) -> None:
        from dbprint.engine.context_assembler import _markdown_joins

        lines = _markdown_joins(TestRelationshipsMarkdown._artifacts({"eligible_target": True}))

        assert [line.text for line in lines] == ["## Joins", "none found"]

    def test_an_ineligible_target_with_no_edge_gives_the_reason(self) -> None:
        from dbprint.engine.context_assembler import _markdown_joins

        lines = _markdown_joins(TestRelationshipsMarkdown._artifacts({"eligible_target": False}))

        assert [line.text for line in lines] == [
            "## Joins",
            "none found (not a join target, no declared-unique column)",
        ]

    def test_a_seeded_table_with_no_edge_renders_the_section(self, tmp_path: Path) -> None:
        text = _query(_seed_print(tmp_path), "herbarium.public.collector", MANIFEST)

        assert "## Joins\nnone found" in text

    def test_unread_relationships_render_no_section_and_no_structured_key(self) -> None:
        from dbprint.engine.context_assembler import _markdown_joins, _query_candidates

        artifacts = TestRelationshipsMarkdown._artifacts({})
        artifacts.relationships = None

        assert _markdown_joins(artifacts) == []
        assert "joins" not in dict(_query_candidates(artifacts, AssemblyOptions(purpose="query")))

    def test_the_structured_payload_of_a_table_with_no_edge_has_both_lists_empty(self) -> None:
        from dbprint.engine.context_assembler import _query_candidates

        artifacts = TestRelationshipsMarkdown._artifacts({"eligible_target": False})
        candidates = dict(_query_candidates(artifacts, AssemblyOptions(purpose="query")))

        assert candidates["joins"] == {"refers_to": [], "referenced_by": []}

    def test_the_structured_payload_carries_each_edge_and_its_detection(self) -> None:
        from dbprint.engine.context_assembler import _structured_joins

        joins = _structured_joins(TestRelationshipsMarkdown._artifacts(EDGES_OF_EVERY_DETECTION))

        assert joins["refers_to"][0] == {
            "column": ["site_id", "plot_no"],
            "target_table": "public.field_site",
            "target_column": ["site_id", "plot_no"],
            "detection": "declared",
        }
        assert joins["refers_to"][1] == {
            "column": ["garden_id"],
            "target_table": "public.garden",
            "target_column": ["garden_code"],
            "detection": "inferred",
        }
        assert "observed" not in joins["refers_to"][2]
        assert joins["referenced_by"] == [
            {
                "column": ["collector_id"],
                "referencer_table": "public.accession",
                "referencer_column": ["collector_id"],
                "detection": "inferred",
            },
        ]


def _nullable(null_count: int, null_rate: float, **extra: Any) -> dict[str, Any]:
    return {"nullable": True, "null_count": null_count, "null_rate": null_rate, **extra}


def _listed(*values: str) -> dict[str, Any]:
    return {"values": [{"value": v, "count": 10} for v in values], "values_coverage": 1.0}


NULL_SHARE_COLUMNS: dict[str, Any] = {
    "id": {"nullable": False, "null_count": 0, "null_rate": 0.0},
    "garden_id": _nullable(8, 0.004),
    "rank": _nullable(0, 0.0, **_listed("genus", "species")),
    "condition": _nullable(100, 0.05, **_listed("dry", "moist")),
    "viability": {"nullable": False, "null_count": 0, "null_rate": 0.0, **_listed("high", "low")},
    "site_id": _nullable(400, 0.2),
    "plot_no": _nullable(0, 0.0),
    "withdrawn_at": _nullable(1000, 0.5),
}


def _null_share_artifacts(
    relationships: dict[str, Any] = EDGES_OF_EVERY_DETECTION,
    scope: dict[str, Any] | None = None,
) -> TableArtifacts:
    a = TestRelationshipsMarkdown._artifacts(relationships)
    a.statistics = {"row_count": 2000, "columns": NULL_SHARE_COLUMNS}

    if scope:
        a.statistics["scope"] = scope

    return a


class TestTheQueryPurposeStatesEachNullShareOnce:
    """A nullable column's share sits on its value row, its Joins line, or the `Nulls:` line."""

    def test_a_lone_referencing_column_states_its_share_on_its_joins_line(self) -> None:
        from dbprint.engine.context_assembler import _markdown_joins

        lines = [line.text for line in _markdown_joins(_null_share_artifacts())]

        assert "- garden_id -> public.garden.garden_code (inferred); nulls: 0.4%" in lines
        assert "- (site_id, plot_no) -> public.field_site.(site_id, plot_no) (declared)" in lines
        assert "- collector_id <- public.accession.collector_id (inferred)" in lines

    def test_a_value_row_carries_its_share_and_a_not_null_one_an_empty_cell(self) -> None:
        from dbprint.engine.context_assembler import _markdown_column_values

        lines = [
            line.text for line in _markdown_column_values(_null_share_artifacts(), {"garden_id"})
        ]

        assert "| Column | Values (count) | Coverage | Nulls |" in lines
        assert _row_for("\n".join(lines), "rank").endswith("| none |")
        assert _row_for("\n".join(lines), "condition").endswith("| 5% |")
        assert _row_for("\n".join(lines), "viability").endswith("|  |")

    def test_every_other_nullable_column_lands_on_the_nulls_line(self) -> None:
        from dbprint.engine.context_assembler import _markdown_column_values

        lines = [
            line.text for line in _markdown_column_values(_null_share_artifacts(), {"garden_id"})
        ]

        assert lines[-1] == "Nulls: site_id (20%), plot_no (none), withdrawn_at (50%)"

    def test_with_no_joins_section_the_referencing_column_moves_to_the_nulls_line(self) -> None:
        from dbprint.engine.context_assembler import _query_sections

        sections = _query_sections(
            _null_share_artifacts(),
            AssemblyOptions(purpose="query", include_relationships=False),
            None,
        )
        values = next(s for s in sections if s.name == "values")

        assert values.text.splitlines()[-1].startswith("Nulls: garden_id (0.4%), site_id (20%)")

    def test_a_scoped_table_qualifies_none(self) -> None:
        from dbprint.engine.context_assembler import _markdown_column_values

        artifacts = _null_share_artifacts(scope={"rows_scanned": 1000, "sample": 0.5})
        lines = [line.text for line in _markdown_column_values(artifacts, set())]

        assert _row_for("\n".join(lines), "rank").endswith("| none over the rows scanned |")
        assert "plot_no (none over the rows scanned)" in lines[-1]

    def test_without_value_rows_the_section_holds_the_nulls_line_alone(self) -> None:
        from dbprint.engine.context_assembler import _markdown_column_values

        artifacts = _null_share_artifacts({})
        artifacts.statistics = {"row_count": 10, "columns": {"plot_no": _nullable(1, 0.1)}}

        assert [line.text for line in _markdown_column_values(artifacts, set())] == [
            "## Column values",
            "",
            "Nulls: plot_no (10%)",
        ]

    def test_a_table_with_no_nullable_column_states_no_share(self) -> None:
        from dbprint.engine.context_assembler import _markdown_column_values

        artifacts = _null_share_artifacts({})
        artifacts.statistics = {
            "row_count": 10,
            "columns": {
                "viability": NULL_SHARE_COLUMNS["viability"],
                "id": NULL_SHARE_COLUMNS["id"],
            },
        }
        lines = [line.text for line in _markdown_column_values(artifacts, set())]

        assert not any(line.startswith("Nulls:") for line in lines)
        assert _row_for("\n".join(lines), "viability").endswith("|  |")

    def test_the_structured_payload_carries_the_raw_rates_by_the_same_rules(self) -> None:
        from dbprint.engine.context_assembler import _query_candidates

        candidates = dict(
            _query_candidates(_null_share_artifacts(), AssemblyOptions(purpose="query")),
        )
        garden = next(e for e in candidates["joins"]["refers_to"] if e["column"] == ["garden_id"])
        composite = candidates["joins"]["refers_to"][0]

        assert candidates["values"]["rank"]["null_rate"] == 0.0  # noqa: RUF069 - the expected value is an exact literal
        assert candidates["values"]["condition"]["null_rate"] == 0.05  # noqa: RUF069 - the expected value is an exact literal
        assert "null_rate" not in candidates["values"]["viability"]
        assert garden["null_rate"] == 0.004  # noqa: RUF069 - the expected value is an exact literal
        assert "null_rate" not in composite
        assert candidates["nulls"] == {"site_id": 0.2, "plot_no": 0.0, "withdrawn_at": 0.5}


def _seed_print_with_edges(tmp_path: Path) -> Path:
    print_root = _seed_print_with_annotations(tmp_path)
    table_dir = print_root / "herbarium" / "public" / "collector"
    (table_dir / "relationships.yaml").write_text(
        yaml.safe_dump({**RELATIONSHIPS, **EDGES_OF_EVERY_DETECTION}),
    )

    return print_root


class TestTheSectionFlagsNarrowTheQuerySelection:
    """A flag whose section exists under `query` is honoured; `include_stats` drops nothing."""

    def test_each_flag_drops_its_section(self, tmp_path: Path) -> None:
        print_root = _seed_print_with_edges(tmp_path)
        manifest = yaml.safe_load((print_root / "manifest.yaml").read_text())
        table = "herbarium.public.collector"
        everything = _query(print_root, table, manifest)

        for marker in ("## DDL", "## Joins", "Collector roster", "- rank:", "## Column values"):
            assert marker in everything, marker

        assert "## DDL" not in _query(print_root, table, manifest, include_ddl=False)
        assert "## Joins" not in _query(print_root, table, manifest, include_relationships=False)
        assert "Collector roster" not in _query(
            print_root,
            table,
            manifest,
            include_description=False,
        )
        assert "- rank:" not in _query(print_root, table, manifest, include_annotations=False)
        assert "## Column values" in _query(print_root, table, manifest, include_stats=False)

    def test_the_structured_payload_honours_the_same_flags(self, tmp_path: Path) -> None:
        print_root = _seed_print_with_edges(tmp_path)
        manifest = yaml.safe_load((print_root / "manifest.yaml").read_text())
        payload = assemble_structured_context(
            manifest,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(
                format="json",
                purpose="query",
                include_ddl=False,
                include_relationships=False,
                include_description=False,
                include_annotations=False,
            ),
        )

        assert "values" in payload
        for absent in ("ddl", "joins", "description", "dictionary"):
            assert absent not in payload, absent


class TestDatabaseContentCannotReshapeTheTableQuotingIt:
    """Every cell a row interpolates is escaped where the row is assembled (SPEC 2.2.3)."""

    def test_a_pipe_is_escaped(self) -> None:
        assert _escape_cell("fair|poor") == "fair\\|poor"

    def test_the_backslash_is_escaped_first(self) -> None:
        """Escaping the pipe first leaves a live delimiter behind an escaped backslash."""

        assert _escape_cell("a\\|b") == "a\\\\\\|b"

    def test_a_line_break_becomes_one_space(self) -> None:
        assert _escape_cell("sound\nbut small") == "sound but small"
        assert _escape_cell("sound\r\nbut small") == "sound but small"

    def test_a_value_carrying_neither_is_untouched(self) -> None:
        assert _escape_cell("sound  but   small") == "sound  but   small"

    def test_a_pipe_in_a_column_name_does_not_split_the_null_pattern_row(self) -> None:
        artifacts = _artifacts_with_null_patterns(["a|b", "plain"])
        lines = [line.text for line in _markdown_null_patterns(artifacts)]
        rows = [l for l in lines if l.startswith("| ")]
        body_rows = [r for r in rows if not set(r) <= set("|- ")]

        assert body_rows
        assert all(len(_unescaped_cells(row)) == 2 for row in body_rows)

    def test_a_pipe_in_a_type_name_does_not_split_the_catalog_only_row(self) -> None:
        """A MySQL `enum('a|b','c')` reaches the catalog-only table as its own sql_type."""

        artifacts = _artifacts_catalog_only("enum('a|b','c')")
        rows = [
            l
            for l in _markdown_catalog_only_columns(artifacts).splitlines()
            if l.startswith("| ") and not set(l) <= set("|- ")
        ]

        assert rows
        assert all(len(_unescaped_cells(row)) == 3 for row in rows)


class TestLostTableBlocks:
    """SPEC 2.2.1: a block the file names `unmeasured` is unknown, and the context says so."""

    def test_a_lost_layout_read_is_named_as_lost(self) -> None:
        text = self._render({"row_count": 10, "columns": {}, "unmeasured": ["physical_layout"]})

        assert "## Blocks in the file's `unmeasured` list" in text
        assert "`physical_layout` (clustering or partitioning)" in text

    def test_a_file_that_lost_nothing_renders_no_such_section(self) -> None:
        assert "`unmeasured` list" not in self._render({"row_count": 10, "columns": {}})

    @staticmethod
    def _render(statistics: dict[str, Any]) -> str:
        return _render_table_markdown(
            _bare_artifacts(statistics),
            AssemblyOptions(),
            None,
            {},
            None,
            with_legend=True,
        ).text


def _unescaped_cells(row: str) -> list[str]:
    """The cells a Markdown reader sees: an escaped pipe does not end one."""

    return [c for c in re.split(r"(?<!\\)\|", row.strip()) if c.strip()]


def _artifacts_with_null_patterns(columns: list[str]) -> TableArtifacts:
    return _bare_artifacts(
        {
            "row_count": 10,
            "columns": {},
            "null_patterns": {"patterns": [{"columns": columns, "count": 4}], "coverage": 1.0},
        },
    )


def _artifacts_catalog_only(sql_type: str) -> TableArtifacts:
    return _bare_artifacts(
        {
            "catalog_only": True,
            "columns": {"condition": {"sql_type": sql_type, "classification": "unsupported"}},
        },
    )


def _bare_artifacts(statistics: dict[str, Any]) -> TableArtifacts:
    return TableArtifacts(
        fqn="herbarium.public.curation_event",
        table_type="table",
        row_count=10,
        column_count=1,
        ddl="",
        statistics=statistics,
        relationships=None,
        description=None,
        annotations=None,
        annotated_grain=None,
        relationship_annotations=None,
        missing=(),
        corrupted={},
        statistics_params_override=None,
    )


def _seed_rejected_edge_into_collector(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """`accession` rejects its inferred edge into `collector`; only `accession` authors that."""

    print_root = _seed_print(tmp_path)
    manifest: dict[str, Any] = copy.deepcopy(MANIFEST)
    collector_dir = print_root / "herbarium" / "public" / "collector"
    (collector_dir / "relationships.yaml").write_text(
        yaml.safe_dump(
            {
                "format_version": 1,
                "table": "herbarium.public.collector",
                "referenced_by": [
                    {
                        "column": ["collector_id"],
                        "referencer_table": "herbarium.public.accession",
                        "referencer_column": ["collector_id"],
                        "detection": "inferred",
                    },
                ],
            },
        ),
    )
    accession_dir = print_root / "herbarium" / "public" / "accession"
    accession_dir.mkdir(parents=True)
    (accession_dir / "ddl.sql").write_text("CREATE TABLE accession (collector_id uuid);\n")
    (accession_dir / "relationships.annotations.yaml").write_text(
        yaml.safe_dump(
            {
                "format_version": 1,
                "refers_to": [
                    {
                        "column": ["collector_id"],
                        "target_table": "herbarium.public.collector",
                        "target_column": ["collector_id"],
                        "verdict": "rejected",
                        "note": "a batch number, not a collector",
                    },
                ],
            },
        ),
    )
    manifest["tables"]["herbarium.public.accession"] = {
        "type": "table",
        "path": "herbarium/public/accession",
        "artifacts": {
            "ddl": "ddl.sql",
            "relationships_annotations": "relationships.annotations.yaml",
        },
        "columns": 1,
        "profiled_at": "2026-06-09T00:00:00Z",
    }

    return print_root, manifest


class TestARejectedEdgeIsWithheld:
    """A rejected edge reaches no agent surface, from either side (SPEC 2.7.2)."""

    def test_the_referencing_table_drops_its_own_rejected_edge_and_names_no_fk(
        self,
        tmp_path: Path,
    ) -> None:
        print_root, manifest = _seed_rejected_edge_into_collector(tmp_path)
        accession_dir = print_root / "herbarium" / "public" / "accession"
        (accession_dir / "relationships.yaml").write_text(
            yaml.safe_dump(
                {
                    "format_version": 1,
                    "table": "herbarium.public.accession",
                    "refers_to": [
                        {
                            "column": ["collector_id"],
                            "target_table": "herbarium.public.collector",
                            "target_column": ["collector_id"],
                            "detection": "inferred",
                        },
                    ],
                },
            ),
        )
        (accession_dir / "statistics.yaml").write_text(
            yaml.safe_dump(
                {
                    "format_version": 1,
                    "table": "herbarium.public.accession",
                    "row_count": 40,
                    "columns": {
                        "collector_id": {
                            "sql_type": "uuid",
                            "nullable": False,
                            "null_count": 0,
                            "null_rate": 0.0,
                            "cardinality": 40,
                            "cardinality_ratio": 1.0,
                            "classification": "foreign_key_candidate",
                        },
                    },
                },
            ),
        )
        manifest["tables"]["herbarium.public.accession"]["artifacts"].update(
            {"relationships": "relationships.yaml", "statistics": "statistics.yaml"},
        )
        text = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.accession"],
            AssemblyOptions(),
            "primary",
        ).text

        assert "herbarium.public.collector" not in text
        assert "REJECTED" not in text
        assert "FK candidate" in _row_for(text, "collector_id")

        (accession_dir / "relationships.annotations.yaml").write_text("format_version: 1\n")
        unrejected = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.accession"],
            AssemblyOptions(),
            "primary",
        ).text

        assert "FK: herbarium.public.collector.collector_id" in _row_for(unrejected, "collector_id")

    def test_the_referenced_table_drops_the_edge_its_referencer_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        print_root, manifest = _seed_rejected_edge_into_collector(tmp_path)
        text = assemble_context(
            manifest,
            print_root,
            ["herbarium.public.collector"],
            AssemblyOptions(),
            "primary",
        ).text

        assert "herbarium.public.accession" not in text
        assert "REJECTED" not in text
        assert "## Relationships\n- (none)" in text

    def test_the_query_joins_drop_the_incoming_edge(self, tmp_path: Path) -> None:
        print_root, manifest = _seed_rejected_edge_into_collector(tmp_path)
        text = _query(print_root, "herbarium.public.collector", manifest)

        assert "herbarium.public.accession" not in text
        assert "## Joins\nnone found" in text

    def test_the_structured_joins_drop_the_incoming_edge(self, tmp_path: Path) -> None:
        print_root, manifest = _seed_rejected_edge_into_collector(tmp_path)
        payload = assemble_structured_context(
            manifest,
            print_root,
            "herbarium.public.collector",
            AssemblyOptions(purpose="query"),
        )

        assert payload["joins"] == {"refers_to": [], "referenced_by": []}

    def test_an_edge_the_referencer_did_not_reject_is_kept(self, tmp_path: Path) -> None:
        print_root, manifest = _seed_rejected_edge_into_collector(tmp_path)
        (print_root / "herbarium/public/accession/relationships.annotations.yaml").write_text(
            "format_version: 1\nrefers_to: []\n",
        )
        text = _query(print_root, "herbarium.public.collector", manifest)

        assert "- collector_id <- herbarium.public.accession.collector_id (inferred)" in text

    def test_a_verdict_on_a_declared_edge_withholds_nothing(self, tmp_path: Path) -> None:
        print_root, manifest = _seed_rejected_edge_into_collector(tmp_path)
        collector = print_root / "herbarium/public/collector/relationships.yaml"
        data = yaml.safe_load(collector.read_text())
        data["referenced_by"][0]["detection"] = "declared"
        collector.write_text(yaml.safe_dump(data))
        text = _query(print_root, "herbarium.public.collector", manifest)

        assert "- collector_id <- herbarium.public.accession.collector_id (declared)" in text

    def test_the_rejected_edges_verdict_leaves_and_every_other_entry_stays(
        self,
        tmp_path: Path,
    ) -> None:
        print_root, manifest = _seed_rejected_edge_into_collector(tmp_path)
        accession = print_root / "herbarium/public/accession"
        manifest["tables"]["herbarium.public.accession"]["artifacts"]["relationships"] = (
            "relationships.yaml"
        )
        (accession / "relationships.yaml").write_text(
            yaml.safe_dump(
                {
                    "format_version": 1,
                    "table": "herbarium.public.accession",
                    "refers_to": [
                        {
                            "column": ["collector_id"],
                            "target_table": "herbarium.public.collector",
                            "target_column": ["collector_id"],
                            "detection": "inferred",
                        },
                    ],
                },
            ),
        )
        annotations = accession / "relationships.annotations.yaml"
        data = yaml.safe_load(annotations.read_text())
        data["refers_to"].append(
            {"column": ["vault_id"], "target_table": "herbarium.public.vault", "note": "kept"},
        )
        annotations.write_text(yaml.safe_dump(data))
        payload = assemble_structured_context(
            manifest,
            print_root,
            "herbarium.public.accession",
            AssemblyOptions(),
        )

        assert payload["relationships"]["refers_to"] == []
        assert payload["relationship_annotations"] == [
            {"column": ["vault_id"], "target_table": "herbarium.public.vault", "note": "kept"},
        ]


def test_a_manifest_whose_statistics_params_is_not_a_mapping_still_renders(tmp_path: Path) -> None:
    print_root = _seed_print(tmp_path)
    manifest: dict[str, Any] = {**copy.deepcopy(MANIFEST), "statistics_params": "default"}

    text = assemble_context(
        manifest,
        print_root,
        ["herbarium.public.collector"],
        AssemblyOptions(),
        "primary",
    ).text

    assert "# Table: herbarium.public.collector" in text
