"""Unit tests for the dbprint diff renderers (text + structured)."""

from __future__ import annotations

import json
from io import StringIO
from typing import Any

import yaml

from dbprint.cli.rendering.diff_data import (
    DiffRenderOptions,
    render_data,
    render_human_text,
)


def _options(threshold_override: float | None = None) -> DiffRenderOptions:
    return DiffRenderOptions(
        thresholds={
            "cardinality_ratio": 0.02,
            "percentile_pct": 0.05,
            "values_coverage": 0.05,
            "default": 0.01,
        },
        threshold_override=threshold_override,
    )


def _empty_diff() -> dict[str, Any]:
    return {
        "format_version": 1,
        "connection": "primary",
        "adapter": "postgres",
        "changes": [],
        "summary": {},
    }


def _diff_with(changes: list[dict[str, Any]]) -> dict[str, Any]:
    base = _empty_diff()
    base["changes"] = changes

    return base


class TestEmptyDiff:
    def test_all_sections_show_none(self) -> None:
        text = render_human_text(_empty_diff(), _options())

        for label in (
            "Modified (DDL)",
            "Modified (row count)",
            "Modified (grain)",
            "Modified (physical layout)",
            "Modified (statistics)",
            "Modified (relationships)",
            "Modified (indexes)",
            "Added",
            "Removed",
        ):
            assert label in text

        # Every section renders the (none) marker on empty diff.
        assert text.count("(none)") == 9

    def test_footer_emitted(self) -> None:
        text = render_human_text(_empty_diff(), _options())
        assert "Run `dbprint generate` to refresh" in text


class TestHeadline:
    """SPEC 2.6.4: what was compared, and the artifact's own totals, before the sections."""

    def test_appears_before_the_event_sections(self) -> None:
        text = render_human_text(_empty_diff(), _options())
        assert text.index("Summary:") < text.index("Modified (DDL):")

    def test_non_zero_counters_render_in_fixed_order(self) -> None:
        diff = {
            **_empty_diff(),
            "summary": {
                "tables_added": 0,
                "columns_added": 3,
                "statistics_drifted": 5,
                "unchanged_tables": 10,
            },
        }
        line = render_human_text(diff, _options()).splitlines()[5]
        assert line == "Summary: columns_added=3, statistics_drifted=5, unchanged_tables=10"

    def test_a_clean_diff_states_so_instead_of_an_empty_line(self) -> None:
        diff = {**_empty_diff(), "summary": {"unchanged_tables": 0}}
        text = render_human_text(diff, _options())
        assert "Summary: no changes" in text

    def test_the_summary_is_read_from_the_artifact_not_recomputed_from_rendered_events(
        self,
    ) -> None:
        """A threshold-elided event still counts in the headline (SPEC 2.6.9's own split)."""

        diff = {
            **_diff_with(
                [
                    {
                        "kind": "statistic_changed",
                        "table": "t",
                        "column": "x",
                        "stat": "cardinality_ratio",
                        "before": 1.0,
                        "after": 1.0001,
                        "delta": 0.0001,
                        "delta_pct": 0.0001,
                    },
                ],
            ),
            "summary": {"statistics_drifted": 5},
        }
        text = render_human_text(diff, _options())
        assert "statistics_drifted=5" in text
        assert "(1 change elided below threshold)" in text

    def test_baseline_states_path_generation_and_version(self) -> None:
        diff = {
            **_empty_diff(),
            "baseline": {
                "path": "prints/primary",
                "generated_at": "2026-01-01T00:00:00Z",
                "dbprint_version": "0.4.2",
            },
        }
        line = render_human_text(diff, _options()).splitlines()[3]
        assert line == "Baseline: prints/primary, generated 2026-01-01T00:00:00Z, dbprint 0.4.2"

    def test_target_states_scan_time_and_table_count(self) -> None:
        diff = {
            **_empty_diff(),
            "target": {"scanned_at": "2026-08-26T12:00:00Z", "tables_scanned": 12},
        }
        line = render_human_text(diff, _options()).splitlines()[4]
        assert line == "Target: live database, scanned 2026-08-26T12:00:00Z, 12 tables scanned"

    def test_a_narrowed_comparison_states_its_selectors(self) -> None:
        diff = {
            **_empty_diff(),
            "target": {"selectors": {"include": ["public.*"], "exclude": ["public.tmp_*"]}},
        }
        text = render_human_text(diff, _options())
        assert "selectors include=['public.*'] exclude=['public.tmp_*']" in text

    def test_an_unnarrowed_comparison_states_no_selectors(self) -> None:
        diff = {**_empty_diff(), "target": {"selectors": {"include": [], "exclude": []}}}
        text = render_human_text(diff, _options())
        assert "selectors" not in text


class TestDdlSection:
    def test_column_added_event_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "column_added",
                    "table": "public.curator",
                    "column": "email",
                    "sql_type": "varchar",
                    "nullable": True,
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "public.curator" in text
        assert "+ email" in text
        assert "varchar" in text

    def test_column_type_changed_shows_before_and_after(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "column_type_changed",
                    "table": "public.curator",
                    "column": "seed_count",
                    "before": "int",
                    "after": "bigint",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "~ seed_count" in text
        assert "'int'" in text
        assert "'bigint'" in text


class TestRowCountSection:
    def test_row_count_change_rendered_with_delta(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "table_row_count_changed",
                    "table": "public.curator",
                    "before": 100,
                    "after": 120,
                    "delta": 20,
                    "before_method": "exact",
                    "after_method": "exact",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "public.curator" in text
        assert "~ row_count: 100 -> 120 (+20)" in text
        assert "(approximate)" not in text

    def test_approximate_method_annotated(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "table_row_count_changed",
                    "table": "public.curator",
                    "before": 1_000_000,
                    "after": 1_050_000,
                    "delta": 50_000,
                    "before_method": "approximate",
                    "after_method": "approximate",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "(approximate)" in text

    def test_shrinking_shows_negative_delta(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "table_row_count_changed",
                    "table": "public.curator",
                    "before": 120,
                    "after": 100,
                    "delta": -20,
                    "before_method": "exact",
                    "after_method": "exact",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "(-20)" in text


class TestGrainSection:
    def test_a_declared_key_gained_is_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "grain_changed",
                    "table": "public.curator",
                    "before": {"keys": []},
                    "after": {"keys": [{"columns": ["id"], "detection": "declared"}]},
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "public.curator" in text
        assert "~ grain: none -> (id) declared" in text

    def test_search_exhausted_is_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "grain_changed",
                    "table": "public.curator",
                    "before": {"keys": [], "search": {"exhausted": False}},
                    "after": {"keys": [], "search": {"exhausted": True}},
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "[search exhausted=False]" in text
        assert "[search exhausted=True]" in text


class TestPhysicalLayoutSection:
    def test_a_genuine_gain_is_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "physical_layout_changed",
                    "table": "public.curation_event",
                    "before": None,
                    "after": {
                        "mechanism": "cluster",
                        "keys": [{"expression": "logged_at::date"}],
                    },
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "public.curation_event" in text
        assert "~ physical_layout: none -> cluster (logged_at::date)" in text


class TestStatisticsThreshold:
    def test_sub_threshold_filtered_in_human(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "id",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 1.005,
                    "delta": 0.005,
                    "delta_pct": 0.005,  # below 0.02 threshold for cardinality_ratio
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "Modified (statistics):\n  (none)" in text

    def test_above_threshold_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "id",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 0.95,
                    "delta": -0.05,
                    "delta_pct": -0.05,  # above 0.02 threshold
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "Modified (statistics):" in text
        assert "id cardinality_ratio" in text

    def test_a_configured_threshold_decides_the_line(self) -> None:
        """The renderer judges by the dict it is handed, not by the spec defaults."""

        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "id",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 0.9,
                    "delta": -0.1,
                    "delta_pct": -0.1,  # between the two thresholds below
                },
            ],
        )
        coarse = DiffRenderOptions(thresholds={"cardinality_ratio": 0.5, "default": 0.01})
        fine = DiffRenderOptions(thresholds={"cardinality_ratio": 0.05, "default": 0.01})

        assert "id cardinality_ratio" not in render_human_text(diff, coarse)
        assert "id cardinality_ratio" in render_human_text(diff, fine)

    def test_override_zero_shows_every_event(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "id",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 1.0001,
                    "delta": 0.0001,
                    "delta_pct": 0.0001,
                },
            ],
        )
        text = render_human_text(diff, _options(threshold_override=0.0))
        assert "id cardinality_ratio" in text

    def test_sub_threshold_elision_is_disclosed(self) -> None:
        """A table whose only change is sub-threshold must not silently vanish."""

        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "id",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 1.005,
                    "delta": 0.005,
                    "delta_pct": 0.005,  # below 0.02 threshold for cardinality_ratio
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "1 change elided below threshold" in text

    def test_no_elision_note_when_nothing_was_filtered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "id",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 0.95,
                    "delta": -0.05,
                    "delta_pct": -0.05,
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "elided" not in text

    def test_event_without_delta_pct_always_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "status",
                    "stat": "classification",
                    "before": "categorical",
                    "after": "text",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "status classification" in text


class TestRelationshipsDualDirection:
    def test_added_renders_under_both_source_and_target(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "relationship_added",
                    "source_table": "public.curator",
                    "source_column": ["herbarium_id"],
                    "target_table": "public.herbarium",
                    "target_column": ["id"],
                    "on_delete": "CASCADE",
                    "on_update": "NO ACTION",
                    "detection": "declared",
                },
            ],
        )
        text = render_human_text(diff, _options())
        # Under source: outgoing -> target
        assert "+ -> public.herbarium.id" in text
        # Under target: incoming <- source
        assert "+ <- public.curator.herbarium_id" in text

    def test_modified_includes_on_delete_change(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "relationship_modified",
                    "source_table": "public.curator",
                    "source_column": ["herbarium_id"],
                    "target_table": "public.herbarium",
                    "target_column": ["id"],
                    "on_delete": {"before": "NO ACTION", "after": "CASCADE"},
                    "on_update": "NO ACTION",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "on_delete NO ACTION -> CASCADE" in text


class TestIndexesSection:
    def test_index_added(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "index_added",
                    "table": "public.curator",
                    "index_name": "curator_email_idx",
                    "columns": ["email"],
                    "unique": True,
                    "type": "btree",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "+ curator_email_idx" in text
        assert "(email)" in text
        assert "unique=True" in text
        assert "type=btree" in text

    def test_index_modified_states_a_uniqueness_flip(self) -> None:
        """A unique index becoming non-unique must not render as an identity change."""

        diff = _diff_with(
            [
                {
                    "kind": "index_modified",
                    "table": "public.curator",
                    "index_name": "curator_email_idx",
                    "before": {"columns": ["email"], "unique": True, "type": "btree"},
                    "after": {"columns": ["email"], "unique": False, "type": "btree"},
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "unique True -> False" in text

    def test_index_modified_states_a_type_change(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "index_modified",
                    "table": "public.curator",
                    "index_name": "curator_email_idx",
                    "before": {"columns": ["email"], "unique": True, "type": "btree"},
                    "after": {"columns": ["email"], "unique": True, "type": "hash"},
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "type btree -> hash" in text

    def test_index_modified_columns_only_carries_no_stray_detail(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "index_modified",
                    "table": "public.curator",
                    "index_name": "curator_email_idx",
                    "before": {"columns": ["email"], "unique": True, "type": "btree"},
                    "after": {"columns": ["email", "id"], "unique": True, "type": "btree"},
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "unique" not in text.split("Modified (indexes):")[1].split("Added:")[0]


class TestAddedRemovedSections:
    def test_table_added_lists_fqn_and_type(self) -> None:
        diff = _diff_with(
            [{"kind": "table_added", "table": "public.curation_event", "type": "table"}],
        )
        text = render_human_text(diff, _options())
        assert "Added:" in text
        assert "public.curation_event" in text

    def test_table_removed_lists_fqn(self) -> None:
        diff = _diff_with([{"kind": "table_removed", "table": "public.legacy"}])
        text = render_human_text(diff, _options())
        assert "Removed:" in text
        assert "public.legacy" in text


class TestStructuredOutput:
    def test_yaml_is_multidoc(self) -> None:
        diffs = [_empty_diff(), {**_empty_diff(), "connection": "secondary"}]
        buf = StringIO()
        render_data(diffs, "yaml", buf)
        docs = list(yaml.safe_load_all(buf.getvalue()))
        assert len(docs) == 2
        assert docs[0]["connection"] == "primary"
        assert docs[1]["connection"] == "secondary"

    def test_json_is_array(self) -> None:
        diffs = [_empty_diff(), {**_empty_diff(), "connection": "secondary"}]
        buf = StringIO()
        render_data(diffs, "json", buf)
        data = json.loads(buf.getvalue())
        assert isinstance(data, list)
        assert {d["connection"] for d in data} == {"primary", "secondary"}

    def test_structured_output_ignores_threshold(self) -> None:
        # Even a tiny sub-threshold drift survives in machine output.
        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "t",
                    "column": "x",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 1.0001,
                    "delta": 0.0001,
                    "delta_pct": 0.0001,
                },
            ],
        )
        buf = StringIO()
        render_data([diff], "json", buf)
        data = json.loads(buf.getvalue())
        assert data[0]["changes"][0]["kind"] == "statistic_changed"
